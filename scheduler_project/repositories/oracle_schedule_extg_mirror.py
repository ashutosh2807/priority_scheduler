"""Best-effort Oracle mirror for scheduler-owned Schedule_extg status.

The local SQLite/JSON scheduler state is authoritative for the Python worker.
This module mirrors a compact, operational status record to Oracle only after
the local state update has succeeded.  It deliberately never raises from the
worker path: a temporary database outage must not turn a completed extract into
a failed local job.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime
from datetime_compat import parse_iso_datetime
from typing import Any, Callable

from repositories.oracle_schedule_master_repository import (
    OracleMasterSyncError,
    _safe_qualified_identifier,
    connect_oracle_from_environment,
)


logger = logging.getLogger("scheduler.schedule_extg.oracle")


class OracleScheduleExtgMirror:
    """Mirror PENDING/RUNNING/SUCCESS/FAILED states without controlling work."""

    VALID_STATUSES = {"PENDING", "RUNNING", "SUCCESS", "FAILED"}

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        connection_factory: Callable[[], Any] | None = None,
        table_name: str | None = None,
    ) -> None:
        self.enabled = self._env_flag("SCHEDULER_EXTG_MIRROR", False) if enabled is None else bool(enabled)
        self.connection_factory = connection_factory
        configured_table = table_name or os.getenv("SCHEDULER_EXTG_TABLE", "SCHEDULE_EXTG")
        # An optional disabled mirror must never prevent a worker starting.
        # Validate its dynamic SQL identifier only when the mirror is enabled.
        self.table_name = (
            _safe_qualified_identifier(configured_table, "Schedule_extg table")
            if self.enabled
            else str(configured_table)
        )

    def mirror(self, record: dict[str, Any]) -> bool:
        """Try one idempotent mirror operation and return whether it succeeded.

        The local record ID is used as ``SOURCE_RECORD_ID`` in Oracle.  It is
        stable across retries and also distinguishes manual runs where the
        report date is intentionally null.
        """
        if not self.enabled:
            return False
        try:
            values = self._normalise_record(record)
            connection = self.connection_factory() if self.connection_factory else connect_oracle_from_environment()
            cursor = None
            try:
                cursor = connection.cursor()
                cursor.execute(self._merge_sql(), values)
                connection.commit()
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                try:
                    connection.close()
                except Exception:
                    pass
            return True
        except OracleMasterSyncError:
            logger.warning("Oracle Schedule_extg mirror is unavailable; local scheduler state remains authoritative.")
        except Exception:
            # Includes malformed optional mirror records and non-Oracle DB-API
            # failures. Never log the driver text because it may contain a
            # descriptor or credentials.
            logger.warning("Oracle Schedule_extg mirror failed; local scheduler state remains authoritative.")
        return False

    def _merge_sql(self) -> str:
        return f"""
            MERGE INTO {self.table_name} target
            USING (SELECT :source_record_id AS source_record_id FROM dual) source
               ON (target.source_record_id = source.source_record_id)
            WHEN MATCHED THEN UPDATE SET
                target.report_date = :report_date,
                target.name = :name,
                target.status = :status,
                target.same_day = :same_day,
                target.run_date = :run_date,
                target.error_info = :error_info,
                target.result_count = :result_count,
                target.updated_at = SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT (
                source_record_id, report_date, name, status, same_day,
                run_date, error_info, result_count, updated_at
            ) VALUES (
                :source_record_id, :report_date, :name, :status, :same_day,
                :run_date, :error_info, :result_count, SYSTIMESTAMP
            )
        """

    @classmethod
    def _normalise_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise OracleMasterSyncError("Schedule_extg mirror record is invalid.")
        try:
            source_record_id = int(record["id"])
        except (KeyError, TypeError, ValueError):
            raise OracleMasterSyncError("Schedule_extg mirror record has no valid source ID.") from None
        name = str(record.get("name") or "").strip()
        if not name:
            raise OracleMasterSyncError("Schedule_extg mirror record has no name.")
        status = str(record.get("status") or "").strip().upper()
        if status not in cls.VALID_STATUSES:
            raise OracleMasterSyncError("Schedule_extg mirror record has an unsupported status.")
        return {
            "source_record_id": source_record_id,
            "report_date": cls._as_date(record.get("report_date")),
            "name": name[:100],
            "status": status,
            "same_day": 1 if cls._truthy(record.get("same_day")) else 0,
            "run_date": cls._as_datetime(record.get("last_run") or record.get("run_date")),
            "error_info": cls._redact_error(record.get("error_info")),
            "result_count": cls._as_number(record.get("count")),
        }

    @staticmethod
    def _as_date(value: Any) -> date | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value).strip()
        for parser in (date.fromisoformat, lambda item: datetime.fromisoformat(item).date()):
            try:
                return parser(text)
            except ValueError:
                continue
        raise OracleMasterSyncError("Schedule_extg mirror record has an invalid report date.")

    @staticmethod
    def _as_datetime(value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        text = str(value).strip()
        try:
            return parse_iso_datetime(text)
        except ValueError:
            raise OracleMasterSyncError("Schedule_extg mirror record has an invalid run date.") from None

    @staticmethod
    def _as_number(value: Any) -> int | float | None:
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if number.is_integer() else number

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().upper() in {"1", "Y", "YES", "TRUE", "T"}
        return bool(value)

    @staticmethod
    def _redact_error(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        # Error text may come from a driver.  Keep useful ORA diagnostics but
        # never replicate common credential-shaped fragments to Oracle.
        text = re.sub(r"(?i)(password|passwd|pwd)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
        text = re.sub(r"(?i)(user(?:name)?|uid)\s*[:=]\s*[^\s,;]+", r"\1=***", text)
        text = re.sub(r"(?i)\b[^\s/@:]+/[^\s@]+@[^\s,;]+", "***", text)
        return text[:4000]

    @staticmethod
    def _env_flag(name: str, default: bool) -> bool:
        return os.getenv(name, "1" if default else "0").strip().lower() in {
            "1", "true", "yes", "on",
        }
