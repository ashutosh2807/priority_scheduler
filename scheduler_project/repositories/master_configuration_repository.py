"""Bounded Schedule Master configuration changes.

The Scheduler Master is the source of truth for a job definition.  Operators
need a small, safe part of that definition to be manageable at runtime:

* whether an existing job is active; and
* the ``RUN_CONFIG.RUN_BY`` execution window; and
* the ``RUN_CONFIG.MAX_ATTEMPTS`` automatic attempt limit.

This module deliberately exposes *only* those operational fields.  It never accepts a
table name, a procedure name, a frequency, or arbitrary SQL from an API
request.  File mode updates the local demo snapshot atomically.  Oracle mode
updates the approved table through bind variables and immediately refreshes
the scheduler's read snapshot.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from config.settings import MAX_SCHEDULED_ATTEMPTS

from repositories.oracle_schedule_master_repository import (
    OracleMasterSyncError,
    OracleScheduleMasterRepository,
    _lob_value,
    _safe_driver_error,
    _safe_qualified_identifier,
    connect_oracle_from_environment,
)


_UNSET = object()


class MasterConfigurationError(ValueError):
    """A safe, operator-facing master-configuration error."""


class MasterConfigurationNotFound(LookupError):
    """The requested Scheduler Master row does not exist."""


class MasterConfigurationRepository:
    """Safely update the permitted operational fields of an existing job.

    ``schedule_master_repository`` owns the authoritative source selection.
    The configuration API cannot switch a deployment from file to Oracle mode
    and cannot select a different table at request time.
    """

    def __init__(
        self,
        schedule_master_repository,
        *,
        source: str | None = None,
        connection_factory=None,
        table_name: str | None = None,
    ) -> None:
        self.schedule_master_repository = schedule_master_repository
        self.source = str(
            source or getattr(schedule_master_repository, "source", "file")
        ).strip().lower()
        if self.source not in {"file", "oracle"}:
            raise ValueError("Scheduler Master source must be 'file' or 'oracle'.")

        configured_path = getattr(schedule_master_repository, "file_path", None)
        self.file_path = Path(configured_path) if configured_path else None
        if self.file_path is None:
            raise ValueError("Scheduler Master configuration requires a snapshot path.")

        oracle_repository = getattr(schedule_master_repository, "oracle_repository", None)
        self.oracle_repository = oracle_repository or (
            OracleScheduleMasterRepository(self.file_path)
            if self.source == "oracle"
            else None
        )
        configured_table = table_name or getattr(self.oracle_repository, "table_name", None)
        self.table_name = (
            _safe_qualified_identifier(configured_table, "Schedule Master table")
            if self.source == "oracle"
            else None
        )
        self.connection_factory = connection_factory

    def update(self, job_id, *, is_active=_UNSET, run_by=_UNSET, max_attempts=_UNSET) -> dict[str, Any]:
        """Apply a whitelisted update and return safe before/after values.

        ``run_by`` is either ``None`` (remove the time restriction) or an
        object containing exactly ``from_time`` and ``to_time``.  An overnight
        range is valid, but an equal start/end time is rejected because the
        scheduler treats it as an expired window.
        """
        job_id = self._normalise_job_id(job_id)
        if is_active is _UNSET and run_by is _UNSET and max_attempts is _UNSET:
            raise MasterConfigurationError(
                "Choose an active state, execution window, or automatic attempt limit to update."
            )

        active_value = (
            self._normalise_active(is_active) if is_active is not _UNSET else _UNSET
        )
        run_by_value = (
            self._normalise_run_by(run_by) if run_by is not _UNSET else _UNSET
        )
        attempts_value = (
            self._normalise_max_attempts(max_attempts) if max_attempts is not _UNSET else _UNSET
        )

        if self.source == "oracle":
            return self._update_oracle(job_id, active_value, run_by_value, attempts_value)
        return self._update_file(job_id, active_value, run_by_value, attempts_value)

    # ------------------------------------------------------------------
    # File-backed local/demo master
    # ------------------------------------------------------------------

    def _update_file(self, job_id: int, active_value, run_by_value, attempts_value) -> dict[str, Any]:
        try:
            with self.file_path.open("r", encoding="utf-8") as file:
                records = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise MasterConfigurationError(
                "The local Scheduler Master snapshot could not be read."
            ) from exc

        if not isinstance(records, list):
            raise MasterConfigurationError("Schedule_Master.json must contain a JSON list.")

        updated_record = None
        before = None
        for record in records:
            if not isinstance(record, dict):
                continue
            if self._record_id(record) != job_id:
                continue
            before = self._public_state(record, job_id)
            self._apply_record_update(record, active_value, run_by_value, attempts_value)
            updated_record = record
            break

        if updated_record is None:
            raise MasterConfigurationNotFound("The requested schedule does not exist in Scheduler Master.")

        self._atomic_write(self.file_path, records)
        return {
            "id": job_id,
            "source": "file",
            "before": before,
            "after": self._public_state(updated_record, job_id),
            "snapshot_refreshed": True,
        }

    # ------------------------------------------------------------------
    # Oracle-backed master
    # ------------------------------------------------------------------

    def _update_oracle(self, job_id: int, active_value, run_by_value, attempts_value) -> dict[str, Any]:
        connection = None
        cursor = None
        committed = False
        try:
            connection = (
                self.connection_factory()
                if self.connection_factory is not None
                else connect_oracle_from_environment()
            )
            cursor = connection.cursor()
            # The table identifier was validated during construction.  Values
            # are binds; this is intentionally not a general SQL interface.
            cursor.execute(
                f"SELECT ID, IS_ACTIVE, RUN_CONFIG FROM {self.table_name} "
                "WHERE ID = :job_id FOR UPDATE",
                {"job_id": job_id},
            )
            row = cursor.fetchone()
            if row is None:
                raise MasterConfigurationNotFound(
                    "The requested schedule does not exist in Scheduler Master."
                )

            current_record = {
                "ID": row[0],
                "IS_ACTIVE": _lob_value(row[1]),
                "RUN_CONFIG": _lob_value(row[2]),
            }
            before = self._public_state(current_record, job_id)
            self._apply_record_update(current_record, active_value, run_by_value, attempts_value)

            assignments = []
            parameters: dict[str, Any] = {"job_id": job_id}
            if active_value is not _UNSET:
                assignments.append("IS_ACTIVE = :is_active")
                parameters["is_active"] = current_record["IS_ACTIVE"]
            if run_by_value is not _UNSET or attempts_value is not _UNSET:
                assignments.append("RUN_CONFIG = :run_config")
                parameters["run_config"] = self._serialise_oracle_run_config(
                    current_record["RUN_CONFIG"]
                )

            cursor.execute(
                f"UPDATE {self.table_name} SET {', '.join(assignments)} WHERE ID = :job_id",
                parameters,
            )
            if getattr(cursor, "rowcount", 1) == 0:
                raise MasterConfigurationNotFound(
                    "The requested schedule no longer exists in Scheduler Master."
                )
            connection.commit()
            committed = True

            result = {
                "id": job_id,
                "source": "oracle",
                "before": before,
                "after": self._public_state(current_record, job_id),
                "snapshot_refreshed": True,
            }
            try:
                self._refresh_oracle_snapshot()
            except Exception:
                # Oracle commit has succeeded.  Report that fact accurately;
                # the regular safe refresh will retry on the next worker cycle.
                result["snapshot_refreshed"] = False
                result["warning"] = (
                    "Configuration was saved in Oracle, but the local scheduler "
                    "snapshot has not refreshed yet. The service will retry safely."
                )
            return result
        except (MasterConfigurationError, MasterConfigurationNotFound):
            raise
        except OracleMasterSyncError as exc:
            raise MasterConfigurationError(str(exc)) from None
        except Exception as exc:
            raise MasterConfigurationError(_safe_driver_error(exc)) from None
        finally:
            if connection is not None and not committed:
                try:
                    connection.rollback()
                except Exception:
                    pass
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

    def _refresh_oracle_snapshot(self) -> None:
        refresher = getattr(self.schedule_master_repository, "refresh_now", None)
        if callable(refresher):
            refresher()
            return
        if self.oracle_repository is None:
            raise MasterConfigurationError("Oracle Scheduler Master refresh is unavailable.")
        self.oracle_repository.refresh_snapshot()

    # ------------------------------------------------------------------
    # Whitelisted record manipulation
    # ------------------------------------------------------------------

    @classmethod
    def _apply_record_update(cls, record: dict[str, Any], active_value, run_by_value, attempts_value) -> None:
        if active_value is not _UNSET:
            active_key = cls._field_key(record, "IS_ACTIVE")
            record[active_key] = active_value

        if run_by_value is not _UNSET or attempts_value is not _UNSET:
            config_key = cls._field_key(record, "RUN_CONFIG")
            run_config, was_text = cls._parse_run_config(record.get(config_key))
            # Do not permit a second case-variant of RUN_BY to survive.  All
            # unrelated configuration fields (frequencies, holidays, etc.) are
            # copied through untouched.
            if run_by_value is not _UNSET:
                for key in list(run_config):
                    if str(key).upper() == "RUN_BY":
                        del run_config[key]
                if run_by_value is not None:
                    run_config["RUN_BY"] = dict(run_by_value)
            if attempts_value is not _UNSET:
                for key in list(run_config):
                    if str(key).upper() == "MAX_ATTEMPTS":
                        del run_config[key]
                run_config["MAX_ATTEMPTS"] = attempts_value
            record[config_key] = (
                json.dumps(run_config, separators=(",", ":"), ensure_ascii=False)
                if was_text
                else run_config
            )

    @staticmethod
    def _field_key(record: dict[str, Any], canonical: str) -> str:
        for key in record:
            if str(key).upper() == canonical:
                return key
        return canonical

    @classmethod
    def _public_state(cls, record: dict[str, Any], job_id: int) -> dict[str, Any]:
        active_key = cls._field_key(record, "IS_ACTIVE")
        config_key = cls._field_key(record, "RUN_CONFIG")
        run_config, _ = cls._parse_run_config(record.get(config_key))
        run_by = next(
            (value for key, value in run_config.items() if str(key).upper() == "RUN_BY"),
            None,
        )
        if isinstance(run_by, dict):
            from_time = cls._mapping_value(run_by, "FROM_TIME")
            to_time = cls._mapping_value(run_by, "TO_TIME")
            public_run_by = (
                {"from_time": str(from_time), "to_time": str(to_time)}
                if from_time is not None and to_time is not None
                else None
            )
        else:
            public_run_by = None
        return {
            "id": job_id,
            "is_active": cls._active_as_bool(record.get(active_key)),
            "run_by": public_run_by,
            "max_attempts": next(
                (value for key, value in run_config.items() if str(key).upper() == "MAX_ATTEMPTS"),
                MAX_SCHEDULED_ATTEMPTS,
            ),
        }

    @staticmethod
    def _parse_run_config(value: Any) -> tuple[dict[str, Any], bool]:
        value = _lob_value(value)
        if value is None or value == "":
            return {}, False
        if isinstance(value, dict):
            return copy.deepcopy(value), False
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise MasterConfigurationError("The existing RUN_CONFIG is not valid JSON.") from exc
            if not isinstance(parsed, dict):
                raise MasterConfigurationError("The existing RUN_CONFIG must be a JSON object.")
            return parsed, True
        raise MasterConfigurationError("The existing RUN_CONFIG must be an object or JSON object.")

    @staticmethod
    def _serialise_oracle_run_config(value: Any) -> str:
        config, _ = MasterConfigurationRepository._parse_run_config(value)
        return json.dumps(config, separators=(",", ":"), ensure_ascii=False)

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_job_id(value) -> int:
        if isinstance(value, bool):
            raise MasterConfigurationError("Job ID must be a positive integer.")
        try:
            job_id = int(str(value).strip())
        except (TypeError, ValueError):
            raise MasterConfigurationError("Job ID must be a positive integer.") from None
        if job_id < 1:
            raise MasterConfigurationError("Job ID must be a positive integer.")
        return job_id

    @staticmethod
    def _normalise_active(value) -> int:
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, int) and value in {0, 1}:
            return value
        if isinstance(value, str):
            normalised = value.strip().upper()
            if normalised in {"1", "TRUE", "ACTIVE"}:
                return 1
            if normalised in {"0", "FALSE", "INACTIVE"}:
                return 0
        raise MasterConfigurationError("is_active must be true or false.")

    @classmethod
    def _normalise_run_by(cls, value) -> dict[str, str] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise MasterConfigurationError("run_by must be an object with from_time and to_time.")

        allowed = {"from_time", "to_time", "FROM_TIME", "TO_TIME"}
        if any(key not in allowed for key in value):
            raise MasterConfigurationError("run_by permits only from_time and to_time.")
        from_value = cls._mapping_value(value, "FROM_TIME")
        to_value = cls._mapping_value(value, "TO_TIME")
        if from_value is None or to_value is None:
            raise MasterConfigurationError("run_by requires both from_time and to_time.")
        from_time = cls._normalise_time(from_value)
        to_time = cls._normalise_time(to_value)
        if from_time == to_time:
            raise MasterConfigurationError("run_by from_time and to_time must be different.")
        return {"FROM_TIME": from_time, "TO_TIME": to_time}

    @staticmethod
    def _mapping_value(mapping: dict[str, Any], canonical: str):
        values = [value for key, value in mapping.items() if str(key).upper() == canonical]
        if len(values) > 1:
            raise MasterConfigurationError(f"run_by specifies {canonical.lower()} more than once.")
        return values[0] if values else None

    @staticmethod
    def _normalise_time(value) -> str:
        if not isinstance(value, str):
            raise MasterConfigurationError("Execution times must use 24-hour HH:MM format.")
        try:
            return datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")
        except ValueError:
            raise MasterConfigurationError("Execution times must use 24-hour HH:MM format.") from None

    @staticmethod
    def _active_as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().upper() in {"1", "Y", "YES", "TRUE", "T", "ACTIVE"}
        return bool(value)

    @staticmethod
    def _record_id(record: dict[str, Any]) -> int | None:
        value = next(
            (candidate for key, candidate in record.items() if str(key).upper() == "ID"),
            None,
        )
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _atomic_write(path: Path, payload: list[dict[str, Any]]) -> None:
        temporary_name = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
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
        except OSError as exc:
            raise MasterConfigurationError("The local Scheduler Master snapshot could not be saved.") from exc
        finally:
            if temporary_name:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass
    @staticmethod
    def _normalise_max_attempts(value) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            raise MasterConfigurationError("max_attempts must be a whole number between 1 and 100, including the first run.")
        return value
