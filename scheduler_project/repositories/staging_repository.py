from models.staging_job import StagingJob


class StagingRepository:

    def __init__(self, connection):
        self.connection = connection

    # =========================================================
    # Save
    # =========================================================

    def save(self, job):

        query = """
            INSERT INTO staging_jobs (
                job_id,
                job_name,
                state,
                occurrence_date,
                execution_date,
                t_date,
                report_date,
                target_date,
                margin,
                confirmation_required,
                confirmation_status,
                time_flag,
                from_time,
                to_time,
                waiting_for,
                reason,
                next_evaluation,
                calculated_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            )

            ON CONFLICT(job_id)
            DO UPDATE SET
                job_name = excluded.job_name,
                state = excluded.state,
                occurrence_date = excluded.occurrence_date,
                execution_date = excluded.execution_date,
                t_date = excluded.t_date,
                report_date = excluded.report_date,
                target_date = excluded.target_date,
                margin = excluded.margin,
                confirmation_required = excluded.confirmation_required,
                confirmation_status = excluded.confirmation_status,
                time_flag = excluded.time_flag,
                from_time = excluded.from_time,
                to_time = excluded.to_time,
                waiting_for = excluded.waiting_for,
                reason = excluded.reason,
                next_evaluation = excluded.next_evaluation,
                calculated_at = excluded.calculated_at,
                updated_at = excluded.updated_at
        """

        self.connection.execute(
            query,
            (
                job.job_id,
                job.job_name,
                job.state,
                job.occurrence_date,
                job.execution_date,
                job.t_date,
                job.report_date,
                job.target_date,
                job.margin,
                job.confirmation_required,
                job.confirmation_status,
                job.time_flag,
                job.from_time,
                job.to_time,
                job.waiting_for,
                job.reason,
                job.next_evaluation,
                job.calculated_at,
                job.updated_at,
            ),
        )

        self.connection.commit()

    # =========================================================
    # Get by ID
    # =========================================================

    def get_by_id(self, job_id):

        query = """
            SELECT
                job_id,
                job_name,
                state,
                occurrence_date,
                execution_date,
                t_date,
                report_date,
                target_date,
                margin,
                confirmation_required,
                confirmation_status,
                time_flag,
                from_time,
                to_time,
                waiting_for,
                reason,
                next_evaluation,
                calculated_at,
                updated_at
            FROM staging_jobs
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

        query = """
            SELECT
                job_id,
                job_name,
                state,
                occurrence_date,
                execution_date,
                t_date,
                report_date,
                target_date,
                margin,
                confirmation_required,
                confirmation_status,
                time_flag,
                from_time,
                to_time,
                waiting_for,
                reason,
                next_evaluation,
                calculated_at,
                updated_at
            FROM staging_jobs
            ORDER BY
                target_date ASC,
                job_id ASC
        """

        rows = self.connection.execute(query).fetchall()

        return [
            self._row_to_model(row)
            for row in rows
        ]

    # =========================================================
    # Get by state
    # =========================================================

    def get_by_state(self, state):

        query = """
            SELECT
                job_id,
                job_name,
                state,
                occurrence_date,
                execution_date,
                t_date,
                report_date,
                target_date,
                margin,
                confirmation_required,
                confirmation_status,
                time_flag,
                from_time,
                to_time,
                waiting_for,
                reason,
                next_evaluation,
                calculated_at,
                updated_at
            FROM staging_jobs
            WHERE state = ?
            ORDER BY
                target_date ASC,
                job_id ASC
        """

        rows = self.connection.execute(
            query,
            (state,),
        ).fetchall()

        return [
            self._row_to_model(row)
            for row in rows
        ]

    # =========================================================
    # Delete
    # =========================================================

    def delete(self, job_id):

        self.connection.execute(
            """
            DELETE FROM staging_jobs
            WHERE job_id = ?
            """,
            (job_id,),
        )

        self.connection.commit()

    # =========================================================
    # Clear
    # =========================================================

    def clear(self):

        self.connection.execute(
            "DELETE FROM staging_jobs"
        )

        self.connection.commit()

    # =========================================================
    # Count
    # =========================================================

    def count(self):

        row = self.connection.execute(
            "SELECT COUNT(*) FROM staging_jobs"
        ).fetchone()

        return row[0]

    # =========================================================
    # Row -> model
    # =========================================================

    @staticmethod
    def _row_to_model(row):

        (
            job_id,
            job_name,
            state,
            occurrence_date,
            execution_date,
            t_date,
            report_date,
            target_date,
            margin,
            confirmation_required,
            confirmation_status,
            time_flag,
            from_time,
            to_time,
            waiting_for,
            reason,
            next_evaluation,
            calculated_at,
            updated_at,
        ) = row

        return StagingJob(
            job_id=job_id,
            job_name=job_name,
            state=state,
            occurrence_date=occurrence_date,
            execution_date=execution_date,
            t_date=t_date,
            report_date=report_date,
            target_date=target_date,
            margin=margin,
            confirmation_required=confirmation_required,
            confirmation_status=confirmation_status,
            time_flag=time_flag,
            from_time=from_time,
            to_time=to_time,
            waiting_for=waiting_for,
            reason=reason,
            next_evaluation=next_evaluation,
            calculated_at=calculated_at,
            updated_at=updated_at,
        )