# repositories/schedule_extg_repository.py

import json
import logging
import os
import tempfile
from datetime import date, datetime

from repositories.oracle_schedule_extg_mirror import OracleScheduleExtgMirror


logger = logging.getLogger("scheduler.schedule_extg")


class ScheduleExtgRepository:
    """
    File-based repository for Schedule_extg.json.

    Schedule_extg represents the execution-facing status of an
    extract occurrence.

    Lifecycle:

        PENDING
           |
           v
        RUNNING
         /   \
        v     v
    SUCCESS  FAILED


    Identity rules
    --------------

    Scheduled execution:
        NAME + REPORT_DATE

    Manual execution:
        NAME + REPORT_DATE=None

    Scheduled occurrences are deduplicated by NAME + REPORT_DATE.

    Manual executions are NOT deduplicated. Every manual execution
    gets a separate record.


    Example record
    --------------

    {
        "id": 1,
        "report_date": "2026-09-07",
        "name": "GPB",
        "status": "SUCCESS",
        "same_day": 0,
        "run_date": "2026-09-10T17:15:00",
        "last_run": "2026-09-10T17:16:12",
        "time_flag": 1,
        "run_config": {},
        "error_info": null,
        "confirmation": 0,
        "count": 123
    }


    The repository is intentionally independent of SQLite.

    SQLite execution_jobs:
        detailed execution history

    Schedule_extg.json:
        execution-facing current status / monitoring representation
    """

    STATUS_PENDING = "PENDING"
    STATUS_RUNNING = "RUNNING"
    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILED = "FAILED"

    VALID_STATUSES = {
        STATUS_PENDING,
        STATUS_RUNNING,
        STATUS_SUCCESS,
        STATUS_FAILED,
    }

    def __init__(
        self,
        file_path,
        *,
        oracle_mirror=None,
    ):
        if file_path is None:
            raise ValueError(
                "file_path is required."
            )

        self.file_path = os.fspath(
            file_path
        )
        # Oracle mirroring is optional and deliberately lives behind this
        # repository boundary. JSON/SQLite state is committed first; a mirror
        # outage cannot interrupt a running scheduler or procedure call.
        self.oracle_mirror = (
            oracle_mirror
            if oracle_mirror is not None
            else OracleScheduleExtgMirror()
        )

        self._ensure_file()

    # ========================================================================
    # BASIC FILE OPERATIONS
    # ========================================================================

    def _ensure_file(self):
        """
        Create an empty JSON array if the file does not exist.

        Existing files are never overwritten here.
        """

        directory = os.path.dirname(
            os.path.abspath(
                self.file_path
            )
        )

        if directory:
            os.makedirs(
                directory,
                exist_ok=True,
            )

        if not os.path.exists(
            self.file_path
        ):

            self._write_records(
                []
            )

    def _read_records(self):
        """
        Read Schedule_extg.json.

        Supported structures:

            []
            {"data": []}
            {"records": []}
            {"items": []}

        A single JSON object is also accepted and converted to
        a one-record list.

        Invalid JSON raises ValueError rather than silently deleting
        execution history.
        """

        if not os.path.exists(
            self.file_path
        ):
            return []

        with open(
            self.file_path,
            "r",
            encoding="utf-8",
        ) as file:

            content = file.read().strip()

        if not content:
            return []

        try:

            data = json.loads(
                content
            )

        except json.JSONDecodeError as exc:

            raise ValueError(
                "Schedule_extg.json contains invalid JSON: "
                f"{exc}"
            ) from exc

        if isinstance(
            data,
            list,
        ):

            return [
                dict(record)
                for record in data
                if isinstance(
                    record,
                    dict,
                )
            ]

        if isinstance(
            data,
            dict,
        ):

            for key in (
                "data",
                "records",
                "items",
                "schedule_extg",
                "SCHEDULE_EXTG",
            ):

                value = data.get(
                    key
                )

                if isinstance(
                    value,
                    list,
                ):

                    return [
                        dict(record)
                        for record in value
                        if isinstance(
                            record,
                            dict,
                        )
                    ]

            # A single Schedule_extg record.
            return [
                dict(data)
            ]

        raise ValueError(
            "Schedule_extg.json must contain "
            "a JSON array or object."
        )

    def _write_records(
        self,
        records,
    ):
        """
        Atomically write records to JSON.

        Write to a temporary file first, then replace the original.
        """

        directory = os.path.dirname(
            os.path.abspath(
                self.file_path
            )
        )

        os.makedirs(
            directory,
            exist_ok=True,
        )

        fd, temp_path = tempfile.mkstemp(
            prefix=".schedule_extg_",
            suffix=".tmp",
            dir=directory,
            text=True,
        )

        try:

            with os.fdopen(
                fd,
                "w",
                encoding="utf-8",
            ) as file:

                json.dump(
                    records,
                    file,
                    indent=4,
                    ensure_ascii=False,
                )

                file.write(
                    "\n"
                )

            os.replace(
                temp_path,
                self.file_path,
            )

        except Exception:

            try:

                os.unlink(
                    temp_path
                )

            except OSError:
                pass

            raise

    # ========================================================================
    # GET ALL
    # ========================================================================

    def get_all(self):
        """
        Return all Schedule_extg records.
        """

        return self._read_records()

    # ========================================================================
    # GET BY ID
    # ========================================================================

    def get_by_id(
        self,
        record_id,
    ):
        """
        Return one Schedule_extg record by ID.
        """

        records = self._read_records()

        for record in records:

            if self._same_id(
                record.get("id"),
                record_id,
            ):

                return record

        return None

    # ========================================================================
    # GET BY NAME + REPORT DATE
    # ========================================================================

    def get_by_name_report_date(
        self,
        name,
        report_date,
    ):
        """
        Find a scheduled/manual execution record by:

            name
            report_date

        For scheduled records:

            name + report_date

        For manual records:

            name + None

        Note that this method returns the first matching record.
        """

        name = self._normalize_name(
            name
        )

        report_date = self._date_to_string(
            report_date
        )

        records = self._read_records()

        # Search newest records first.
        records = list(
            reversed(records)
        )

        for record in records:

            record_name = self._normalize_name(
                record.get("name")
            )

            record_report_date = (
                self._date_to_string(
                    record.get(
                        "report_date"
                    )
                )
            )

            if (
                record_name == name
                and record_report_date == report_date
            ):

                return record

        return None

    # ========================================================================
    # GET BY STATUS
    # ========================================================================

    def get_by_status(
        self,
        status,
    ):
        """
        Return records having the requested status.
        """

        status = self._normalize_status(
            status
        )

        records = self._read_records()

        return [
            record
            for record in records
            if self._normalize_status(
                record.get("status")
            ) == status
        ]

    # ========================================================================
    # PENDING
    # ========================================================================

    def get_pending(self):
        return self.get_by_status(
            self.STATUS_PENDING
        )

    # ========================================================================
    # RUNNING
    # ========================================================================

    def get_running(self):
        return self.get_by_status(
            self.STATUS_RUNNING
        )

    # ========================================================================
    # SUCCESS
    # ========================================================================

    def get_successful(self):
        return self.get_by_status(
            self.STATUS_SUCCESS
        )

    # Alias.
    get_success = get_successful

    # ========================================================================
    # FAILED
    # ========================================================================

    def get_failed(self):
        return self.get_by_status(
            self.STATUS_FAILED
        )

    # ========================================================================
    # CREATE PENDING
    # ========================================================================

    def create_pending(
        self,
        name,
        report_date=None,
        same_day=0,
        time_flag=0,
        run_config=None,
        confirmation=0,
    ):
        """
        Create a PENDING Schedule_extg record.

        Scheduled execution:
            Existing NAME + REPORT_DATE is reused.

        Manual execution:
            REPORT_DATE=None always creates a new record.

        Returns
        -------
        dict
            The created or existing record.
        """

        name = self._normalize_name(
            name
        )

        if not name:
            raise ValueError(
                "name is required."
            )

        report_date = self._date_to_string(
            report_date
        )

        # --------------------------------------------------------------------
        # Scheduled occurrence.
        #
        # Do not create duplicate Schedule_extg rows for the same
        # NAME + REPORT_DATE.
        # --------------------------------------------------------------------

        if report_date is not None:

            existing = (
                self.get_by_name_report_date(
                    name,
                    report_date,
                )
            )

            if existing is not None:

                # If the previous record is FAILED, a new execution
                # attempt should update the same scheduled occurrence
                # back to PENDING.
                #
                # This keeps Schedule_extg focused on occurrence status,
                # while execution_jobs keeps individual attempts.

                if self._normalize_status(
                    existing.get("status")
                ) == self.STATUS_FAILED:

                    existing = self._update_existing_record(
                        existing,
                        status=self.STATUS_PENDING,
                        run_date=self._now(),
                        last_run=None,
                        error_info=None,
                    )
                else:
                    self._mirror_record(existing)
                return existing

        now = self._now()

        records = self._read_records()

        record = {
            "id": self._next_id(
                records
            ),
            "report_date": report_date,
            "name": name,
            "status": self.STATUS_PENDING,
            "same_day": self._to_int(
                same_day
            ),
            "run_date": now,
            "last_run": None,
            "time_flag": self._to_int(
                time_flag
            ),
            "run_config": (
                self._copy_run_config(
                    run_config
                )
            ),
            "error_info": None,
            "confirmation": self._to_int(
                confirmation
            ),
            "count": None,
        }

        records.append(
            record
        )

        self._write_records(
            records
        )

        self._mirror_record(record)

        return record

    # ========================================================================
    # MARK RUNNING
    # ========================================================================

    def mark_running(
        self,
        record_id=None,
        name=None,
        report_date=None,
    ):
        """
        Mark a Schedule_extg record RUNNING.

        Preferred usage:

            mark_running(record_id)

        Compatibility usage:

            mark_running(
                name="GPB",
                report_date="2026-09-07",
            )
        """

        record = self._resolve_record(
            record_id=record_id,
            name=name,
            report_date=report_date,
        )

        if record is None:
            raise ValueError(
                "Schedule_extg record could not be found."
            )

        return self._update_existing_record(
            record,
            status=self.STATUS_RUNNING,
            run_date=self._now(),
            error_info=None,
        )

    # ========================================================================
    # MARK SUCCESS
    # ========================================================================

    def mark_success(
        self,
        record_id=None,
        name=None,
        report_date=None,
        count=None,
    ):
        """
        Mark a Schedule_extg record SUCCESS.
        """

        record = self._resolve_record(
            record_id=record_id,
            name=name,
            report_date=report_date,
        )

        if record is None:
            raise ValueError(
                "Schedule_extg record could not be found."
            )

        now = self._now()

        return self._update_existing_record(
            record,
            status=self.STATUS_SUCCESS,
            last_run=now,
            error_info=None,
            count=count,
        )

    # ========================================================================
    # MARK FAILED
    # ========================================================================

    def mark_failed(
        self,
        record_id=None,
        name=None,
        report_date=None,
        error=None,
        error_type=None,
    ):
        """
        Mark a Schedule_extg record FAILED.
        """

        record = self._resolve_record(
            record_id=record_id,
            name=name,
            report_date=report_date,
        )

        if record is None:
            raise ValueError(
                "Schedule_extg record could not be found."
            )

        error_info = self._build_error_info(
            error=error,
            error_type=error_type,
        )

        return self._update_existing_record(
            record,
            status=self.STATUS_FAILED,
            last_run=self._now(),
            error_info=error_info,
        )

    # ========================================================================
    # GENERIC UPDATE
    # ========================================================================

    def update(
        self,
        record,
        **changes,
    ):
        """
        Generic record update.

        Example:

            repository.update(
                record,
                status="RUNNING",
            )
        """

        if record is None:
            raise ValueError(
                "record is required."
            )

        record_id = self._record_id(
            record
        )

        if record_id is None:

            record_id = record.get(
                "id"
            ) if isinstance(
                record,
                dict,
            ) else None

        if record_id is None:

            raise ValueError(
                "Schedule_extg record has no ID."
            )

        return self._update_by_id(
            record_id,
            changes,
        )

    # ========================================================================
    # UPDATE METADATA
    # ========================================================================

    def update_metadata(
        self,
        record_id,
        **changes,
    ):
        """
        Update arbitrary Schedule_extg metadata.
        """

        return self._update_by_id(
            record_id,
            changes,
        )

    # ========================================================================
    # DELETE
    # ========================================================================

    def delete(
        self,
        record_id,
    ):
        """
        Delete one Schedule_extg record.

        Returns
        -------
        bool
            True if deleted.
        """

        records = self._read_records()

        new_records = []

        deleted = False

        for record in records:

            if self._same_id(
                record.get("id"),
                record_id,
            ):

                deleted = True

                continue

            new_records.append(
                record
            )

        if deleted:

            self._write_records(
                new_records
            )

        return deleted

    # ========================================================================
    # COUNT
    # ========================================================================

    def count(
        self,
        status=None,
    ):
        """
        Return number of Schedule_extg records.

        If status is provided, count only that status.
        """

        if status is None:

            return len(
                self._read_records()
            )

        return len(
            self.get_by_status(
                status
            )
        )

    # ========================================================================
    # CLEAR
    # ========================================================================

    def clear(self):
        """
        Remove all Schedule_extg records.

        This should normally only be used for development/testing.
        """

        self._write_records(
            []
        )

    # ========================================================================
    # INTERNAL RESOLUTION
    # ========================================================================

    def _resolve_record(
        self,
        record_id=None,
        name=None,
        report_date=None,
    ):
        """
        Resolve a record either by ID or NAME + REPORT_DATE.
        """

        if record_id is not None:

            return self.get_by_id(
                record_id
            )

        if name is not None:

            return self.get_by_name_report_date(
                name,
                report_date,
            )

        return None

    # ========================================================================
    # INTERNAL UPDATE
    # ========================================================================

    def _update_by_id(
        self,
        record_id,
        changes,
    ):
        """
        Update one record by ID.
        """

        records = self._read_records()

        updated_record = None

        for index, record in enumerate(
            records
        ):

            if not self._same_id(
                record.get("id"),
                record_id,
            ):
                continue

            updated = dict(
                record
            )

            for key, value in changes.items():

                if key == "status":

                    value = self._normalize_status(
                        value
                    )

                elif key == "report_date":

                    value = self._date_to_string(
                        value
                    )

                elif key in {
                    "same_day",
                    "time_flag",
                    "confirmation",
                }:

                    value = self._to_int(
                        value
                    )

                elif key == "run_config":

                    value = self._copy_run_config(
                        value
                    )

                updated[key] = value

            records[index] = updated

            updated_record = updated

            break

        if updated_record is None:

            raise ValueError(
                f"Schedule_extg record {record_id!r} "
                "could not be found."
            )

        self._write_records(
            records
        )

        self._mirror_record(updated_record)

        return updated_record

    def _mirror_record(self, record):
        """Mirror a completed local state write without changing local outcome."""
        if self.oracle_mirror is None:
            return
        try:
            self.oracle_mirror.mirror(record)
        except Exception:
            # The mirror itself already sanitises Oracle errors. This second
            # guard keeps a custom/injected mirror from affecting execution.
            logger.warning("Schedule_extg Oracle mirror raised unexpectedly; local state was retained.")

    def _update_existing_record(
        self,
        record,
        **changes,
    ):
        record_id = self._record_id(
            record
        )

        return self._update_by_id(
            record_id,
            changes,
        )

    # ========================================================================
    # ID
    # ========================================================================

    @staticmethod
    def _record_id(
        record,
    ):
        if record is None:
            return None

        if isinstance(
            record,
            dict,
        ):

            return record.get(
                "id"
            )

        return getattr(
            record,
            "id",
            None,
        )

    @staticmethod
    def _same_id(
        left,
        right,
    ):
        if left is None or right is None:
            return False

        try:
            return int(left) == int(right)

        except (
            TypeError,
            ValueError,
        ):
            return str(left) == str(right)

    # ========================================================================
    # NEXT ID
    # ========================================================================

    @staticmethod
    def _next_id(
        records,
    ):
        """
        Generate the next numeric Schedule_extg ID.
        """

        maximum = 0

        for record in records:

            value = record.get(
                "id"
            )

            try:

                value = int(
                    value
                )

            except (
                TypeError,
                ValueError,
            ):

                continue

            maximum = max(
                maximum,
                value,
            )

        return maximum + 1

    # ========================================================================
    # NORMALIZATION
    # ========================================================================

    @staticmethod
    def _normalize_name(
        value,
    ):
        if value is None:
            return None

        return str(
            value
        ).strip()

    @classmethod
    def _normalize_status(
        cls,
        value,
    ):
        if value is None:
            return None

        status = str(
            value
        ).strip().upper()

        if status not in cls.VALID_STATUSES:

            raise ValueError(
                f"Invalid Schedule_extg status: "
                f"{value!r}. "
                f"Expected one of "
                f"{sorted(cls.VALID_STATUSES)}."
            )

        return status

    # ========================================================================
    # DATE/TIME
    # ========================================================================

    @staticmethod
    def _now():
        return datetime.now().isoformat(
            timespec="seconds"
        )

    @classmethod
    def _date_to_string(
        cls,
        value,
    ):
        if value is None:
            return None

        if isinstance(
            value,
            datetime,
        ):

            return value.date().isoformat()

        if isinstance(
            value,
            date,
        ):

            return value.isoformat()

        value = str(
            value
        ).strip()

        if not value:
            return None

        # Already ISO date.
        try:

            return date.fromisoformat(
                value
            ).isoformat()

        except ValueError:
            pass

        # ISO datetime.
        try:

            return datetime.fromisoformat(
                value
            ).date().isoformat()

        except ValueError:
            pass

        # Common Oracle/application date formats.
        for fmt in (
            "%d-%b-%Y",
            "%d-%B-%Y",
            "%d/%m/%Y",
            "%d-%m-%Y",
            "%Y/%m/%d",
            "%Y%m%d",
        ):

            try:

                return datetime.strptime(
                    value,
                    fmt,
                ).date().isoformat()

            except ValueError:
                continue

        raise ValueError(
            f"Unsupported report_date value: "
            f"{value!r}"
        )

    # ========================================================================
    # RUN CONFIG
    # ========================================================================

    @staticmethod
    def _copy_run_config(
        run_config,
    ):
        if run_config is None:
            return None

        if isinstance(
            run_config,
            str,
        ):

            try:

                return json.loads(
                    run_config
                )

            except json.JSONDecodeError:

                return run_config

        if isinstance(
            run_config,
            dict,
        ):

            return dict(
                run_config
            )

        return run_config

    # ========================================================================
    # INTEGER
    # ========================================================================

    @staticmethod
    def _to_int(
        value,
    ):
        if value is None:
            return 0

        if isinstance(
            value,
            bool,
        ):

            return 1 if value else 0

        try:

            return int(
                value
            )

        except (
            TypeError,
            ValueError,
        ):

            return 1 if str(
                value
            ).strip().upper() in {
                "TRUE",
                "YES",
                "Y",
            } else 0

    # ========================================================================
    # ERROR INFO
    # ========================================================================

    @staticmethod
    def _build_error_info(
        error=None,
        error_type=None,
    ):
        if (
            error is None
            and error_type is None
        ):
            return None

        if (
            error_type is None
        ):
            return str(
                error
            )

        if error is None:

            return str(
                error_type
            )

        return (
            f"[{error_type}] "
            f"{error}"
        )
