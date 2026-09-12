"""Validate and reconcile a JSON schedule master with Oracle in one transaction.

Dry-run is the default. --apply creates a backup and inserts/updates by ID;
unlisted Oracle schedules, LAST_RUN and execution history are never deleted.
"""
from __future__ import annotations

import argparse
import getpass
import json
from datetime import datetime
from datetime_compat import parse_iso_datetime
from pathlib import Path

from repositories.master_configuration_repository import MasterConfigurationRepository
from repositories.oracle_schedule_master_repository import (
    OracleMasterSyncError, OracleScheduleMasterRepository, _BASE_COLUMNS,
    _lob_value, _safe_driver_error, connect_oracle_from_environment,
)

ROOT = Path(__file__).resolve().parent


def canonical_record(row, validator):
    if not isinstance(row, dict):
        raise ValueError("Every schedule must be a JSON object.")
    upper = {str(key).upper(): value for key, value in row.items()}
    if len(upper) != len(row):
        raise ValueError("Schedule fields cannot repeat with different letter case.")
    job_id = MasterConfigurationRepository._normalise_job_id(upper.get("ID"))
    if job_id > 2147483647:
        raise ValueError("Schedule IDs must be between 1 and 2147483647.")
    record = validator._normalise_record(upper)
    config = record["run_config"]
    if "MAX_ATTEMPTS" in config:
        config["MAX_ATTEMPTS"] = MasterConfigurationRepository._normalise_max_attempts(config["MAX_ATTEMPTS"])
    if config.get("RUN_BY"):
        config["RUN_BY"] = MasterConfigurationRepository._normalise_run_by(config["RUN_BY"])
    for field in ("is_active", "same_day", "time_flag", "confirmation_needed"):
        if field.upper() in upper:
            record[field] = MasterConfigurationRepository._normalise_active(upper[field.upper()])
    optional = set(validator.optional_columns)
    if "TIME_FLAG" not in optional:
        config["TIME_FLAG"] = record["time_flag"]
    if not {"CONFIRMATION", "CONFIRMATION_NEEDED"}.intersection(optional):
        config["CONFIRMATION_NEEDED"] = record["confirmation_needed"]
    if record.get("created_date"):
        # Validate timestamps before any database write. Bind a datetime below.
        parse_iso_datetime(str(record["created_date"]))
    return record


def validate_records(rows, validator):
    if not isinstance(rows, list) or not rows:
        raise ValueError("The input must be a nonempty JSON list of schedules.")
    records = [canonical_record(row, validator) for row in rows]
    ids, names = set(), set()
    for row in records:
        if row["id"] in ids or row["name"].upper() in names:
            raise ValueError("The input contains duplicate schedule IDs or names.")
        ids.add(row["id"])
        names.add(row["name"].upper())
    return records


def plan_import(incoming, existing):
    by_id = {row["id"]: row for row in existing}
    by_name = {row["name"].upper(): row["id"] for row in existing}
    plan = []
    for row in incoming:
        before = by_id.get(row["id"])
        if row["name"].upper() in by_name and by_name[row["name"].upper()] != row["id"]:
            raise ValueError(f"Schedule {row['id']} has a name already assigned to another Oracle ID.")
        if before and before["name"].upper() != row["name"].upper():
            raise ValueError(f"Schedule ID {row['id']} belongs to a different Oracle schedule. Resolve the ID through the portal before importing.")
        changes = [key for key in row if key != "created_date" and before and row[key] != before.get(key)]
        action = "insert" if before is None else "update" if changes else "unchanged"
        plan.append({"id": row["id"], "name": row["name"], "action": action, "changed_fields": changes})
    return plan


def reconcile(incoming, validator, *, apply=False, backup_dir=None, source=None, connection_factory=None):
    connection = (connection_factory or connect_oracle_from_environment)()
    cursor = None
    committed = False
    try:
        cursor = connection.cursor()
        optional = tuple(column for column in validator.optional_columns
                         if column in {"TIME_FLAG", "CONFIRMATION_NEEDED", "CONFIRMATION"})
        columns = (*_BASE_COLUMNS, *optional)
        # Lock existing definitions while applying; a concurrently inserted ID
        # remains protected by Oracle's unique constraint and rolls back the batch.
        lock = " FOR UPDATE NOWAIT" if apply else ""
        cursor.execute(f"SELECT {', '.join(columns)} FROM {validator.table_name}{lock}")
        before = [dict(zip(columns, (_lob_value(value) for value in row))) for row in cursor.fetchall()]
        existing = [canonical_record(row, validator) for row in before]
        plan = plan_import(incoming, existing)
        result = {"applied": False, "source": str(source or ""), "table": validator.table_name,
                  "actor": getpass.getuser(), "plans": plan,
                  "counts": {action: sum(row["action"] == action for row in plan)
                             for action in ("insert", "update", "unchanged")}}
        if not apply:
            return result
        if backup_dir is None:
            raise ValueError("An import backup directory is required for --apply.")
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"schedule-master-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
        backup.write_text(json.dumps({"import": result, "oracle_before": before, "incoming": incoming},
                                    default=str, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for row, operation in zip(incoming, plan):
            if operation["action"] == "unchanged":
                continue
            write_columns = list(columns) if operation["action"] == "insert" else [column for column in columns if column not in {"ID", "CREATED_DATE"}]
            values = {column.lower(): row["confirmation_needed" if column == "CONFIRMATION" else column.lower()] for column in write_columns}
            config = dict(row["run_config"])
            if "TIME_FLAG" not in optional:
                config["TIME_FLAG"] = row["time_flag"]
            if not {"CONFIRMATION", "CONFIRMATION_NEEDED"}.intersection(optional):
                config["CONFIRMATION_NEEDED"] = row["confirmation_needed"]
            values["run_config"] = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
            if operation["action"] == "insert":
                values["created_date"] = parse_iso_datetime(str(row["created_date"])) if row.get("created_date") else datetime.now()
                cursor.execute(f"INSERT INTO {validator.table_name} ({', '.join(write_columns)}) VALUES ({', '.join(':' + column.lower() for column in write_columns)})", values)
            else:
                values["job_id"] = row["id"]
                cursor.execute(f"UPDATE {validator.table_name} SET {', '.join(column + ' = :' + column.lower() for column in write_columns)} WHERE ID = :job_id", values)
            if cursor.rowcount != 1:
                raise ValueError("A schedule changed during import; the complete import was rolled back.")
        connection.commit()
        committed = True
        result.update(applied=True, backup=str(backup))
        return result
    finally:
        if not committed:
            connection.rollback()
        if cursor is not None:
            cursor.close()
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Source JSON; default is the configured SCHEDULER_DATA_DIR/file_repository/Schedule_Master.json.")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--backup-dir", type=Path, default=ROOT / "backups")
    parser.add_argument("--apply", action="store_true", help="Commit the validated batch. Default only previews changes.")
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(args.env_file.resolve())
    try:
        from config.settings import SCHEDULE_MASTER_FILE
        source = (args.source or SCHEDULE_MASTER_FILE).resolve()
        rows = json.loads(source.read_text(encoding="utf-8-sig"))
        validator = OracleScheduleMasterRepository(source)
        incoming = validate_records(rows, validator)
        result = reconcile(incoming, validator, apply=args.apply, backup_dir=args.backup_dir.resolve(), source=source)
    except (ValueError, OSError, OracleMasterSyncError) as error:
        parser.exit(1, f"Import failed: {error}\n")
    except Exception as error:
        parser.exit(1, f"Import failed: {_safe_driver_error(error)}\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
