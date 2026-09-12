import json


class ReadyRepository:
    """
    Repository for persistent READY jobs.

    SQLite is the persistent store.
    The in-memory heap is rebuilt from this repository.
    """

    def __init__(self, connection):
        self.connection = connection

    # =========================================================
    # Save / Update
    # =========================================================

    def save(self, job):
        """
        Insert or update a READY job.

        The complete execution context is persisted:
            occurrence_date
            execution_date
            t_date
            report_date
            target_date
        """

        query = """
            INSERT INTO ready_jobs (
                job_id,
                job_name,

                occurrence_date,
                execution_date,
                t_date,
                report_date,
                target_date,

                ready_since,

                time_priority,
                date_priority,
                job_priority,
                priority_key,

                from_time,
                to_time,
                time_state,

                updated_at
            )
            VALUES (
                ?, ?,

                ?, ?, ?, ?, ?,

                ?,

                ?, ?, ?, ?,

                ?, ?, ?,

                ?
            )
            ON CONFLICT(job_id)
            DO UPDATE SET
                job_name = excluded.job_name,

                occurrence_date = excluded.occurrence_date,
                execution_date = excluded.execution_date,
                t_date = excluded.t_date,
                report_date = excluded.report_date,
                target_date = excluded.target_date,

                ready_since = excluded.ready_since,

                time_priority = excluded.time_priority,
                date_priority = excluded.date_priority,
                job_priority = excluded.job_priority,
                priority_key = excluded.priority_key,

                from_time = excluded.from_time,
                to_time = excluded.to_time,
                time_state = excluded.time_state,

                updated_at = excluded.updated_at
        """

        priority_key = job.priority_key

        if priority_key is not None:
            priority_key = json.dumps(
                list(priority_key)
            )

        self.connection.execute(
            query,
            (
                job.job_id,
                job.job_name,

                job.occurrence_date,
                job.execution_date,
                job.t_date,
                job.report_date,
                job.target_date,

                job.ready_since,

                job.time_priority,
                job.date_priority,
                job.job_priority,
                priority_key,

                job.from_time,
                job.to_time,
                job.time_state,

                job.updated_at,
            ),
        )

        self.connection.commit()

    # =========================================================
    # Get one
    # =========================================================

    def get_by_id(self, job_id):
        """
        Return one ReadyJob or None.
        """

        query = """
            SELECT
                job_id,
                job_name,

                occurrence_date,
                execution_date,
                t_date,
                report_date,
                target_date,

                ready_since,

                time_priority,
                date_priority,
                job_priority,
                priority_key,

                from_time,
                to_time,
                time_state,

                updated_at
            FROM ready_jobs
            WHERE job_id = ?
        """

        row = self.connection.execute(
            query,
            (job_id,),
        ).fetchone()

        if row is None:
            return None

        return self._row_to_model(row)

    # =========================================================
    # Get all
    # =========================================================

    def get_all(self):
        """
        Return all READY jobs.

        The SQL ordering is only for inspection.
        The actual execution order comes from the heap.
        """

        query = """
            SELECT
                job_id,
                job_name,

                occurrence_date,
                execution_date,
                t_date,
                report_date,
                target_date,

                ready_since,

                time_priority,
                date_priority,
                job_priority,
                priority_key,

                from_time,
                to_time,
                time_state,

                updated_at
            FROM ready_jobs
            ORDER BY
                date_priority ASC,
                time_priority ASC,
                job_priority ASC,
                job_id ASC
        """

        rows = self.connection.execute(
            query
        ).fetchall()

        return [
            self._row_to_model(row)
            for row in rows
        ]

    # =========================================================
    # Delete
    # =========================================================

    def delete(self, job_id):
        """
        Remove a job from READY state.
        """

        self.connection.execute(
            """
            DELETE FROM ready_jobs
            WHERE job_id = ?
            """,
            (job_id,),
        )

        self.connection.commit()

    # =========================================================
    # Clear
    # =========================================================

    def clear(self):
        """
        Remove all READY records.

        Primarily useful when rebuilding scheduler state.
        """

        self.connection.execute(
            """
            DELETE FROM ready_jobs
            """
        )

        self.connection.commit()

    # =========================================================
    # Count
    # =========================================================

    def count(self):
        row = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM ready_jobs
            """
        ).fetchone()

        return row[0]

    # =========================================================
    # Convert database row → model
    # =========================================================

    @staticmethod
    def _row_to_model(row):
        from models.ready_job import ReadyJob

        (
            job_id,
            job_name,

            occurrence_date,
            execution_date,
            t_date,
            report_date,
            target_date,

            ready_since,

            time_priority,
            date_priority,
            job_priority,
            priority_key,

            from_time,
            to_time,
            time_state,

            updated_at,
        ) = row

        # -----------------------------------------------------
        # Convert priority_key back to tuple
        # -----------------------------------------------------

        if priority_key:
            try:
                priority_key = tuple(
                    json.loads(priority_key)
                )

            except (
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                priority_key = None

        return ReadyJob(
            job_id=job_id,
            job_name=job_name,

            occurrence_date=occurrence_date,
            execution_date=execution_date,
            t_date=t_date,
            report_date=report_date,
            target_date=target_date,

            ready_since=ready_since,

            time_priority=time_priority,
            date_priority=date_priority,
            job_priority=job_priority,
            priority_key=priority_key,

            from_time=from_time,
            to_time=to_time,
            time_state=time_state,

            updated_at=updated_at,
        )
