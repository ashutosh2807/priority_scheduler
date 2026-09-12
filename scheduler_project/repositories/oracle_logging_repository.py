"""Durable local event outbox with replayable Oracle current-state and audit logs.

Enqueue with the business repository's SQLite connection and commit=False
before its commit. Oracle delivery never participates in business execution:
failed deliveries remain local and retry with the same event identity.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime_compat import parse_iso_datetime
from decimal import Decimal

from database.sqlite_db import get_connection
from repositories.oracle_schedule_master_repository import (
    _safe_driver_error, _safe_qualified_identifier,
    connect_oracle_from_environment, load_oracle_driver,
)


def ensure_schema(connection):
    """Create local logging tables without committing a caller's transaction."""
    for sql in (
        """CREATE TABLE IF NOT EXISTS scheduler_oracle_log_source (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            source_id TEXT NOT NULL UNIQUE)""",
        """CREATE TABLE IF NOT EXISTS scheduler_oracle_log_outbox (
            event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            dedupe_key TEXT UNIQUE,
            event_json TEXT NOT NULL,
            semantic_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT)""",
        """CREATE INDEX IF NOT EXISTS idx_oracle_outbox_pending
            ON scheduler_oracle_log_outbox(delivered_at,event_seq)""",
        """CREATE TABLE IF NOT EXISTS scheduler_oracle_log_state (
            record_key TEXT PRIMARY KEY,
            semantic_hash TEXT NOT NULL,
            event_id TEXT NOT NULL)""",
    ):
        connection.execute(sql)
    connection.execute(
        "INSERT OR IGNORE INTO scheduler_oracle_log_source(singleton,source_id) VALUES(1,?)",
        (uuid.uuid4().hex,),
    )


