from datetime import datetime, date
from datetime_compat import parse_iso_datetime

from database.sqlite_db import get_connection
from models.execution_job import ExecutionJob
from repositories.worker_logging import atomic_logging, ensure_context_schema, log_execution


class ExecutionRepository:
    """Persistent SQLite repository for Oracle execution attempts."""

    def __init__(self, connection=None, event_logger=None):
        self.connection = connection
        self.event_logger = event_logger
        if event_logger is not None and connection is not None:
            ensure_context_schema(connection)

    def _get_connection(self):
        if self.connection is not None:
            return self.connection
        return get_connection()

    @staticmethod
    def _now():
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _date_string(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return str(value)[:10]

    @staticmethod
    def _datetime_string(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat(timespec="seconds")
        return str(value)

    def start_execution(
        self,
        job_id,
        job_name,
        procedure_name,
        report_date=None,
        started_at=None,
    ):
        """Create a RUNNING execution attempt and return its id."""
        connection = self._get_connection()
        close_connection = connection is not self.connection

        try:
            with atomic_logging(connection):
                now = self._now()
                started = self._datetime_string(started_at) or now
                report = self._date_string(report_date)

                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(attempt_no), 0) + 1
                    FROM execution_jobs
                    WHERE job_id = ?
                      AND report_date IS ?
                    """,
                    (int(job_id), report),
                ).fetchone()

                attempt_no = int(row[0])

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
                    VALUES (?, ?, ?, ?, 'RUNNING', ?, NULL, ?, NULL,
                            NULL, NULL, NULL, ?, ?)
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

                if self.event_logger is not None:
                    ensure_context_schema(connection)
                    log_execution(self.event_logger, connection, cursor.lastrowid)
                return cursor.lastrowid
        finally:
            if close_connection:
                connection.close()

    def get_by_id(self, execution_id):
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            row = connection.execute(
                "SELECT * FROM execution_jobs WHERE id = ?",
                (execution_id,),
            ).fetchone()
            return self._row_to_model(row) if row else None
        finally:
            if close_connection:
                connection.close()

    def get_latest(self, job_id, report_date=None):
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            row = connection.execute(
                """
                SELECT *
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(job_id), self._date_string(report_date)),
            ).fetchone()
            return self._row_to_model(row) if row else None
        finally:
            if close_connection:
                connection.close()

    def has_success(self, job_id, report_date):
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            row = connection.execute(
                """
                SELECT 1
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                  AND status = 'SUCCESS'
                LIMIT 1
                """,
                (int(job_id), self._date_string(report_date)),
            ).fetchone()
            return row is not None
        finally:
            if close_connection:
                connection.close()

    def has_running(self, job_id, report_date):
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            row = connection.execute(
                """
                SELECT 1
                FROM execution_jobs
                WHERE job_id = ?
                  AND report_date IS ?
                  AND status = 'RUNNING'
                LIMIT 1
                """,
                (int(job_id), self._date_string(report_date)),
            ).fetchone()
            return row is not None
        finally:
            if close_connection:
                connection.close()

    def mark_success(
        self,
        execution_id,
        count=None,
        finished_at=None,
        duration_seconds=None,
    ):
        self._finish(
            execution_id=execution_id,
            status="SUCCESS",
            count=count,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            error=None,
            error_type=None,
        )

    def mark_failed(
        self,
        execution_id,
        error,
        error_type=None,
        finished_at=None,
        duration_seconds=None,
    ):
        self._finish(
            execution_id=execution_id,
            status="FAILED",
            count=None,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            error=error,
            error_type=error_type,
        )

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
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            with atomic_logging(connection):
                existing = connection.execute('SELECT status FROM execution_jobs WHERE id=?', (execution_id,)).fetchone()
                if self.event_logger is not None and existing and existing[0] == status:
                    return
                now = self._now()
                finished = self._datetime_string(finished_at) or now

                connection.execute(
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
                if self.event_logger is not None and existing:
                    ensure_context_schema(connection)
                    log_execution(self.event_logger, connection, execution_id)
        finally:
            if close_connection:
                connection.close()

    def recover_running(
        self,
        error="Scheduler restarted while execution was RUNNING.",
    ):
        """Convert orphaned RUNNING records into FAILED records."""
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            with atomic_logging(connection):
                rows = connection.execute(
                    """
                    SELECT id, started_at
                    FROM execution_jobs
                    WHERE status = 'RUNNING'
                    """
                ).fetchall()

                now_dt = datetime.now()
                now = now_dt.isoformat(timespec="seconds")

                for row in rows:
                    duration = None
                    if row[1]:
                        try:
                            started = parse_iso_datetime(row[1])
                            duration = (now_dt - started).total_seconds()
                        except (TypeError, ValueError):
                            pass

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
                        """,
                        (now, error, duration, now, row[0]),
                    )

                if self.event_logger is not None:
                    ensure_context_schema(connection)
                    for row in rows:
                        log_execution(self.event_logger, connection, row[0], recovered=True)
                return len(rows)
        finally:
            if close_connection:
                connection.close()

    def get_by_status(self, status):
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            rows = connection.execute(
                """
                SELECT *
                FROM execution_jobs
                WHERE status = ?
                ORDER BY id DESC
                """,
                (str(status).upper(),),
            ).fetchall()
            return [self._row_to_model(row) for row in rows]
        finally:
            if close_connection:
                connection.close()

    def get_all(self, limit=None):
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            if limit is None:
                rows = connection.execute(
                    "SELECT * FROM execution_jobs ORDER BY id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM execution_jobs ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [self._row_to_model(row) for row in rows]
        finally:
            if close_connection:
                connection.close()

    @staticmethod
    def _planned_dates_cte(connection):
        """Resolve durable planned days, preferring occurrence state to legacy rows.

        Missing, malformed or conflicting dates remain unknown. An execution's
        report date/start timestamp is never a substitute for its planned day.
        """
        available = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources = []
        for table, rank in (("ready_occurrences", 0), ("staging_occurrences", 0),
                            ("ready_jobs", 1), ("staging_jobs", 1)):
            if table not in available:
                continue
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if {"job_id", "report_date", "execution_date"}.issubset(columns):
                sources.append(f"SELECT job_id, report_date, execution_date, {rank} AS source_rank FROM {table}")
        if not sources:
            return None
        return """WITH durable AS (""" + " UNION ALL ".join(sources) + """),
            preferred AS (
                SELECT d.* FROM durable AS d WHERE source_rank = (
                    SELECT MIN(other.source_rank) FROM durable AS other
                    WHERE other.job_id=d.job_id AND other.report_date IS d.report_date
                )
            ), planned AS (
                SELECT job_id, report_date,
                    CASE WHEN COUNT(date(execution_date))=COUNT(*)
                              AND MIN(date(execution_date))=MAX(date(execution_date))
                         THEN MIN(date(execution_date)) END AS execution_date
                FROM preferred GROUP BY job_id, report_date
            ) """

    def get_planned_execution_date(self, job_id, report_date):
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            cte = self._planned_dates_cte(connection)
            if cte is None:
                return None
            row = connection.execute(cte + "SELECT execution_date FROM planned WHERE job_id=? AND report_date IS ?",
                                     (int(job_id), self._date_string(report_date))).fetchone()
            return date.fromisoformat(row[0]) if row and row[0] else None
        finally:
            if close_connection:
                connection.close()

    def get_retry_candidates(self, current_date, lookback_days=15, limit=100):
        """Return unresolved failures planned for this actual execution day only.

        ``lookback_days`` remains accepted for caller compatibility; past planned
        days now require manual requests, regardless of the report-date margin.
        """
        today = self._date_string(current_date)
        if today is None:
            raise ValueError("current_date is required for retry selection.")
        try:
            lookback_days = max(1, int(lookback_days))
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError) as exc:
            raise ValueError("Retry lookback and limit must be positive integers.") from exc

        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            cte = self._planned_dates_cte(connection)
            if cte is None:
                return []
            rows = connection.execute(
                cte + """
                SELECT failed.*
                FROM execution_jobs AS failed
                JOIN planned ON planned.job_id=failed.job_id AND planned.report_date IS failed.report_date
                WHERE failed.status = 'FAILED'
                  AND failed.report_date IS NOT NULL
                  AND planned.execution_date = ?
                  AND failed.id = (
                      SELECT MAX(latest.id)
                      FROM execution_jobs AS latest
                      WHERE latest.job_id = failed.job_id
                        AND latest.report_date IS failed.report_date
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM execution_jobs AS completed
                      WHERE completed.job_id = failed.job_id
                        AND completed.report_date IS failed.report_date
                        AND completed.status = 'SUCCESS'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM execution_jobs AS active
                      WHERE active.job_id = failed.job_id
                        AND active.report_date IS failed.report_date
                        AND active.status = 'RUNNING'
                  )
                ORDER BY failed.report_date ASC, failed.id ASC
                LIMIT ?
                """,
                (today, limit),
            ).fetchall()
            return [self._row_to_model(row) for row in rows]
        finally:
            if close_connection:
                connection.close()

    @staticmethod
    def _parse_datetime(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return parse_iso_datetime(str(value))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _row_to_model(cls, row):
        if row is None:
            return None

        return ExecutionJob(
            id=row["id"],
            job_id=row["job_id"],
            job_name=row["job_name"],
            procedure_name=row["procedure_name"],
            report_date=(
                date.fromisoformat(row["report_date"])
                if row["report_date"] else None
            ),
            status=row["status"],
            attempt_no=row["attempt_no"],
            count=row["count"],
            started_at=cls._parse_datetime(row["started_at"]),
            finished_at=cls._parse_datetime(row["finished_at"]),
            error=row["error"],
            error_type=row["error_type"],
            duration_seconds=row["duration_seconds"],
            created_at=cls._parse_datetime(row["created_at"]),
            updated_at=cls._parse_datetime(row["updated_at"]),
        )
