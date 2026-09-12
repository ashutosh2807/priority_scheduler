"""Validated schedule definition CRUD against the configured master source.

The web application submits data to the scheduler API. Only this repository
writes master definitions; Oracle table names come from deployment settings
and every record value is bound. Execution and report history are retained.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime

from repositories.master_configuration_repository import (
    MasterConfigurationError, MasterConfigurationNotFound,
    MasterConfigurationRepository,
)
from repositories.oracle_schedule_master_repository import (
    OracleMasterSyncError, OracleScheduleMasterRepository,
    _BASE_COLUMNS, _lob_value, _safe_driver_error,
    connect_oracle_from_environment,
)


class ScheduleDefinitionRepository(MasterConfigurationRepository):
    FIELDS = {
        "id", "name", "package_name", "run_config", "margin", "same_day",
        "time_flag", "is_active", "confirmation_needed",
    }

    @staticmethod
    def _normalise_job_id(value):
        job_id = MasterConfigurationRepository._normalise_job_id(value)
        if job_id > 2147483647:
            raise MasterConfigurationError("Schedule ID must be between 1 and 2147483647.")
        return job_id

    def create(self, payload):
        changes = self._validate_payload(payload, creating=True)
        job_id = self._normalise_job_id(changes["id"])
        return self._mutate("create", job_id, changes)

    def update_definition(self, job_id, payload):
        changes = self._validate_payload(payload)
        return self._mutate("update", self._normalise_job_id(job_id), changes)

    def delete_definition(self, job_id):
        return self._mutate("delete", self._normalise_job_id(job_id), {})

    @classmethod
    def _validate_payload(cls, payload, creating=False):
        if not isinstance(payload, dict):
            raise MasterConfigurationError("Schedule definition must be a JSON object.")
        allowed = cls.FIELDS if creating else cls.FIELDS - {"id"}
        if set(payload) - allowed:
            raise MasterConfigurationError("The schedule contains unsupported fields; its ID cannot be changed.")
        required = {"id", "name", "package_name", "run_config"} if creating else set()
        if required - set(payload):
            raise MasterConfigurationError("Provide the schedule ID, name, Oracle procedure and frequency configuration.")
        if not payload:
            raise MasterConfigurationError("Choose schedule fields to change.")
        changes = copy.deepcopy(payload)
        for field in ("name", "package_name"):
            if field in changes:
                value = changes[field]
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > (128 if field == "name" else 386):
                    raise MasterConfigurationError(f"Provide a valid {field.replace('_', ' ')}.")
                changes[field] = value.strip()
        for field in ("same_day", "time_flag", "is_active", "confirmation_needed"):
            if field in changes:
                changes[field] = cls._normalise_active(changes[field])
        if "run_config" in changes:
            if not isinstance(changes["run_config"], dict):
                raise MasterConfigurationError("run_config must be a JSON object.")
            try:
                json.dumps(changes["run_config"], allow_nan=False)
            except (TypeError, ValueError):
                raise MasterConfigurationError("run_config must contain valid JSON values.") from None
            keys = [str(key).upper() for key in changes["run_config"]]
            if len(set(keys)) != len(keys):
                raise MasterConfigurationError("Configuration fields must not be repeated with different letter case.")
            changes["run_config"] = {str(key).upper(): value for key, value in changes["run_config"].items()}
        return changes

    def _definition(self, job_id, changes, before=None):
        record = {str(key).lower(): _lob_value(value) for key, value in (before or {}).items()}
        defaults = {
            "id": job_id, "is_active": 1, "same_day": 0, "margin": "T+1",
            "created_date": datetime.now().isoformat(timespec="seconds"),
        }
        record = {**defaults, **record}
        config, _ = self._parse_run_config(record.get("run_config"))
        config = {str(key).upper(): value for key, value in config.items()}
        for key, value in changes.get("run_config", {}).items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        record.update({key: value for key, value in changes.items() if key != "run_config"})
        record["id"] = job_id
        if "margin" in changes:
            config["MARGIN"] = changes["margin"]
        if "time_flag" in changes:
            config["TIME_FLAG"] = changes["time_flag"]
        elif "RUN_BY" in changes.get("run_config", {}):
            config["TIME_FLAG"] = int(bool(config.get("RUN_BY")))
            record["time_flag"] = config["TIME_FLAG"]
        if "confirmation_needed" in changes:
            config["CONFIRMATION_NEEDED"] = changes["confirmation_needed"]
        if "MAX_ATTEMPTS" in config:
            config["MAX_ATTEMPTS"] = self._normalise_max_attempts(config["MAX_ATTEMPTS"])
        if config.get("RUN_BY") is not None:
            config["RUN_BY"] = self._normalise_run_by(config["RUN_BY"])
        record["run_config"] = config
        validator = OracleScheduleMasterRepository(self.file_path)
        try:
            canonical = validator._normalise_record({key.upper(): value for key, value in record.items()})
        except OracleMasterSyncError as error:
            raise MasterConfigurationError(str(error)) from None
        canonical["run_config"]["CONFIRMATION_NEEDED"] = canonical["confirmation_needed"]
        canonical["run_config"]["TIME_FLAG"] = canonical["time_flag"]
        return canonical

    def _mutate(self, operation, job_id, changes):
        if self.source == "oracle":
            return self._mutate_oracle(operation, job_id, changes)
        try:
            records = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise MasterConfigurationError("The local Scheduler Master snapshot could not be read.") from None
        if not isinstance(records, list):
            raise MasterConfigurationError("Schedule_Master.json must contain a JSON list.")
        before = next((row for row in records if isinstance(row, dict) and self._record_id(row) == job_id), None)
        self._assert_exists(operation, before)
        after = self._definition(job_id, changes, before) if operation != "delete" else None
        if after is not None:
            for row in records:
                if not isinstance(row, dict) or self._record_id(row) == job_id:
                    continue
                if str(row.get(self._field_key(row, "NAME"), "")).strip().upper() == after["name"].upper():
                    raise MasterConfigurationError("Another schedule already uses this name.")
        saved = [row for row in records if not isinstance(row, dict) or self._record_id(row) != job_id]
        if after is not None:
            saved.append(after)
        self._atomic_write(self.file_path, saved)
        return self._result(job_id, before, after)

    @staticmethod
    def _assert_exists(operation, before):
        if operation == "create" and before is not None:
            raise MasterConfigurationError("This schedule ID is already in use.")
        if operation != "create" and before is None:
            raise MasterConfigurationNotFound("The requested schedule does not exist in Scheduler Master.")

    def _mutate_oracle(self, operation, job_id, changes):
        connection = cursor = None
        committed = False
        try:
            connection = self.connection_factory() if self.connection_factory else connect_oracle_from_environment()
            cursor = connection.cursor()
            optional = tuple(column for column in getattr(self.oracle_repository, "optional_columns", ())
                             if column.upper() in {"TIME_FLAG", "CONFIRMATION_NEEDED", "CONFIRMATION"})
            columns = (*_BASE_COLUMNS, *optional)
            cursor.execute(f"SELECT {', '.join(columns)} FROM {self.table_name} WHERE ID = :job_id FOR UPDATE", {"job_id": job_id})
            row = cursor.fetchone()
            before = dict(zip(columns, [_lob_value(value) for value in row])) if row is not None else None
            self._assert_exists(operation, before)
            after = self._definition(job_id, changes, before) if operation != "delete" else None
            if after is not None:
                cursor.execute(f"SELECT ID FROM {self.table_name} WHERE UPPER(NAME) = :name AND ID <> :job_id", {"name": after["name"].upper(), "job_id": job_id})
                if cursor.fetchone() is not None:
                    raise MasterConfigurationError("Another schedule already uses this name.")
            if operation == "delete":
                cursor.execute(f"DELETE FROM {self.table_name} WHERE ID = :job_id", {"job_id": job_id})
            else:
                write_columns = list(columns) if operation == "create" else [column for column in columns if column not in {"ID", "CREATED_DATE"}]
                values = {column.lower(): after["confirmation_needed" if column.upper() == "CONFIRMATION" else column.lower()] for column in write_columns}
                values["run_config"] = json.dumps(after["run_config"], separators=(",", ":"), ensure_ascii=False)
                if operation == "create":
                    values["created_date"] = datetime.now()
                    cursor.execute(f"INSERT INTO {self.table_name} ({', '.join(write_columns)}) VALUES ({', '.join(':' + column.lower() for column in write_columns)})", values)
                else:
                    values["job_id"] = job_id
                    assignments = ", ".join(column + " = :" + column.lower() for column in write_columns)
                    cursor.execute(f"UPDATE {self.table_name} SET {assignments} WHERE ID = :job_id", values)
            if getattr(cursor, "rowcount", 1) == 0:
                raise MasterConfigurationNotFound("The schedule changed before it could be saved; refresh and try again.")
            connection.commit()
            committed = True
            result = self._result(job_id, before, after)
            try:
                self._refresh_oracle_snapshot()
            except Exception:
                # Never report the committed master change as a failure or
                # encourage a second create. Apply it to the in-memory read
                # cache where available so old queued definitions cannot run.
                source = self.schedule_master_repository
                cached = getattr(source, "_cached_records", None)
                if cached is not None:
                    source._cached_records = [record for record in cached if self._record_id(record) != job_id]
                    if after is not None:
                        source._cached_records.append(after)
                result["snapshot_refreshed"] = False
                result["warning"] = "The schedule was saved in Oracle. Its local snapshot refresh will be retried by the service."
            return result
        except (MasterConfigurationError, MasterConfigurationNotFound):
            raise
        except OracleMasterSyncError as error:
            raise MasterConfigurationError(str(error)) from None
        except Exception as error:
            raise MasterConfigurationError(_safe_driver_error(error)) from None
        finally:
            if connection is not None and not committed:
                try:
                    connection.rollback()
                except Exception:
                    pass
            for resource in (cursor, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass

    def _result(self, job_id, before, after):
        return {"id": job_id, "source": self.source, "before": before,
                "after": after, "snapshot_refreshed": True}
