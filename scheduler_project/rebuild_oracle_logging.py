"""Back up and rebuild SCHEDULE_EXTG for durable Oracle operational logging.

Default is read-only inspection. --apply validates replacement data before
dropping the old table and preserves an Oracle backup plus local DDL/data.
Run with the scheduler stopped. SCHEDULE_EXTRACT_MASTER and DATEMAST are untouched.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from dotenv import load_dotenv
from repositories.oracle_schedule_master_repository import (
    _lob_value, _safe_driver_error, connect_oracle_from_environment, load_oracle_driver,
)

ROOT = Path(__file__).resolve().parent


def schema_columns(create_statement):
    return set(re.findall(r"^\s*([A-Z][A-Z0-9_]*)\s+(?:NUMBER|VARCHAR2|CLOB|TIMESTAMP|DATE)\b", create_statement, re.M))


def execute_bound(connection, sql, values, clob_names=()):
    """Use a fresh cursor for each bind shape, including optional CLOB fields."""
    cursor = connection.cursor()
    try:
        names = set(clob_names).intersection(values)
        timestamp_names = {name for name, value in values.items() if isinstance(value, datetime)}
        if names or timestamp_names:
            driver = load_oracle_driver()
            clob = getattr(driver, "DB_TYPE_CLOB", getattr(driver, "CLOB", None))
            timestamp = getattr(driver, "DB_TYPE_TIMESTAMP", getattr(driver, "TIMESTAMP", None))
            if names and clob is None or timestamp_names and timestamp is None:
                raise RuntimeError("Oracle driver does not support CLOB bindings.")
            cursor.setinputsizes(**{**{name: clob for name in names},
                                   **{name: timestamp for name in timestamp_names}})
        cursor.execute(sql, values)
    finally:
        cursor.close()


def read_rows(cursor, table, columns):
    # Identifiers originate from our schema or Oracle metadata, never a UI.
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", value) for value in (table, *columns)):
        raise RuntimeError("The old table uses identifiers requiring a dedicated migration.")
    cursor.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY ID")
    return [[_lob_value(value) for value in row] for row in cursor.fetchall()]


def ddl_statements(path):
    content = "\n".join(line for line in Path(path).read_text(encoding="utf-8").splitlines()
                        if not line.lstrip().startswith("--"))
    return [part.strip() for part in content.split(";") if part.strip()]


def inspect(cursor):
    result = {}
    for key, sql in {
        "columns": "SELECT COLUMN_NAME FROM USER_TAB_COLUMNS WHERE TABLE_NAME='SCHEDULE_EXTG' ORDER BY COLUMN_ID",
        "log_columns": "SELECT COLUMN_NAME FROM USER_TAB_COLUMNS WHERE TABLE_NAME='SCHEDULE_EXTG_LOG' ORDER BY COLUMN_ID",
        "foreign_references": "SELECT TABLE_NAME, CONSTRAINT_NAME FROM USER_CONSTRAINTS WHERE R_CONSTRAINT_NAME IN (SELECT CONSTRAINT_NAME FROM USER_CONSTRAINTS WHERE TABLE_NAME='SCHEDULE_EXTG')",
        "dependencies": "SELECT NAME, TYPE FROM USER_DEPENDENCIES WHERE REFERENCED_NAME='SCHEDULE_EXTG'",
        "triggers": "SELECT TRIGGER_NAME FROM USER_TRIGGERS WHERE TABLE_NAME='SCHEDULE_EXTG'",
        "grants": "SELECT GRANTEE, PRIVILEGE FROM USER_TAB_PRIVS_MADE WHERE TABLE_NAME='SCHEDULE_EXTG'",
    }.items():
        cursor.execute(sql)
        result[key] = cursor.fetchall()
    result["columns"] = [row[0] for row in result["columns"]]
    result["log_columns"] = [row[0] for row in result["log_columns"]]
    if result["columns"]:
        cursor.execute("SELECT COUNT(*) FROM SCHEDULE_EXTG")
        result["rows"] = int(cursor.fetchone()[0])
    else:
        result["rows"] = 0
    return result


def rebuild(*, apply=False, backup_root=None):
    connection = connect_oracle_from_environment()
    cursor = connection.cursor()
    try:
        statements = ddl_statements(ROOT / "sql" / "02_oracle_logging_schema.sql")
        current_columns, log_columns = schema_columns(statements[0]), schema_columns(statements[1])
        state = inspect(cursor)
        if {"SOURCE_ID", "RECORD_KEY", "EVENT_SEQ", "PAYLOAD"}.issubset(state["columns"]):
            if current_columns.issubset(state["columns"]) and log_columns.issubset(state["log_columns"]):
                return {"status": "already_integrated", "rows": state["rows"]}
            raise RuntimeError("Current status table already uses the logging schema but the log table is incomplete. Preserve current data and repair the missing log schema separately.")
        if any(state[key] for key in ("foreign_references", "dependencies", "triggers", "grants")):
            raise RuntimeError("External dependencies or grants require explicit migration handling before rebuilding this table.")
        if state["log_columns"]:
            raise RuntimeError("A log table already exists. Preserve its events and review migration state before rebuilding.")
        unsupported = set(state["columns"]) - current_columns
        if unsupported:
            raise RuntimeError("Preserve these existing columns in a dedicated migration before rebuilding: " + ", ".join(sorted(unsupported)))
        if not apply:
            return {"status": "preview", "inspection": state,
                    "actions": ["Back up original data and DDL", "Build and validate replacement and event log", "Drop old SCHEDULE_EXTG without PURGE", "Rename prepared replacements"]}
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = Path(backup_root or ROOT / "backups") / ("oracle-logging-" + stamp)
        backup.mkdir(parents=True, exist_ok=False)
        suffix = datetime.now().strftime("%y%m%d%H%M%S%f")
        replacement = "S_EXTG_NEW_" + suffix
        replacement_log = "S_EXTG_LNEW_" + suffix
        oracle_backup = "S_EXTG_BAK_" + suffix
        legacy = []
        if state["columns"]:
            cursor.execute("SELECT DBMS_METADATA.GET_DDL('TABLE', 'SCHEDULE_EXTG') FROM DUAL")
            backup.joinpath("original-table.sql").write_text(_lob_value(cursor.fetchone()[0]), encoding="utf-8")
            cursor.execute("SELECT * FROM SCHEDULE_EXTG ORDER BY ID")
            names = [item[0] for item in cursor.description]
            legacy = [dict(zip(names, (_lob_value(value) for value in row))) for row in cursor.fetchall()]
        backup.joinpath("original-data.json").write_text(json.dumps(legacy, default=str, indent=2) + "\n", encoding="utf-8")
        next_id = max((int(row.get("ID") or 0) for row in legacy), default=0) + 1
        create_current = statements[0].replace("CREATE TABLE SCHEDULE_EXTG (", f"CREATE TABLE {replacement} (")
        create_current = create_current.replace("AS IDENTITY PRIMARY KEY", f"AS IDENTITY (START WITH {next_id}) PRIMARY KEY")
        create_current = create_current.replace("UQ_EXTG_LOGGING_RECORD", "UQ_EL_REC_" + suffix)
        cursor.execute(create_current)
        cursor.execute(statements[1].replace("CREATE TABLE SCHEDULE_EXTG_LOG (", f"CREATE TABLE {replacement_log} (")
                                   .replace("UQ_EXTG_LOGGING_SEQUENCE", "UQ_EL_SEQ_" + suffix))
        for row in legacy:
            values = {key.lower(): value for key, value in row.items()}
            values.update(source_id="LEGACY", record_key="legacy:" + str(row["ID"]), event_seq=0,
                          payload=json.dumps(row, default=str))
            columns = list(values)
            execute_bound(connection, f"INSERT INTO {replacement} ({', '.join(columns)}) VALUES ({', '.join(':' + name for name in columns)})", values,
                          ("payload", "run_config", "error_info", "reason"))
        for index, row in enumerate(legacy, 1):
            event_id = str(uuid5(NAMESPACE_URL, f"sbi-scheduler-legacy-extg:{row['ID']}:{row.get('REPORT_DATE')}"))
            occurred = row.get("LAST_RUN") or row.get("EXECUTED_AT") or row.get("CREATED_DATE") or datetime.now()
            values = {"event_id": event_id, "event_seq": index, "record_key": "legacy:" + str(row["ID"]),
                      "name": row.get("NAME"), "report_date": row.get("REPORT_DATE"), "status": row.get("STATUS") or "PENDING",
                      "occurred_at": occurred, "payload": json.dumps(row, default=str)}
            execute_bound(connection, f"""INSERT INTO {replacement_log}
                (EVENT_ID,SOURCE_ID,EVENT_SEQ,RECORD_KEY,NAME,REPORT_DATE,STATUS,EVENT_TYPE,OCCURRED_AT,SOURCE,PAYLOAD)
                VALUES (:event_id,'LEGACY',:event_seq,:record_key,:name,:report_date,:status,'LEGACY_STATUS_IMPORTED',:occurred_at,'MIGRATION',:payload)""", values, ("payload",))
        connection.commit()
        # Compare all old fields, not just row counts, while the original is intact.
        expected = [[row[key] for key in state["columns"]] for row in legacy]
        if legacy:
            copied = read_rows(cursor, replacement, state["columns"])
            if copied != expected:
                raise RuntimeError("Replacement validation failed; the original table has not been dropped.")
        cursor.execute(f"SELECT COUNT(*) FROM {replacement_log}")
        if int(cursor.fetchone()[0]) != len(legacy):
            raise RuntimeError("Legacy event-log validation failed; the original table has not been dropped.")
        # Prepare all indexes before the destructive step. If any DDL or
        # validation fails, the original table is still available unchanged.
        for index, sql in enumerate(statements[2:], 1):
            sql = re.sub(r"CREATE INDEX [A-Z0-9_]+", f"CREATE INDEX IX_EL_{index}_{suffix}", sql)
            cursor.execute(sql.replace(" ON SCHEDULE_EXTG_LOG ", f" ON {replacement_log} ")
                              .replace(" ON SCHEDULE_EXTG ", f" ON {replacement} "))
        if state["columns"]:
            cursor.execute(f"CREATE TABLE {oracle_backup} AS SELECT * FROM SCHEDULE_EXTG")
            if read_rows(cursor, oracle_backup, state["columns"]) != expected or read_rows(cursor, "SCHEDULE_EXTG", state["columns"]) != expected:
                raise RuntimeError("Original table changed during backup; the original has not been dropped.")
        manifest = {"backup_directory": str(backup), "oracle_backup_table": oracle_backup if state["columns"] else None,
                    "replacement_table": replacement, "replacement_log_table": replacement_log,
                    "legacy_rows": len(legacy), "status": "validated_before_drop"}
        backup.joinpath("manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if state["columns"]:
            cursor.execute("DROP TABLE SCHEDULE_EXTG")
        cursor.execute(f"ALTER TABLE {replacement} RENAME TO SCHEDULE_EXTG")
        cursor.execute(f"ALTER TABLE {replacement_log} RENAME TO SCHEDULE_EXTG_LOG")
        connection.commit()
        manifest.update(status="complete", current_table="SCHEDULE_EXTG", event_table="SCHEDULE_EXTG_LOG")
        backup.joinpath("manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest
    finally:
        cursor.close()
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "backups")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    try:
        result = rebuild(apply=args.apply, backup_root=args.backup_dir)
    except RuntimeError as error:
        parser.exit(1, str(error) + "\n")
    except Exception as error:
        parser.exit(1, "Oracle logging rebuild did not complete: " + _safe_driver_error(error) + "\n")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