class OracleLoggingRepository:
    """Commit local events first; deliver ordered, idempotent Oracle batches."""

    EVENT_FIELDS = {
        "event_id", "event_type", "occurred_at", "source", "actor", "reason",
        "correlation_id", "record_key", "job_id", "name", "report_date",
        "planned_execution_date", "status", "payload",
    }
    VOLATILE_PAYLOAD_FIELDS = {"calculated_at", "updated_at", "next_evaluation"}

    def __init__(self, connection=None, enabled=None, connection_factory=None):
        self.connection = connection if connection is not None else get_connection()
        self.enabled = self._env_flag("SCHEDULER_ORACLE_LOGGING", False) if enabled is None else bool(enabled)
        self.connection_factory = connection_factory or connect_oracle_from_environment
        self.current_table = _safe_qualified_identifier(os.getenv("SCHEDULER_EXTG_TABLE", "SCHEDULE_EXTG"), "current logging table")
        self.log_table = _safe_qualified_identifier(os.getenv("SCHEDULER_EXTG_LOG_TABLE", "SCHEDULE_EXTG_LOG"), "event logging table")
        self._lock = threading.RLock()
        self.retry_seconds = self._positive_int(os.getenv("SCHEDULER_ORACLE_LOG_RETRY_SECONDS", "30"), 30)
        self.max_retry_seconds = max(self.retry_seconds, 3600)
        was_in_transaction = self.connection.in_transaction
        ensure_schema(self.connection)
        if not was_in_transaction:
            self.connection.commit()
        self.source_id = self.connection.execute(
            "SELECT source_id FROM scheduler_oracle_log_source WHERE singleton=1"
        ).fetchone()[0]

    @staticmethod
    def ensure_schema(connection):
        return ensure_schema(connection)

    def enqueue(self, event, *, connection=None, commit=True, dedupe_key=None):
        """Append one event locally, optionally inside a business transaction."""
        normalised = self._normalise_event(event)
        if normalised.get("record_key"):
            self._current_values(normalised, 0)
        if dedupe_key is not None:
            dedupe_key = str(dedupe_key)
            if not dedupe_key or len(dedupe_key) > 500:
                raise ValueError("Log dedupe key must contain 1 to 500 characters.")
        connection = self.connection if connection is None else connection
        with self._lock, self._transaction(connection, commit):
            return self._enqueue(connection, normalised, dedupe_key)

    def capture_state(self, record_key, event, *, connection=None, commit=True):
        """Append only meaningful changes to a current record's state."""
        if not isinstance(event, dict):
            raise ValueError("A logging event must be an object.")
        if event.get("record_key") not in (None, record_key):
            raise ValueError("Event record key does not match the captured record.")
        normalised = self._normalise_event({**event, "record_key": record_key})
        self._text(normalised["record_key"], 160, required=True)
        self._current_values(normalised, 0)
        fingerprint = self._fingerprint(normalised)
        connection = self.connection if connection is None else connection
        with self._lock, self._transaction(connection, commit):
            previous = connection.execute(
                "SELECT semantic_hash FROM scheduler_oracle_log_state WHERE record_key=?",
                (normalised["record_key"],),
            ).fetchone()
            if previous is not None and previous[0] == fingerprint:
                return None
            event_id = self._enqueue(connection, normalised, None)
            connection.execute(
                """INSERT INTO scheduler_oracle_log_state(record_key,semantic_hash,event_id)
                   VALUES(?,?,?) ON CONFLICT(record_key) DO UPDATE SET
                   semantic_hash=excluded.semantic_hash,event_id=excluded.event_id""",
                (normalised["record_key"], fingerprint, event_id),
            )
            return event_id

    def _enqueue(self, connection, event, dedupe_key):
        self._assert_source(connection)
        fingerprint = self._fingerprint(event)
        if dedupe_key is None:
            previous = connection.execute(
                "SELECT event_id,semantic_hash FROM scheduler_oracle_log_outbox WHERE event_id=?",
                (event["event_id"],),
            ).fetchall()
        else:
            previous = connection.execute(
                """SELECT event_id,semantic_hash FROM scheduler_oracle_log_outbox
                   WHERE event_id=? OR dedupe_key=?""", (event["event_id"], dedupe_key),
            ).fetchall()
        if previous:
            if len(previous) != 1 or previous[0][1] != fingerprint:
                raise ValueError("This log event identity was already used for different data.")
            return previous[0][0]
        connection.execute(
            """INSERT INTO scheduler_oracle_log_outbox
               (event_id,dedupe_key,event_json,semantic_hash,created_at) VALUES(?,?,?,?,?)""",
            (event["event_id"], dedupe_key, self._json(event), fingerprint, self._now()),
        )
        return event["event_id"]

    def _assert_source(self, connection):
        source = connection.execute(
            "SELECT source_id FROM scheduler_oracle_log_source WHERE singleton=1"
        ).fetchone()
        if source is None or source[0] != self.source_id:
            raise ValueError("Logging must use the same scheduler SQLite database as the event source.")

    @contextmanager
    def _transaction(self, connection, commit):
        # BEGIN before SAVEPOINT keeps commit=False pending even when the
        # caller has not started its first business INSERT yet.
        started_transaction = not connection.in_transaction
        if started_transaction:
            connection.execute("BEGIN")
        savepoint = "oracle_log_" + uuid.uuid4().hex
        connection.execute("SAVEPOINT " + savepoint)
        released = False
        try:
            yield
            connection.execute("RELEASE SAVEPOINT " + savepoint)
            released = True
            if commit:
                connection.commit()
        except Exception:
            if not released:
                connection.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                connection.execute("RELEASE SAVEPOINT " + savepoint)
                if started_transaction and commit:
                    connection.rollback()
            elif commit:
                connection.rollback()
            raise

    def flush(self, limit=100, force=False):
        """Deliver in sequence, stopping at the first failed or deferred event.

        A database commit with a lost acknowledgement is safe: the same event
        ID is retried and the immutable Oracle log is never inserted twice.
        """
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            result = {"enabled": self.enabled, "delivered": 0, "failed": 0, "deferred": False}
            if not self.enabled:
                return {**result, "pending": self.stats()["pending"]}
            if self.connection.in_transaction:
                raise RuntimeError("Commit the business transaction before flushing Oracle logs.")
            rows = self.connection.execute(
                """SELECT event_seq,event_id,event_json,attempts,next_attempt_at
                   FROM scheduler_oracle_log_outbox WHERE delivered_at IS NULL
                   ORDER BY event_seq LIMIT ?""", (limit,),
            ).fetchall()
            for sequence, event_id, event_json, attempts, next_attempt_at in rows:
                if not force and next_attempt_at and next_attempt_at > self._now():
                    result["deferred"] = True
                    break
                try:
                    self._deliver(json.loads(event_json), sequence)
                except Exception as error:
                    delay = min(self.max_retry_seconds, self.retry_seconds * (2 ** min(attempts, 10)))
                    retry_at = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="microseconds")
                    with self._transaction(self.connection, True):
                        self.connection.execute(
                            """UPDATE scheduler_oracle_log_outbox SET attempts=attempts+1,
                               next_attempt_at=?,last_error=? WHERE event_id=? AND delivered_at IS NULL""",
                            (retry_at, _safe_driver_error(error), event_id),
                        )
                    result["failed"] += 1
                    break
                # Oracle is committed before the local acknowledgement. If
                # this local write fails, leave the event available to replay.
                with self._transaction(self.connection, True):
                    self.connection.execute(
                        """UPDATE scheduler_oracle_log_outbox SET delivered_at=?,
                           attempts=attempts+1,next_attempt_at=NULL,last_error=NULL
                           WHERE event_id=? AND delivered_at IS NULL""",
                        (self._now(), event_id),
                    )
                result["delivered"] += 1
            return {**result, "pending": self.stats()["pending"]}

    def _deliver(self, event, sequence):
        connection = log_cursor = current_cursor = None
        committed = False
        try:
            connection = self.connection_factory()
            log_cursor = connection.cursor()
            log_values = self._log_values(event, sequence)
            self._set_clob_sizes(log_cursor, ("payload", "reason"), log_values)
            log_cursor.execute(self._log_sql(), log_values)
            if event.get("record_key"):
                # A driver's named input-size declarations can survive later
                # execute calls. Each SQL shape gets its own cursor so unused
                # declarations never leak into the other statement.
                current_cursor = connection.cursor()
                values = self._current_values(event, sequence)
                self._set_clob_sizes(current_cursor, ("payload", "reason", "error_info", "run_config"), values)
                current_cursor.execute(self._current_sql(), values)
            connection.commit()
            committed = True
        finally:
            if connection is not None and not committed:
                try:
                    connection.rollback()
                except Exception:
                    pass
            for resource in (current_cursor, log_cursor, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass

    def stats(self):
        with self._lock:
            counts = self.connection.execute(
                """SELECT COUNT(*),SUM(CASE WHEN delivered_at IS NULL THEN 1 ELSE 0 END),
                   MAX(delivered_at) FROM scheduler_oracle_log_outbox"""
            ).fetchone()
            pending = self.connection.execute(
                """SELECT created_at,next_attempt_at,last_error FROM scheduler_oracle_log_outbox
                   WHERE delivered_at IS NULL ORDER BY event_seq LIMIT 1"""
            ).fetchone()
            return {"enabled": self.enabled, "source_id": self.source_id,
                    "total": counts[0], "pending": counts[1] or 0,
                    "delivered": counts[0] - (counts[1] or 0), "last_delivered_at": counts[2],
                    "oldest_pending_at": pending[0] if pending else None,
                    "next_attempt_at": pending[1] if pending else None,
                    "last_error": pending[2] if pending else None}

    def _log_values(self, event, sequence):
        values = {key: event.get(key) for key in (
            "event_id", "record_key", "job_id", "name", "status", "event_type",
            "actor", "reason", "source", "correlation_id",
        )}
        values.update(source_id=self.source_id, event_seq=sequence,
                      report_date=self._date(event.get("report_date")),
                      planned_execution_date=self._date(event.get("planned_execution_date")),
                      occurred_at=self._datetime(event["occurred_at"]),
                      payload=self._json(event["payload"]))
        return values

    def _current_values(self, event, sequence):
        payload = event["payload"]
        values = {key: event.get(key) for key in ("record_key", "job_id", "name", "status", "actor", "reason")}
        values.update(
            source_id=self.source_id, event_seq=sequence,
            package_name=self._text(payload.get("package_name"), 500),
            occurrence_key=self._text(payload.get("occurrence_key"), 160),
            report_date=self._date(event.get("report_date")),
            planned_execution_date=self._date(event.get("planned_execution_date")),
            run_date=self._datetime(payload.get("run_date")),
            same_day=self._flag(payload.get("same_day")),
            attempt_no=self._number(payload.get("attempt_no")),
            executed_at=self._datetime(payload.get("executed_at")),
            records_loaded=self._number(payload.get("records_loaded", payload.get("count"))),
            error_info=self._error_text(payload.get("error_info", payload.get("error"))),
            confirmation=self._flag(payload.get("confirmation")),
            run_config=self._json(payload.get("run_config") or {}),
            time_flag=self._flag(payload.get("time_flag")),
            last_run=self._datetime(payload.get("last_run")),
            waiting_for=self._text(payload.get("waiting_for"), 100),
            payload=self._json(payload), occurred_at=self._datetime(event["occurred_at"]),
        )
        return values

    def _log_sql(self):
        columns = ("event_id", "source_id", "event_seq", "record_key", "job_id", "name",
                   "report_date", "planned_execution_date", "status", "event_type", "occurred_at",
                   "actor", "reason", "source", "correlation_id", "payload")
        return f"""MERGE INTO {self.log_table} target
            USING (SELECT :event_id AS event_id FROM dual) source_event
            ON (target.event_id=source_event.event_id)
            WHEN NOT MATCHED THEN INSERT ({', '.join(columns)}, created_at)
            VALUES ({', '.join(':' + name for name in columns)}, SYSTIMESTAMP)"""

    def _current_sql(self):
        keys = ("source_id", "record_key")
        columns = ("event_seq", "job_id", "name", "package_name", "occurrence_key", "report_date",
                   "planned_execution_date", "run_date", "status", "same_day", "attempt_no",
                   "executed_at", "records_loaded", "error_info", "confirmation", "run_config",
                   "time_flag", "last_run", "actor", "reason", "waiting_for", "payload")
        return f"""MERGE INTO {self.current_table} target
            USING (SELECT :source_id AS source_id, :record_key AS record_key FROM dual) source_event
            ON (target.source_id=source_event.source_id AND target.record_key=source_event.record_key)
            WHEN MATCHED THEN UPDATE SET {', '.join('target.' + name + '=:' + name for name in columns)},
                target.updated_at=:occurred_at
                WHERE target.event_seq < :event_seq
            WHEN NOT MATCHED THEN INSERT ({', '.join(keys + columns)}, created_date, updated_at)
            VALUES ({', '.join(':' + name for name in keys + columns)}, :occurred_at, :occurred_at)"""

    @staticmethod
    def _set_clob_sizes(cursor, names, values=None):
        if not hasattr(cursor, "setinputsizes"):
            return
        driver = load_oracle_driver()
        clob = getattr(driver, "DB_TYPE_CLOB", getattr(driver, "CLOB", None))
        if clob is None:
            raise RuntimeError("The Oracle driver does not provide CLOB binding support.")
        timestamp_names = {name for name, value in (values or {}).items() if isinstance(value, datetime)}
        timestamp = getattr(driver, "DB_TYPE_TIMESTAMP", getattr(driver, "TIMESTAMP", None))
        if timestamp_names and timestamp is None:
            raise RuntimeError("The Oracle driver does not provide TIMESTAMP binding support.")
        cursor.setinputsizes(**{**{name: clob for name in names},
                               **{name: timestamp for name in timestamp_names}})

    @classmethod
    def _normalise_event(cls, event):
        if not isinstance(event, dict) or set(event) - cls.EVENT_FIELDS:
            raise ValueError("A logging event must contain only supported event fields.")
        if not isinstance(event.get("payload", {}), dict):
            raise ValueError("Logging payload must be an object.")
        value = cls._clean(copy.deepcopy(event))
        value["event_id"] = cls._text(value.get("event_id") or uuid.uuid4().hex, 64, required=True)
        value["event_type"] = cls._text(value.get("event_type"), 100, required=True)
        value["source"] = cls._text(value.get("source") or "SCHEDULER", 80, required=True)
        value["occurred_at"] = cls._datetime(value.get("occurred_at") or datetime.now()).isoformat(timespec="microseconds")
        for key, length in (("record_key", 160), ("name", 255), ("status", 80), ("actor", 255), ("correlation_id", 64)):
            value[key] = cls._text(value.get(key), length)
        if value["record_key"]:
            cls._text(value["name"], 255, required=True)
            cls._text(value["status"], 80, required=True)
        for key in ("report_date", "planned_execution_date"):
            parsed = cls._date(value.get(key))
            value[key] = parsed.isoformat() if parsed is not None else None
        if value.get("job_id") is not None:
            if isinstance(value["job_id"], bool) or not str(value["job_id"]).isdigit():
                raise ValueError("Logging job ID must be a positive integer.")
            value["job_id"] = int(value["job_id"])
            if value["job_id"] < 1:
                raise ValueError("Logging job ID must be a positive integer.")
        value["reason"] = cls._error_text(value.get("reason"))
        value["payload"] = value.get("payload", {})
        cls._json(value)
        return value

    @classmethod
    def _fingerprint(cls, event):
        semantic = {key: value for key, value in event.items() if key not in {"event_id", "occurred_at"}}
        semantic["payload"] = {key: value for key, value in event["payload"].items()
                               if str(key).lower() not in cls.VOLATILE_PAYLOAD_FIELDS}
        return hashlib.sha256(cls._json(semantic).encode("utf-8")).hexdigest()

    @classmethod
    def _clean(cls, value):
        if isinstance(value, dict):
            return {str(key): "***" if re.search(r"(^|_)(password|passwd|pwd|secret|token|dsn|connection_string)$", str(key), re.I)
                    else cls._clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._clean(item) for item in value]
        if isinstance(value, str):
            return cls._redact(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        return value

    @staticmethod
    def _redact(value):
        text = re.sub(r"(?i)(password|passwd|pwd|secret|token)\s*[:=]\s*[^\s,;]+", r"\1=***", str(value))
        text = re.sub(r"(?i)(user(?:name)?|uid)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
        return re.sub(r"(?i)\b[^\s/@:]+/[^\s@]+@[^\s,;]+", "***", text)

    @classmethod
    def _error_text(cls, value):
        if value is None:
            return None
        return cls._redact(cls._json(value) if isinstance(value, (dict, list)) else str(value))

    @staticmethod
    def _text(value, length, required=False):
        if value is None or value == "":
            if required:
                raise ValueError("A required logging text field is missing.")
            return None
        text = str(value).strip()
        if not text and required or len(text) > length:
            raise ValueError("A logging text field is empty or exceeds its supported length.")
        return text or None

    @staticmethod
    def _datetime(value):
        if value in (None, ""):
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return datetime.combine(value, datetime.min.time())
        parsed = value if isinstance(value, datetime) else parse_iso_datetime(str(value))
        # Oracle TIMESTAMP has no timezone; store application-local wall time.
        return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo is not None else parsed

    @classmethod
    def _date(cls, value):
        if value in (None, ""):
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return cls._datetime(value).date()

    @staticmethod
    def _number(value):
        if value in (None, ""):
            return None
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("Logging numeric values must be finite.")
        return int(number) if number == number.to_integral_value() else number

    @staticmethod
    def _flag(value):
        if value is None:
            return None
        if isinstance(value, str):
            return 1 if value.strip().upper() in {"1", "Y", "YES", "TRUE", "T", "CONFIRMED"} else 0
        return int(bool(value))

    @staticmethod
    def _json(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _now():
        return datetime.now().isoformat(timespec="microseconds")

    @staticmethod
    def _positive_int(value, default):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _env_flag(name, default):
        return os.getenv(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}
