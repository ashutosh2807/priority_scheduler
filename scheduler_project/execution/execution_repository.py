from datetime import date, datetime
from datetime_compat import parse_iso_datetime

from database.sqlite_db import get_connection
from models.execution_job import ExecutionJob


class ExecutionRepository:
    """
    Persistent SQLite repository for Oracle execution attempts.

    One row in execution_jobs represents one execution attempt.

    Lifecycle:

        RUNNING
           |
           +------> SUCCESS
           |
           +------> FAILED

    A FAILED execution is retained in history and can be retried.
    Every retry creates a new execution_jobs row with an incremented
    attempt_no.

    Scheduled occurrence identity:

        job_id + report_date

    Manual execution:

        job_id + report_date=None

    The repository is intentionally independent of Oracle.
    """

    # =====================================================================
    # INITIALIZATION
    # =====================================================================

    def __init__(
        self,
        connection=None,
    ):
        """
        Parameters
        ----------
        connection:
            Optional existing SQLite connection.

            If omitted, a new connection is created for each operation.
        """

        self.connection = connection

    # =====================================================================
    # CONNECTION
    # =====================================================================

    def _get_connection(self):
        if self.connection is not None:
            return self.connection

        return get_connection()

    # =====================================================================
    # TIME HELPERS
    # =====================================================================

    @staticmethod
    def _now():
        return datetime.now().isoformat(
            timespec="seconds"
        )

    # =====================================================================
    # DATE HELPERS
    # =====================================================================

    @staticmethod
    def _date_string(
        value,
    ):
        """
        Convert a date-like value into YYYY-MM-DD.

        Supported:

            datetime
            date
            ISO date string
            ISO datetime string
        """

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

        # ISO date.
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

        # Common application/Oracle formats.
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
            f"Unsupported date value: {value!r}"
        )

    # =====================================================================
    # DATETIME HELPERS
    # =====================================================================

    @staticmethod
    def _datetime_string(
        value,
    ):
        if value is None:
            return None

        if isinstance(
            value,
            datetime,
        ):
            return value.isoformat(
                timespec="seconds"
            )

        return str(
            value
        )

    # =====================================================================
    # START EXECUTION
    # =====================================================================

    def start_execution(
        self,
        job_id,
        job_name,
        procedure_name,
        report_date=None,
        started_at=None,
    ):
        """
        Create a new RUNNING execution attempt.

        Returns
        -------
        int
            execution_jobs.id

        Important
        ---------
        This method MUST succeed before Oracle execution is attempted.

        The attempt number is calculated independently for each:

            job_id + report_date

        combination.
        """

        if job_id is None:
            raise ValueError(
                "job_id is required."
            )

        if not procedure_name:
            raise ValueError(
                "procedure_name is required."
            )

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            now = self._now()

            started = (
                self._datetime_string(
                    started_at
                )
                or now
            )

            report = self._date_string(
                report_date
            )

            # -------------------------------------------------------------
            # Determine next attempt number.
            #
            # "IS ?" is intentionally used so that NULL report_date
            # correctly represents manual execution.
            # -------------------------------------------------------------

            row = connection.execute(
                """
                SELECT COALESCE(
                    MAX(attempt_no),
                    0
                ) + 1
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                """,
                (
                    int(job_id),
                    report,
                ),
            ).fetchone()

            attempt_no = int(
                row[0]
            )

            # -------------------------------------------------------------
            # Insert RUNNING execution.
            # -------------------------------------------------------------

            cursor = connection.execute(
                """
                INSERT INTO execution_jobs (
                    job_id,
                    job_name,
                    procedure_name,
                    report_date,
                    status,
                    attempt_no,
                    count,
                    started_at,
                    finished_at,
                    error,
                    error_type,
                    duration_seconds,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    'RUNNING',
                    ?,
                    NULL,
                    ?,
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    ?,
                    ?
                )
                """,
                (
                    int(job_id),
                    job_name,
                    procedure_name,
                    report,
                    attempt_no,
                    started,
                    now,
                    now,
                ),
            )

            connection.commit()

            return cursor.lastrowid

        except Exception:

            connection.rollback()

            raise

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # BACKWARD-COMPATIBILITY ALIAS
    # =====================================================================

    def start(
        self,
        job_id,
        job_name,
        procedure_name,
        report_date=None,
        started_at=None,
    ):
        """
        Compatibility alias for start_execution().

        The current ExecutionManager uses start_execution().
        Keeping this alias makes the repository safer for older callers.
        """

        return self.start_execution(
            job_id=job_id,
            job_name=job_name,
            procedure_name=procedure_name,
            report_date=report_date,
            started_at=started_at,
        )

    # =====================================================================
    # GET BY ID
    # =====================================================================

    def get_by_id(
        self,
        execution_id,
    ):
        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            row = connection.execute(
                """
                SELECT *
                FROM execution_jobs
                WHERE id = ?
                """,
                (
                    execution_id,
                ),
            ).fetchone()

            if row is None:
                return None

            return self._row_to_model(
                row
            )

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # GET LATEST
    # =====================================================================

    def get_latest(
        self,
        job_id,
        report_date=None,
    ):
        """
        Return the latest execution attempt for a job/report-date pair.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            report = self._date_string(
                report_date
            )

            row = connection.execute(
                """
                SELECT *
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    int(job_id),
                    report,
                ),
            ).fetchone()

            if row is None:
                return None

            return self._row_to_model(
                row
            )

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # SUCCESS CHECK
    # =====================================================================

    def has_success(
        self,
        job_id,
        report_date,
    ):
        """
        Return True when the scheduled occurrence already succeeded.

        For scheduled executions:

            job_id + report_date

        identifies an occurrence.

        A SUCCESS record prevents another automatic execution of the
        same occurrence.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            report = self._date_string(
                report_date
            )

            row = connection.execute(
                """
                SELECT 1
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                  AND status = 'SUCCESS'
                LIMIT 1
                """,
                (
                    int(job_id),
                    report,
                ),
            ).fetchone()

            return row is not None

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # RUNNING CHECK
    # =====================================================================

    def has_running(
        self,
        job_id,
        report_date,
    ):
        """
        Return True when the same occurrence is already RUNNING.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            report = self._date_string(
                report_date
            )

            row = connection.execute(
                """
                SELECT 1
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                  AND status = 'RUNNING'
                LIMIT 1
                """,
                (
                    int(job_id),
                    report,
                ),
            ).fetchone()

            return row is not None

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # FAILED CHECK
    # =====================================================================

    def has_failed(
        self,
        job_id,
        report_date,
    ):
        """
        Return True when at least one FAILED attempt exists.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            report = self._date_string(
                report_date
            )

            row = connection.execute(
                """
                SELECT 1
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                  AND status = 'FAILED'
                LIMIT 1
                """,
                (
                    int(job_id),
                    report,
                ),
            ).fetchone()

            return row is not None

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # MARK SUCCESS
    # =====================================================================

    def mark_success(
        self,
        execution_id,
        count=None,
        finished_at=None,
        duration_seconds=None,
    ):
        """
        Mark an execution attempt SUCCESS.
        """

        self._finish(
            execution_id=execution_id,
            status="SUCCESS",
            count=count,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            error=None,
            error_type=None,
        )

    # =====================================================================
    # MARK FAILED
    # =====================================================================

    def mark_failed(
        self,
        execution_id,
        error,
        error_type=None,
        finished_at=None,
        duration_seconds=None,
    ):
        """
        Mark an execution attempt FAILED.

        The failure remains permanently available in execution history.
        """

        self._finish(
            execution_id=execution_id,
            status="FAILED",
            count=None,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            error=error,
            error_type=error_type,
        )

    # =====================================================================
    # FINISH EXECUTION
    # =====================================================================

    def _finish(
        self,
        execution_id,
        status,
        count,
        finished_at,
        duration_seconds,
        error,
        error_type,
    ):
        if execution_id is None:
            raise ValueError(
                "execution_id is required."
            )

        status = str(
            status
        ).strip().upper()

        if status not in {
            "SUCCESS",
            "FAILED",
        }:

            raise ValueError(
                "Execution can only be finished "
                "as SUCCESS or FAILED."
            )

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            now = self._now()

            finished = (
                self._datetime_string(
                    finished_at
                )
                or now
            )

            # -------------------------------------------------------------
            # If duration wasn't supplied, calculate it from started_at.
            # -------------------------------------------------------------

            if duration_seconds is None:

                row = connection.execute(
                    """
                    SELECT started_at
                    FROM execution_jobs
                    WHERE id = ?
                    """,
                    (
                        execution_id,
                    ),
                ).fetchone()

                if row is not None:

                    started_at = (
                        row["started_at"]
                    )

                    duration_seconds = (
                        self._calculate_duration(
                            started_at,
                            finished,
                        )
                    )

            cursor = connection.execute(
                """
                UPDATE execution_jobs
                SET status = ?,
                    count = ?,
                    finished_at = ?,
                    error = ?,
                    error_type = ?,
                    duration_seconds = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    count,
                    finished,
                    error,
                    error_type,
                    duration_seconds,
                    now,
                    execution_id,
                ),
            )

            if cursor.rowcount == 0:

                raise ValueError(
                    "Execution record not found: "
                    f"{execution_id}"
                )

            connection.commit()

        except Exception:

            connection.rollback()

            raise

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # CALCULATE DURATION
    # =====================================================================

    @staticmethod
    def _calculate_duration(
        started_at,
        finished_at,
    ):
        if not started_at:
            return None

        try:

            started = (
                parse_iso_datetime(
                    str(started_at)
                )
            )

            finished = (
                parse_iso_datetime(
                    str(finished_at)
                )
            )

            return (
                finished - started
            ).total_seconds()

        except (
            TypeError,
            ValueError,
        ):

            return None

    # =====================================================================
    # RECOVER ORPHANED RUNNING
    # =====================================================================

    def recover_running(
        self,
        error=(
            "Scheduler restarted while "
            "execution was RUNNING."
        ),
    ):
        """
        Convert orphaned RUNNING executions to FAILED.

        This is called during scheduler startup.

        Why?

        If the Python scheduler process dies while an Oracle execution
        record is RUNNING, the record must not remain RUNNING forever.

        The execution history therefore becomes:

            RUNNING
                ↓
            FAILED
            error_type = SchedulerRestart

        Returns
        -------
        int
            Number of recovered executions.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            rows = connection.execute(
                """
                SELECT
                    id,
                    started_at
                FROM execution_jobs
                WHERE status = 'RUNNING'
                ORDER BY id
                """
            ).fetchall()

            if not rows:

                return 0

            now_dt = datetime.now()

            now = now_dt.isoformat(
                timespec="seconds"
            )

            recovered_count = 0

            for row in rows:

                duration = (
                    self._calculate_duration(
                        row["started_at"],
                        now,
                    )
                )

                connection.execute(
                    """
                    UPDATE execution_jobs
                    SET status = 'FAILED',
                        finished_at = ?,
                        error = ?,
                        error_type = 'SchedulerRestart',
                        duration_seconds = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND status = 'RUNNING'
                    """,
                    (
                        now,
                        error,
                        duration,
                        now,
                        row["id"],
                    ),
                )

                recovered_count += 1

            connection.commit()

            return recovered_count

        except Exception:

            connection.rollback()

            raise

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # GET BY STATUS
    # =====================================================================

    def get_by_status(
        self,
        status,
    ):
        """
        Return all execution attempts having the supplied status.
        """

        status = str(
            status
        ).strip().upper()

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            rows = connection.execute(
                """
                SELECT *
                FROM execution_jobs
                WHERE status = ?
                ORDER BY id DESC
                """,
                (
                    status,
                ),
            ).fetchall()

            return [
                self._row_to_model(row)
                for row in rows
            ]

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # GET ALL
    # =====================================================================

    def get_all(
        self,
        limit=None,
    ):
        """
        Return execution history ordered newest first.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            if limit is None:

                rows = connection.execute(
                    """
                    SELECT *
                    FROM execution_jobs
                    ORDER BY id DESC
                    """
                ).fetchall()

            else:

                limit = int(
                    limit
                )

                if limit <= 0:
                    raise ValueError(
                        "limit must be greater than zero."
                    )

                rows = connection.execute(
                    """
                    SELECT *
                    FROM execution_jobs
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (
                        limit,
                    ),
                ).fetchall()

            return [
                self._row_to_model(row)
                for row in rows
            ]

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # GET JOB HISTORY
    # =====================================================================

    def get_job_history(
        self,
        job_id,
        report_date=None,
    ):
        """
        Return all attempts for a job/report-date pair.

        Useful for monitoring and retry analysis.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            report = self._date_string(
                report_date
            )

            rows = connection.execute(
                """
                SELECT *
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                ORDER BY attempt_no DESC, id DESC
                """,
                (
                    int(job_id),
                    report,
                ),
            ).fetchall()

            return [
                self._row_to_model(row)
                for row in rows
            ]

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # GET LATEST SUCCESS
    # =====================================================================

    def get_latest_success(
        self,
        job_id,
        report_date=None,
    ):
        """
        Return the latest successful attempt for a job/report-date pair.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            report = self._date_string(
                report_date
            )

            row = connection.execute(
                """
                SELECT *
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                  AND status = 'SUCCESS'
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    int(job_id),
                    report,
                ),
            ).fetchone()

            if row is None:
                return None

            return self._row_to_model(
                row
            )

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # GET LATEST FAILED
    # =====================================================================

    def get_latest_failed(
        self,
        job_id,
        report_date=None,
    ):
        """
        Return the latest failed attempt for a job/report-date pair.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            report = self._date_string(
                report_date
            )

            row = connection.execute(
                """
                SELECT *
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                  AND status = 'FAILED'
                ORDER BY id DESC
                LIMIT 1
                """,
                (
                    int(job_id),
                    report,
                ),
            ).fetchone()

            if row is None:
                return None

            return self._row_to_model(
                row
            )

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # COUNT
    # =====================================================================

    def count(
        self,
        status=None,
    ):
        """
        Return number of execution records.

        If status is supplied, count only that status.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            if status is None:

                row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM execution_jobs
                    """
                ).fetchone()

            else:

                status = str(
                    status
                ).strip().upper()

                row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM execution_jobs
                    WHERE status = ?
                    """,
                    (
                        status,
                    ),
                ).fetchone()

            return int(
                row[0]
            )

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # DELETE BY ID
    # =====================================================================

    def delete(
        self,
        execution_id,
    ):
        """
        Delete one execution history record.

        Normally monitoring should not need this method. It is provided
        for administrative/test cleanup.
        """

        connection = self._get_connection()

        close_connection = (
            connection is not self.connection
        )

        try:

            cursor = connection.execute(
                """
                DELETE FROM execution_jobs
                WHERE id = ?
                """,
                (
                    execution_id,
                ),
            )

            connection.commit()

            return cursor.rowcount > 0

        except Exception:

            connection.rollback()

            raise

        finally:

            if close_connection:
                connection.close()

    # =====================================================================
    # MODEL CONVERSION
    # =====================================================================

    @classmethod
    def _row_to_model(
        cls,
        row,
    ):
        if row is None:
            return None

        return ExecutionJob(
            id=row["id"],
            job_id=row["job_id"],
            job_name=row["job_name"],
            procedure_name=row["procedure_name"],
            report_date=(
                date.fromisoformat(
                    row["report_date"]
                )
                if row["report_date"]
                else None
            ),
            status=row["status"],
            attempt_no=(
                row["attempt_no"]
                if row["attempt_no"] is not None
                else 1
            ),
            count=row["count"],
            started_at=cls._parse_datetime(
                row["started_at"]
            ),
            finished_at=cls._parse_datetime(
                row["finished_at"]
            ),
            error=row["error"],
            error_type=row["error_type"],
            duration_seconds=row[
                "duration_seconds"
            ],
            created_at=cls._parse_datetime(
                row["created_at"]
            ),
            updated_at=cls._parse_datetime(
                row["updated_at"]
            ),
        )

    # =====================================================================
    # DATETIME PARSER
    # =====================================================================

    @staticmethod
    def _parse_datetime(
        value,
    ):
        if value is None:
            return None

        if isinstance(
            value,
            datetime,
        ):
            return value

        try:

            return parse_iso_datetime(
                str(value)
            )

        except (
            TypeError,
            ValueError,
        ):

            return None