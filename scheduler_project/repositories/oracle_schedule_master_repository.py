"""Oracle-backed, read-only Schedule Master snapshot support.

The scheduler continues to consume ``Schedule_Master.json``.  When Oracle is
selected as the source, this repository reads the authoritative
``SCHEDULE_EXTRACT_MASTER`` table, validates every row, then atomically
replaces that JSON snapshot.  This gives the scheduler a stable file contract:
it sees either the last complete snapshot or the next complete snapshot, never
a partially written file.

Credentials are deliberately read only from the process environment.  They are
never written to the snapshot, included in exceptions, or logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from database.oracle_driver import (
    OracleDriverError, connect_oracle,
    load_oracle_driver as _load_configured_oracle_driver,
)


logger = logging.getLogger("scheduler.schedule_master.oracle")


class OracleMasterSyncError(RuntimeError):
    """A safe, operator-facing error while reading Oracle Schedule Master."""


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
_PROCEDURE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_$#]{0,127}(?:\.[A-Za-z][A-Za-z0-9_$#]{0,127}){0,2}$"
)

_BASE_COLUMNS = (
    "ID",
    "NAME",
    "PACKAGE_NAME",
    "RUN_CONFIG",
    "MARGIN",
    "SAME_DAY",
    "IS_ACTIVE",
    "CREATED_DATE",
)

_FREQUENCY_ALIASES = {
    "DAILY": "DAILY",
    "WEEKLY": "WEEKLY",
    "FORTNIGHTLY": "FORTNIGHTLY",
    "MONTHLY": "MONTHLY",
    "QUARTERLY": "QUARTERLY",
    "HALF-YEARLY": "HALF-YEARLY",
    "HALF_YEARLY": "HALF-YEARLY",
    "HALF YEARLY": "HALF-YEARLY",
    "BI-ANNUALLY": "HALF-YEARLY",
    "BI_ANNUALLY": "HALF-YEARLY",
    "BIANNUALLY": "HALF-YEARLY",
    "ANNUALLY": "ANNUALLY",
    "SPECIFIC_DATE": "SPECIFIC_DATE",
    "ON_SPECIFIC_DATE": "SPECIFIC_DATE",
    "ON A SPECIFIC DATE": "SPECIFIC_DATE",
    "ON SPECIFIC DATE": "SPECIFIC_DATE",
}
_HOLIDAY_ALIASES = {
    "SAT": "SAT",
    "SATURDAY": "SAT",
    "SUN": "SUN",
    "SUNDAY": "SUN",
    "HOLIDAY": "HOLIDAY",
}


def load_oracle_driver() -> Any:
    """Compatibility entry point shared by snapshots, logs and utilities."""
    try:
        return _load_configured_oracle_driver()
    except OracleDriverError as error:
        raise OracleMasterSyncError(str(error)) from None


def _safe_qualified_identifier(value: str, label: str) -> str:
    parts = str(value or "").strip().split(".")
    if not 1 <= len(parts) <= 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise OracleMasterSyncError(f"Invalid Oracle {label} configuration.")
    return ".".join(parts)


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().upper() in {"1", "Y", "YES", "TRUE", "T"}
    return bool(value)


def _lob_value(value: Any) -> Any:
    """Materialise CLOB/BLOB values without depending on a particular driver."""
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _json_value(value: Any) -> Any:
    value = _lob_value(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def connect_oracle_from_environment() -> Any:
    """Open an Oracle connection from the approved process environment.

    This intentionally centralises the credential path used by Schedule Master
    and calendar synchronisation.  It never returns configuration text in an
    exception, because callers persist operational errors for the web UI.
    """
    driver = load_oracle_driver()
    user = os.getenv("ORACLE_USER", "").strip()
    password = os.getenv("ORACLE_PASSWORD", "")
    dsn = os.getenv("ORACLE_DSN", "").strip() or _build_dsn_from_environment(driver)
    if not user or not password or not dsn:
        raise OracleMasterSyncError(
            "Oracle sync is not configured. Set ORACLE_USER, ORACLE_PASSWORD, and ORACLE_DSN (or host plus service/SID)."
        )
    try:
        return connect_oracle(user=user, password=password, dsn=dsn, driver=driver)
    except Exception as exc:
        raise OracleMasterSyncError(_safe_driver_error(exc)) from None


def _build_dsn_from_environment(driver: Any) -> str:
    host = os.getenv("ORACLE_HOST", "").strip()
    port = os.getenv("ORACLE_PORT", "1521").strip()
    service = os.getenv("ORACLE_SERVICE", "").strip()
    sid = os.getenv("ORACLE_SID", "").strip()
    if not host or not port or not (service or sid):
        return ""
    try:
        port_number = int(port)
    except ValueError:
        return ""
    if service:
        return driver.makedsn(host, port_number, service_name=service)
    return driver.makedsn(host, port_number, sid=sid)


def _safe_driver_error(exc: Exception) -> str:
    message = str(exc).upper()
    if "ORA-01017" in message or "DPY-4010" in message:
        return "Oracle authentication was rejected while refreshing scheduler inputs."
    if "ORA-12154" in message or "DPY-4027" in message:
        return "Oracle could not resolve the configured scheduler connect identifier."
    if "ORA-12514" in message or "ORA-12505" in message:
        return "Oracle listener does not recognise the configured service name or SID."
    if "ORA-12541" in message or "ORA-12543" in message:
        return "Oracle listener is not reachable for scheduler input refresh."
    return "Oracle scheduler input refresh failed. Verify database availability and approved configuration."


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write one JSON snapshot safely for readers running in other threads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError:
        raise OracleMasterSyncError("Could not publish a scheduler JSON snapshot.") from None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


class OracleScheduleMasterRepository:
    """Fetch and validate the authoritative Oracle master table.

    ``connection_factory`` is intentionally injectable for deterministic tests
    and for a future approved connection-pool integration.  Production uses
    the same ``ORACLE_*`` environment variables as the procedure executor.
    """

    def __init__(
        self,
        snapshot_path: str | Path,
        *,
        connection_factory: Callable[[], Any] | None = None,
        table_name: str | None = None,
        optional_columns: tuple[str, ...] | None = None,
    ) -> None:
        self.snapshot_path = Path(snapshot_path)
        self.connection_factory = connection_factory
        self.table_name = _safe_qualified_identifier(
            table_name or os.getenv("SCHEDULER_MASTER_TABLE", "SCHEDULE_EXTRACT_MASTER"),
            "Schedule Master table",
        )
        self.optional_columns = optional_columns if optional_columns is not None else self._optional_columns_from_env()

    def refresh_snapshot(self) -> list[dict[str, Any]]:
        """Fetch a fully valid Oracle snapshot and atomically publish it."""
        records = self.fetch_records()
        self.write_snapshot(records)
        logger.info("Refreshed Schedule Master snapshot with %s records.", len(records))
        return records

    def fetch_records(self) -> list[dict[str, Any]]:
        connection = None
        cursor = None
        try:
            connection = self.connection_factory() if self.connection_factory else self._connect()
            cursor = connection.cursor()
            columns = (*_BASE_COLUMNS, *self.optional_columns)
            cursor.execute(
                f"SELECT {', '.join(columns)} FROM {self.table_name} ORDER BY ID"
            )
            returned_columns = [str(item[0]).upper() for item in cursor.description]
            records = [
                self._normalise_record(dict(zip(returned_columns, row)))
                for row in cursor.fetchall()
            ]
            self._assert_unique_ids(records)
            return records
        except OracleMasterSyncError:
            raise
        except Exception as exc:
            # Driver exceptions can contain a complete connect descriptor.  Do
            # not pass that raw text into logs, API responses, or the UI.
            raise OracleMasterSyncError(self._safe_driver_error(exc)) from None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def write_snapshot(self, records: list[dict[str, Any]]) -> None:
        """Publish JSON via replace, never by modifying the active file in place."""
        _atomic_write_json(self.snapshot_path, records)

    def _connect(self) -> Any:
        return connect_oracle_from_environment()

    @staticmethod
    def _optional_columns_from_env() -> tuple[str, ...]:
        raw = os.getenv("SCHEDULER_MASTER_OPTIONAL_COLUMNS", "")
        result: list[str] = []
        for item in raw.split(","):
            value = item.strip()
            if not value:
                continue
            result.append(_safe_qualified_identifier(value, "optional Schedule Master column"))
        return tuple(result)

    @staticmethod
    def _safe_driver_error(exc: Exception) -> str:
        return _safe_driver_error(exc)

    def _normalise_record(self, row: dict[str, Any]) -> dict[str, Any]:
        source_id = _lob_value(row.get("ID"))
        try:
            record_id = int(source_id)
        except (TypeError, ValueError):
            raise OracleMasterSyncError("Schedule Master contains a row without a valid ID.") from None

        name = str(_lob_value(row.get("NAME")) or "").strip()
        if not name:
            raise OracleMasterSyncError(f"Schedule Master record {record_id} has no name.")

        package_name = str(_lob_value(row.get("PACKAGE_NAME")) or "").strip()
        if not _PROCEDURE.fullmatch(package_name):
            raise OracleMasterSyncError(
                f"Schedule Master record {record_id} has an invalid Oracle procedure name."
            )

        is_active = int(_truthy(_lob_value(row.get("IS_ACTIVE")), default=True))
        run_config = self._normalise_run_config(row.get("RUN_CONFIG"), record_id, bool(is_active))
        margin = self._normalise_margin(run_config.get("MARGIN", row.get("MARGIN")), record_id)
        run_by = run_config.get("RUN_BY")

        configured_time_flag = row.get("TIME_FLAG", run_config.get("TIME_FLAG"))
        time_flag = int(_truthy(configured_time_flag, default=bool(run_by)))
        confirmation_needed = int(
            _truthy(row.get("CONFIRMATION_NEEDED", row.get("CONFIRMATION", run_config.get("CONFIRMATION_NEEDED"))))
        )

        return {
            "id": record_id,
            "name": name,
            "package_name": package_name,
            "run_config": run_config,
            "margin": margin,
            "same_day": int(_truthy(_lob_value(row.get("SAME_DAY")))),
            "time_flag": time_flag,
            "is_active": is_active,
            "created_date": _json_value(row.get("CREATED_DATE")),
            "confirmation_needed": confirmation_needed,
        }

    def _normalise_run_config(self, value: Any, record_id: int, active: bool) -> dict[str, Any]:
        raw = _lob_value(value)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            config: dict[str, Any] = {}
        elif isinstance(raw, dict):
            config = dict(raw)
        elif isinstance(raw, str):
            try:
                config = json.loads(raw)
            except json.JSONDecodeError:
                raise OracleMasterSyncError(
                    f"Schedule Master record {record_id} has invalid RUN_CONFIG JSON."
                ) from None
        else:
            raise OracleMasterSyncError(
                f"Schedule Master record {record_id} has an unsupported RUN_CONFIG value."
            )
        if not isinstance(config, dict):
            raise OracleMasterSyncError(
                f"Schedule Master record {record_id} RUN_CONFIG must be a JSON object."
            )

        canonical = dict(config)
        runs_on = config.get("RUNS_ON", config.get("runs_on", []))
        if isinstance(runs_on, str):
            runs_on = [runs_on]
        if not isinstance(runs_on, (list, tuple, set)):
            raise OracleMasterSyncError(
                f"Schedule Master record {record_id} RUNS_ON must be a list."
            )
        normalised_frequencies: list[str] = []
        for frequency in runs_on:
            key = str(frequency or "").strip().upper().replace("_", "_")
            value = _FREQUENCY_ALIASES.get(key)
            if value is None:
                raise OracleMasterSyncError(
                    f"Schedule Master record {record_id} has unsupported frequency {frequency!r}."
                )
            if value not in normalised_frequencies:
                normalised_frequencies.append(value)
        if active and not normalised_frequencies:
            raise OracleMasterSyncError(
                f"Active Schedule Master record {record_id} has no RUNS_ON frequency."
            )
        canonical["RUNS_ON"] = normalised_frequencies
        canonical.pop("runs_on", None)

        if "SPECIFIC_DATE" in normalised_frequencies:
            raw_dates = config.get(
                "SPECIFIC_DATES",
                config.get("SPECIFIC_DATE", config.get("specific_date")),
            )
            if raw_dates is None:
                raise OracleMasterSyncError(
                    f"Schedule Master record {record_id} uses SPECIFIC_DATE without a date."
                )
            if not isinstance(raw_dates, (list, tuple, set)):
                raw_dates = [raw_dates]
            specific_dates: list[str] = []
            for raw_date in raw_dates:
                normalised_date = self._normalise_specific_date(raw_date, record_id)
                if normalised_date not in specific_dates:
                    specific_dates.append(normalised_date)
            if not specific_dates:
                raise OracleMasterSyncError(
                    f"Schedule Master record {record_id} uses SPECIFIC_DATE without a date."
                )
            canonical["SPECIFIC_DATES"] = specific_dates
            canonical.pop("specific_date", None)

        raw_window = config.get("RUN_BY", config.get("BY_TIME", config.get("run_by")))
        if raw_window is not None:
            if not isinstance(raw_window, dict):
                raise OracleMasterSyncError(
                    f"Schedule Master record {record_id} RUN_BY must be an object."
                )
            from_time = raw_window.get("FROM_TIME", raw_window.get("FROM", raw_window.get("from_time")))
            to_time = raw_window.get("TO_TIME", raw_window.get("TO", raw_window.get("to_time")))
            if not from_time or not to_time:
                raise OracleMasterSyncError(
                    f"Schedule Master record {record_id} RUN_BY needs FROM_TIME and TO_TIME."
                )
            canonical["RUN_BY"] = {
                "FROM_TIME": self._normalise_time(from_time, record_id),
                "TO_TIME": self._normalise_time(to_time, record_id),
            }
            canonical.pop("BY_TIME", None)
            canonical.pop("run_by", None)

        holiday_run = config.get("HOLIDAY_RUN", config.get("holiday_run", []))
        if isinstance(holiday_run, str):
            holiday_run = [holiday_run]
        if not isinstance(holiday_run, (list, tuple, set)):
            raise OracleMasterSyncError(
                f"Schedule Master record {record_id} HOLIDAY_RUN must be a list."
            )
        normalised_holidays: list[str] = []
        for value in holiday_run:
            normalised = _HOLIDAY_ALIASES.get(str(value or "").strip().upper())
            if normalised is None:
                raise OracleMasterSyncError(
                    f"Schedule Master record {record_id} has unsupported HOLIDAY_RUN value {value!r}."
                )
            if normalised not in normalised_holidays:
                normalised_holidays.append(normalised)
        canonical["HOLIDAY_RUN"] = normalised_holidays
        canonical.pop("holiday_run", None)

        return canonical

    @staticmethod
    def _normalise_time(value: Any, record_id: int) -> str:
        text = str(_lob_value(value) or "").strip()
        for pattern in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(text, pattern).strftime("%H:%M")
            except ValueError:
                continue
        raise OracleMasterSyncError(
            f"Schedule Master record {record_id} has invalid RUN_BY time."
        )

    @staticmethod
    def _normalise_specific_date(value: Any, record_id: int) -> str:
        value = _lob_value(value)
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            text = value.strip()
            try:
                return date.fromisoformat(text).isoformat()
            except ValueError:
                try:
                    return datetime.fromisoformat(text).date().isoformat()
                except ValueError:
                    pass
        raise OracleMasterSyncError(
            f"Schedule Master record {record_id} has an invalid SPECIFIC_DATE."
        )

    @staticmethod
    def _normalise_margin(value: Any, record_id: int) -> str:
        text = str(_lob_value(value) if value is not None else "T").strip().upper()
        if text in {"", "T", "T+0", "0", "+0"}:
            return "T"
        if re.fullmatch(r"T\+\d+", text):
            return text
        if re.fullmatch(r"\+?\d+", text):
            return f"T+{int(text.lstrip('+'))}"
        raise OracleMasterSyncError(
            f"Schedule Master record {record_id} has invalid margin. Use T or T+N."
        )

    @staticmethod
    def _assert_unique_ids(records: list[dict[str, Any]]) -> None:
        identifiers = [record["id"] for record in records]
        if len(identifiers) != len(set(identifiers)):
            raise OracleMasterSyncError("Schedule Master contains duplicate IDs.")


class OracleCalendarSnapshotRepository:
    """Read DATEMAST plus an explicitly configured extra holiday source.

    DATEMAST is bounded to the previous financial-year boundary (31 March)
    through the current local day. Optional holiday sources retain future
    dates without this bound.

    Each upstream result is fetched and validated before either file is
    replaced.  The files are separately atomically replaced, so readers retain
    a complete old or complete new JSON document even if a process is stopped
    during a refresh.  A failed database fetch does not touch either snapshot.
    """

    def __init__(
        self,
        datemast_snapshot_path: str | Path,
        holiday_snapshot_path: str | Path,
        *,
        connection_factory: Callable[[], Any] | None = None,
        datemast_table: str | None = None,
        datemast_date_column: str | None = None,
        holiday_table: str | None = None,
        holiday_date_column: str | None = None,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        self.datemast_snapshot_path = Path(datemast_snapshot_path)
        self.holiday_snapshot_path = Path(holiday_snapshot_path)
        self.connection_factory = connection_factory
        self.today_provider = today_provider or date.today
        self.datemast_table = _safe_qualified_identifier(
            datemast_table or os.getenv("SCHEDULER_DATEMAST_TABLE", "DATEMAST"),
            "DATEMAST table",
        )
        self.datemast_date_column = self._single_identifier(
            datemast_date_column or os.getenv("SCHEDULER_DATEMAST_DATE_COLUMN", "REPORT_DATE"),
            "DATEMAST date column",
        )
        # DATEMAST + the shared bank calendar is the complete default. Do not
        # assume a HOLIDAY_MASTER table exists in the bank's Oracle schema.
        configured_holiday_table = (
            os.getenv("SCHEDULER_HOLIDAY_TABLE", "") if holiday_table is None else holiday_table
        ).strip()
        self.holiday_table = _safe_qualified_identifier(
            configured_holiday_table, "holiday table",
        ) if configured_holiday_table else None
        self.holiday_date_column = self._single_identifier(
            holiday_date_column or os.getenv("SCHEDULER_HOLIDAY_DATE_COLUMN", "HOLIDAY_DATE"),
            "holiday date column",
        ) if self.holiday_table else None

    def refresh_snapshots(self) -> dict[str, Any]:
        """Publish configured sources, preserving an unused holiday snapshot."""
        connection = None
        try:
            coverage_start, coverage_end = self._coverage_dates()
            connection = self.connection_factory() if self.connection_factory else connect_oracle_from_environment()
            report_dates = self._fetch_dates(
                connection,
                table=self.datemast_table,
                date_column=self.datemast_date_column,
                label="DATEMAST",
                date_from=coverage_start,
                date_until=coverage_end + timedelta(days=1),
            )
            holidays = self._fetch_dates(
                connection,
                table=self.holiday_table,
                date_column=self.holiday_date_column,
                label="HOLIDAY_MASTER",
            ) if self.holiday_table else None
        except OracleMasterSyncError:
            raise
        except Exception as exc:
            raise OracleMasterSyncError(_safe_driver_error(exc)) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

        # Do not create new output until both upstream result sets are fully
        # validated. Persist the coverage with its dates so an omitted date
        # outside the fetched range cannot be mistaken for a bank holiday.
        coverage = {"coverage_start": coverage_start.isoformat(), "coverage_end": coverage_end.isoformat()}
        _atomic_write_json(self.datemast_snapshot_path, {"report_dates": report_dates, **coverage})
        if holidays is not None:
            _atomic_write_json(self.holiday_snapshot_path, holidays)
        logger.info(
            "Refreshed calendar snapshots with %s DATEMAST dates and %s holidays.",
            len(report_dates),
            len(holidays) if holidays is not None else "no extra Oracle source configured",
        )
        return {"datemast": len(report_dates), "holidays": len(holidays) if holidays is not None else None, **coverage}

    def read_calendar(self) -> dict[str, Any]:
        """Read live calendar inputs without changing either protected file.

        DATEMAST is independently authoritative. No extra Oracle holiday source
        is needed for the default bank calendar. If an explicitly configured
        source fails, retain its prior snapshot and report that failure.
        """
        connection = None
        try:
            coverage_start, coverage_end = self._coverage_dates()
            connection = self.connection_factory() if self.connection_factory else connect_oracle_from_environment()
            report_dates = self._fetch_dates(connection, table=self.datemast_table,
                                             date_column=self.datemast_date_column, label="DATEMAST",
                                             date_from=coverage_start, date_until=coverage_end + timedelta(days=1))
            warning = None
            holidays = None
            if self.holiday_table:
                try:
                    holidays = self._fetch_dates(connection, table=self.holiday_table,
                                                  date_column=self.holiday_date_column, label="HOLIDAY_MASTER")
                except OracleMasterSyncError:
                    warning = "The configured Oracle holiday source could not be read; previous holiday dates are retained. DATEMAST and bank-calendar rules remain available."
            return {"report_dates": report_dates, "holidays": holidays, "warning": warning,
                    "coverage_start": coverage_start.isoformat(), "coverage_end": coverage_end.isoformat()}
        except OracleMasterSyncError:
            raise
        except Exception as error:
            raise OracleMasterSyncError(_safe_driver_error(error)) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _coverage_dates(self) -> tuple[date, date]:
        today = date.fromisoformat(self._normalise_date(self.today_provider(), "Calendar date"))
        financial_year_start = today.year if today.month >= 4 else today.year - 1
        return date(financial_year_start - 1, 3, 31), today

    @classmethod
    def _fetch_dates(cls, connection: Any, *, table: str, date_column: str, label: str,
                     date_from: date | None = None, date_until: date | None = None) -> list[str]:
        cursor = None
        try:
            cursor = connection.cursor()
            sql = f"SELECT DISTINCT {date_column} FROM {table} WHERE {date_column} IS NOT NULL"
            if date_from is None and date_until is None:
                cursor.execute(sql + f" ORDER BY {date_column}")
            else:
                if date_from is None or date_until is None or date_until <= date_from:
                    raise OracleMasterSyncError("Calendar date coverage is invalid.")
                # Keep the indexed Oracle date column bare in predicates.
                # An exclusive tomorrow-midnight bound includes all of today,
                # even if legacy Oracle DATE values contain a time component.
                sql += f" AND {date_column} >= :date_from AND {date_column} < :date_until ORDER BY {date_column}"
                cursor.execute(sql, {
                    "date_from": datetime.combine(date_from, datetime.min.time()),
                    "date_until": datetime.combine(date_until, datetime.min.time()),
                })
            values = [cls._normalise_date(row[0], label) for row in cursor.fetchall()]
            return sorted(set(values))
        except OracleMasterSyncError:
            raise
        except Exception as exc:
            raise OracleMasterSyncError(_safe_driver_error(exc)) from None
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass

    @staticmethod
    def _normalise_date(value: Any, label: str) -> str:
        value = _lob_value(value)
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, str):
            text = value.strip()
            for parser in (date.fromisoformat, lambda item: datetime.fromisoformat(item).date()):
                try:
                    return parser(text).isoformat()
                except ValueError:
                    continue
        raise OracleMasterSyncError(f"{label} contains an invalid date value.")

    @staticmethod
    def _single_identifier(value: str, label: str) -> str:
        result = str(value or "").strip()
        if not _IDENTIFIER.fullmatch(result):
            raise OracleMasterSyncError(f"Invalid Oracle {label} configuration.")
        return result
