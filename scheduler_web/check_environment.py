"""Check a moved installation without starting jobs or changing Oracle data."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys

from project_paths import PORTAL_DIR, project_path


class EnvironmentCheckError(RuntimeError):
    """An operator-facing setup problem that contains no connection secrets."""


def _identifier(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*(?:\.[A-Za-z][A-Za-z0-9_$#]*)?", value):
        raise EnvironmentCheckError("An Oracle table or date-column setting is invalid.")
    return value


def probe_oracle(connection, environment):
    """Zero-row SELECT probes: no report rows, DDL, DML or procedure calls."""
    checks = [
        ("Schedule Master", environment.get("SCHEDULER_MASTER_TABLE") or "SCHEDULE_EXTRACT_MASTER",
         "ID, NAME, PACKAGE_NAME, RUN_CONFIG, MARGIN, SAME_DAY, IS_ACTIVE, CREATED_DATE"),
        ("DATEMAST", environment.get("SCHEDULER_DATEMAST_TABLE") or "DATEMAST",
         _identifier(environment.get("SCHEDULER_DATEMAST_DATE_COLUMN") or "REPORT_DATE")),
    ]
    if environment.get("SCHEDULER_HOLIDAY_TABLE", "").strip():
        checks.append(("Holiday table", environment["SCHEDULER_HOLIDAY_TABLE"],
                       _identifier(environment.get("SCHEDULER_HOLIDAY_DATE_COLUMN") or "HOLIDAY_DATE")))
    if environment.get("SCHEDULER_ORACLE_LOGGING", "0").strip().lower() in {"1", "true", "yes", "on"}:
        checks.extend([
            ("Current status logging", environment.get("SCHEDULER_EXTG_TABLE") or "SCHEDULE_EXTG",
             "SOURCE_ID, RECORD_KEY, EVENT_SEQ, JOB_ID, NAME, PACKAGE_NAME, OCCURRENCE_KEY, REPORT_DATE, "
             "PLANNED_EXECUTION_DATE, RUN_DATE, STATUS, SAME_DAY, ATTEMPT_NO, EXECUTED_AT, RECORDS_LOADED, "
             "ERROR_INFO, CONFIRMATION, RUN_CONFIG, TIME_FLAG, LAST_RUN, ACTOR, REASON, WAITING_FOR, UPDATED_AT, PAYLOAD"),
            ("Event logging", environment.get("SCHEDULER_EXTG_LOG_TABLE") or "SCHEDULE_EXTG_LOG",
             "EVENT_ID, SOURCE_ID, EVENT_SEQ, RECORD_KEY, JOB_ID, NAME, REPORT_DATE, PLANNED_EXECUTION_DATE, "
             "STATUS, EVENT_TYPE, OCCURRED_AT, ACTOR, REASON, SOURCE, CORRELATION_ID, PAYLOAD"),
        ])
    # Validate every identifier before making any database request.
    checks = [(label, _identifier(table.strip()), columns) for label, table, columns in checks]
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT 1 FROM DUAL")
        cursor.fetchone()
        for label, table, columns in checks:
            try:
                cursor.execute(f"SELECT {columns} FROM {table} WHERE 1=0")
            except Exception:
                raise EnvironmentCheckError(f"{label} check failed. Verify the configured table, columns and SELECT grant.") from None
    finally:
        cursor.close()
    return [label for label, _table, _columns in checks]


def main():
    if sys.version_info < (3, 10):
        print("Setup check failed: this project requires Python 3.10 or newer.", file=sys.stderr)
        return 1
    try:
        import django
        from dotenv import load_dotenv
    except ImportError:
        print("Setup check failed: install the portal requirements in the selected Python environment.", file=sys.stderr)
        return 1
    if not (5, 2) <= django.VERSION[:2] < (6, 2):
        print("Setup check failed: install the supported Django version from the project requirements.", file=sys.stderr)
        return 1
    load_dotenv(PORTAL_DIR / ".env")
    project = project_path(os.getenv("SCHEDULER_PROJECT_PATH"), PORTAL_DIR.parent / "scheduler_project")
    if not (project / "main.py").is_file():
        print("Setup check failed: SCHEDULER_PROJECT_PATH does not point to the worker folder.", file=sys.stderr)
        return 1
    load_dotenv(project / ".env")
    sys.path.insert(0, str(project))
    connection = None
    safe_errors = (EnvironmentCheckError,)
    try:
        from database.oracle_driver import load_oracle_driver, OracleDriverError
        from repositories.oracle_schedule_master_repository import connect_oracle_from_environment, OracleMasterSyncError
        safe_errors += (OracleDriverError, OracleMasterSyncError)
        driver = load_oracle_driver()
        print(f"Python {sys.version.split()[0]} | Django {django.get_version()} | Oracle driver {driver.__name__}")
        connection = connect_oracle_from_environment()
        for label in probe_oracle(connection, os.environ):
            print(f"OK: {label}")
        logging_enabled = os.getenv("SCHEDULER_ORACLE_LOGGING", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not logging_enabled:
            print("Oracle status logging is disabled in this environment; enable it after its tables are provisioned.")
        print("Connection and read checks passed. No jobs ran and no Oracle data changed. Write and EXECUTE grants are not tested.")
        return 0
    except safe_errors as error:
        # Raw driver errors can contain credentials; only sanitized errors print.
        print(f"Setup check failed: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("Setup check failed: verify the selected Oracle driver, client installation and approved connection settings.", file=sys.stderr)
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
