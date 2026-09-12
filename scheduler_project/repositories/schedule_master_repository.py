import json
import logging
import os
import time
from pathlib import Path

from config.settings import SCHEDULE_MASTER_FILE
from models.schedule_master import ScheduleMaster
from repositories.oracle_schedule_master_repository import (
    OracleMasterSyncError,
    OracleScheduleMasterRepository,
)


logger = logging.getLogger("scheduler.schedule_master")


class ScheduleMasterRepository:
    """
    Repository for reading Schedule Master data.

    The normal contract is the local JSON snapshot.  Setting
    ``SCHEDULER_MASTER_SOURCE=oracle`` refreshes that snapshot from Oracle
    before it is consumed, while retaining a last-known-good snapshot if the
    approved configuration allows stale fallback.
    """

    def __init__(
        self,
        file_path=None,
        *,
        source=None,
        oracle_repository=None,
        refresh_seconds=None,
        allow_stale_snapshot=None,
    ):
        self.file_path = Path(file_path or SCHEDULE_MASTER_FILE)
        self.source = str(source or os.getenv("SCHEDULER_MASTER_SOURCE", "file")).strip().lower()
        if self.source not in {"file", "oracle"}:
            raise ValueError("SCHEDULER_MASTER_SOURCE must be 'file' or 'oracle'.")

        self.refresh_seconds = self._positive_int(
            refresh_seconds if refresh_seconds is not None else os.getenv("SCHEDULER_MASTER_REFRESH_SECONDS", "3600"),
            default=3600,
        )
        self.allow_stale_snapshot = (
            self._env_flag("SCHEDULER_MASTER_ALLOW_STALE_SNAPSHOT", True)
            if allow_stale_snapshot is None
            else bool(allow_stale_snapshot)
        )
        self.oracle_repository = oracle_repository or (
            OracleScheduleMasterRepository(self.file_path) if self.source == "oracle" else None
        )
        self._cached_records = None
        self._next_oracle_refresh = 0.0
        self.last_refresh_error = ""

    def get_all(self):
        """
        Load all Schedule Master jobs.

        Returns:
            list[ScheduleMaster]
        """

        data = self._get_records()

        jobs = []

        for item in data:

            if not isinstance(item, dict):
                continue

            job = ScheduleMaster(
                id=item.get("ID", item.get("id")),
                name=item.get("NAME", item.get("name")),
                package_name=item.get(
                    "PACKAGE_NAME",
                    item.get("package_name")
                ),
                run_config=self._parse_run_config(
                    item.get(
                        "RUN_CONFIG",
                        item.get("run_config")
                    )
                ),
                margin=item.get(
                    "MARGIN",
                    item.get("margin")
                ),
                same_day=item.get(
                    "SAME_DAY",
                    item.get("same_day", 0)
                ),
                time_flag=item.get(
                    "TIME_FLAG",
                    item.get("time_flag", 0)
                ),
                is_active=item.get(
                    "IS_ACTIVE",
                    item.get("is_active", 1)
                ),
                created_date=item.get(
                    "CREATED_DATE",
                    item.get("created_date")
                ),
                confirmation_needed=item.get(
                    "CONFIRMATION_NEEDED",
                    item.get("confirmation_needed")
                ),
            )

            jobs.append(job)

        return jobs

    def refresh_now(self):
        """Force an Oracle refresh; useful to an approved batch/admin task."""
        if self.source != "oracle":
            raise ValueError("Schedule Master refresh requires SCHEDULER_MASTER_SOURCE=oracle.")
        records = self.oracle_repository.refresh_snapshot()
        self._cached_records = records
        self._next_oracle_refresh = time.monotonic() + self.refresh_seconds
        self.last_refresh_error = ""
        return records

    def get_by_id(self, job_id):
        """
        Return a ScheduleMaster by ID.

        Returns:
            ScheduleMaster | None
        """

        jobs = self.get_all()

        for job in jobs:
            if job.id == job_id:
                return job

        return None

    def get_active(self):
        """
        Return only active Schedule Master jobs.
        """

        return [
            job
            for job in self.get_all()
            if self._is_active(job.is_active)
        ]

    @staticmethod
    def _is_active(value):
        if isinstance(value, str):
            return value.strip().upper() in {"1", "Y", "YES", "TRUE", "ACTIVE"}
        return bool(value)

    @staticmethod
    def _parse_run_config(run_config):
        """
        Convert RUN_CONFIG into a Python dictionary.

        RUN_CONFIG may already be a dictionary, or it may
        be stored as a JSON string.
        """

        if run_config is None:
            return {}

        if isinstance(run_config, dict):
            return run_config

        if isinstance(run_config, str):

            run_config = run_config.strip()

            if not run_config:
                return {}

            try:
                return json.loads(run_config)

            except json.JSONDecodeError as exc:
                raise ValueError(
                    "Invalid RUN_CONFIG JSON."
                ) from exc

        raise TypeError(
            "RUN_CONFIG must be a dictionary, JSON string, or None."
        )

    def _get_records(self):
        if self.source == "file":
            return self._read_snapshot_file()

        now = time.monotonic()
        if self._cached_records is not None and now < self._next_oracle_refresh:
            return self._cached_records

        try:
            return self.refresh_now()
        except OracleMasterSyncError as exc:
            # The synchroniser has already removed credentials and connect
            # details from its error. Keep a concise status for callers but do
            # not interrupt active work merely because one refresh failed.
            self.last_refresh_error = str(exc)
            logger.error("Schedule Master Oracle refresh failed; retaining last known valid snapshot.")
            if self._cached_records is not None:
                self._next_oracle_refresh = now + self.refresh_seconds
                return self._cached_records
            if self.allow_stale_snapshot:
                records = self._read_snapshot_file()
                self._cached_records = records
                self._next_oracle_refresh = now + self.refresh_seconds
                return records
            raise

    def _read_snapshot_file(self):
        with self.file_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, list):
            raise ValueError("Schedule_Master.json must contain a JSON list.")
        return data

    @staticmethod
    def _env_flag(name, default):
        return os.getenv(name, "1" if default else "0").strip().lower() in {
            "1", "true", "yes", "on",
        }

    @staticmethod
    def _positive_int(value, default):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default
