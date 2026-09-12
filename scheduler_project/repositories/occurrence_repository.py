"""Occurrence-scoped local persistence used by the Oracle-compatible worker.

The older ``staging_jobs`` and ``ready_jobs`` tables are keyed only by
``job_id``.  They remain available for compatibility, but this repository
uses the occurrence-key tables created by ``database.sqlite_db`` so that one
master job may safely carry multiple distinct report dates at once.
"""

from __future__ import annotations

import json

from models.ready_job import ReadyJob
from models.staging_job import StagingJob


class _OccurrenceRepository:
    def __init__(self, connection):
        self.connection = connection

    def delete_by_key(self, occurrence_key, *, commit=True):
        if occurrence_key is None:
            return False
        cursor = self.connection.execute(
            f"DELETE FROM {self.TABLE} WHERE occurrence_key = ?",
            (str(occurrence_key),),
        )
        if commit:
            self.connection.commit()
        return cursor.rowcount > 0

    def get_by_key(self, occurrence_key):
        if occurrence_key is None:
            return None
        row = self.connection.execute(
            f"SELECT {self.COLUMNS} FROM {self.TABLE} WHERE occurrence_key = ?",
            (str(occurrence_key),),
        ).fetchone()
        return self._row_to_model(row) if row is not None else None

    def get_by_job_id(self, job_id):
        rows = self.connection.execute(
            f"""
            SELECT {self.COLUMNS} FROM {self.TABLE}
            WHERE job_id = ?
            ORDER BY COALESCE(report_date, '9999-12-31'), occurrence_key
            """,
            (job_id,),
        ).fetchall()
        return [self._row_to_model(row) for row in rows]

    def get_by_id(self, job_id):
        """Compatibility helper returning the oldest occurrence for a job."""
        values = self.get_by_job_id(job_id)
        return values[0] if values else None

    def get_all(self):
        rows = self.connection.execute(
            f"SELECT {self.COLUMNS} FROM {self.TABLE} ORDER BY {self.ORDER_BY}"
        ).fetchall()
        return [self._row_to_model(row) for row in rows]

    def delete(self, job_id):
        """Compatibility helper deleting every occurrence of a master job."""
        self.connection.execute(
            f"DELETE FROM {self.TABLE} WHERE job_id = ?",
            (job_id,),
        )
        self.connection.commit()

    def clear(self):
        self.connection.execute(f"DELETE FROM {self.TABLE}")
        self.connection.commit()

    def count(self):
        return self.connection.execute(
            f"SELECT COUNT(*) FROM {self.TABLE}"
        ).fetchone()[0]

    @staticmethod
    def _key_for(job):
        key = getattr(job, "occurrence_key", None)
        if key:
            return str(key)
        job_id = getattr(job, "job_id", None)
        report_date = getattr(job, "report_date", None)
        occurrence_date = getattr(job, "occurrence_date", None)
        return f"{job_id}:{report_date or occurrence_date or 'legacy'}"


class OccurrenceStagingRepository(_OccurrenceRepository):
    TABLE = "staging_occurrences"
    COLUMNS = """
        occurrence_key, job_id, job_name, state, occurrence_date,
        execution_date, t_date, report_date, target_date, margin,
        confirmation_required, confirmation_status, time_flag,
        from_time, to_time, waiting_for, reason, next_evaluation,
        calculated_at, updated_at
    """
    ORDER_BY = "COALESCE(execution_date, '9999-12-31'), COALESCE(report_date, '9999-12-31'), occurrence_key"

    def save(self, job, *, commit=True):
        key = self._key_for(job)
        self.connection.execute(
            f"""
            INSERT INTO {self.TABLE} ({self.COLUMNS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(occurrence_key) DO UPDATE SET
                job_id=excluded.job_id, job_name=excluded.job_name,
                state=excluded.state, occurrence_date=excluded.occurrence_date,
                execution_date=excluded.execution_date, t_date=excluded.t_date,
                report_date=excluded.report_date, target_date=excluded.target_date,
                margin=excluded.margin,
                confirmation_required=excluded.confirmation_required,
                confirmation_status=excluded.confirmation_status,
                time_flag=excluded.time_flag, from_time=excluded.from_time,
                to_time=excluded.to_time, waiting_for=excluded.waiting_for,
                reason=excluded.reason, next_evaluation=excluded.next_evaluation,
                calculated_at=excluded.calculated_at, updated_at=excluded.updated_at
            """,
            (
                key, job.job_id, job.job_name, job.state,
                job.occurrence_date, job.execution_date, job.t_date,
                job.report_date, job.target_date, job.margin,
                job.confirmation_required, job.confirmation_status,
                job.time_flag, job.from_time, job.to_time, job.waiting_for,
                job.reason, job.next_evaluation, job.calculated_at, job.updated_at,
            ),
        )
        if commit:
            self.connection.commit()
        job.occurrence_key = key
        return job

    def get_by_state(self, state):
        rows = self.connection.execute(
            f"SELECT {self.COLUMNS} FROM {self.TABLE} WHERE state = ? ORDER BY {self.ORDER_BY}",
            (state,),
        ).fetchall()
        return [self._row_to_model(row) for row in rows]

    @staticmethod
    def _row_to_model(row):
        return StagingJob(
            occurrence_key=row[0], job_id=row[1], job_name=row[2], state=row[3],
            occurrence_date=row[4], execution_date=row[5], t_date=row[6],
            report_date=row[7], target_date=row[8], margin=row[9],
            confirmation_required=row[10], confirmation_status=row[11],
            time_flag=row[12], from_time=row[13], to_time=row[14],
            waiting_for=row[15], reason=row[16], next_evaluation=row[17],
            calculated_at=row[18], updated_at=row[19],
        )


class OccurrenceReadyRepository(_OccurrenceRepository):
    TABLE = "ready_occurrences"
    COLUMNS = """
        occurrence_key, job_id, job_name, occurrence_date, execution_date,
        t_date, report_date, target_date, ready_since, time_priority,
        date_priority, job_priority, priority_key, from_time, to_time,
        time_state, updated_at
    """
    ORDER_BY = "date_priority, time_priority, job_priority, occurrence_key"

    def save(self, job, *, commit=True):
        key = self._key_for(job)
        priority_key = getattr(job, "priority_key", None)
        if priority_key is not None:
            priority_key = json.dumps(list(priority_key))
        self.connection.execute(
            f"""
            INSERT INTO {self.TABLE} ({self.COLUMNS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(occurrence_key) DO UPDATE SET
                job_id=excluded.job_id, job_name=excluded.job_name,
                occurrence_date=excluded.occurrence_date,
                execution_date=excluded.execution_date, t_date=excluded.t_date,
                report_date=excluded.report_date, target_date=excluded.target_date,
                ready_since=excluded.ready_since,
                time_priority=excluded.time_priority,
                date_priority=excluded.date_priority,
                job_priority=excluded.job_priority,
                priority_key=excluded.priority_key, from_time=excluded.from_time,
                to_time=excluded.to_time, time_state=excluded.time_state,
                updated_at=excluded.updated_at
            """,
            (
                key, job.job_id, job.job_name, job.occurrence_date,
                job.execution_date, job.t_date, job.report_date,
                job.target_date, job.ready_since, job.time_priority,
                job.date_priority, job.job_priority, priority_key, job.from_time,
                job.to_time, job.time_state, job.updated_at,
            ),
        )
        if commit:
            self.connection.commit()
        job.occurrence_key = key
        return job

    @staticmethod
    def _row_to_model(row):
        priority_key = row[12]
        if priority_key:
            try:
                priority_key = tuple(json.loads(priority_key))
            except (json.JSONDecodeError, TypeError, ValueError):
                priority_key = None
        return ReadyJob(
            occurrence_key=row[0], job_id=row[1], job_name=row[2],
            occurrence_date=row[3], execution_date=row[4], t_date=row[5],
            report_date=row[6], target_date=row[7], ready_since=row[8],
            time_priority=row[9], date_priority=row[10], job_priority=row[11],
            priority_key=priority_key, from_time=row[13], to_time=row[14],
            time_state=row[15], updated_at=row[16],
        )
