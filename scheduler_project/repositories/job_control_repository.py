from datetime import datetime
from datetime_compat import parse_iso_datetime
from contextlib import contextmanager
from repositories.worker_logging import atomic_logging

from database.sqlite_db import get_connection


class JobControlRepository:
    """
    Repository for authoritative manual/control state of jobs.

    Job control is intentionally separate from:

        - Schedule Master
        - STAGING
        - READY
        - Priority Queue

    Users modify control state.

    The scheduler reads this state and recalculates the job.

    The heap is never directly manipulated by this repository.
    """

    def __init__(self, connection=None):
        self.connection = connection
        self._transaction_depth = 0

    @contextmanager
    def transaction(self):
        """Commit a control mutation and its audit together on the shared DB."""
        if self.connection is None:
            raise ValueError("Control transactions require a shared SQLite connection.")
        self._transaction_depth += 1
        try:
            with atomic_logging(self.connection):
                yield self
        finally:
            self._transaction_depth -= 1

    def _commit(self, connection):
        if not self._transaction_depth:
            connection.commit()

    @contextmanager
    def _transaction_context(self, connection):
        if self._transaction_depth:
            yield
        else:
            with atomic_logging(connection):
                yield

    # =========================================================
    # Connection
    # =========================================================

    def _get_connection(self):
        """
        Return the supplied connection when available.

        Otherwise create a temporary SQLite connection.
        """

        if self.connection is not None:
            return self.connection

        return get_connection()

    # =========================================================
    # Time
    # =========================================================

    @staticmethod
    def _now():
        return datetime.now().isoformat(
            timespec="seconds"
        )

    # =========================================================
    # Get
    # =========================================================

    def get(self, job_id):
        """
        Return the complete control state for one job.

        Returns
        -------
        dict | None
        """

        connection = self._get_connection()
        close_connection = (
            connection is not self.connection
        )

        try:
            row = connection.execute(
                """
                SELECT
                    job_id,
                    control_status,
                    manual_run,
                    confirmation,
                    override_datetime,
                    updated_at
                FROM job_control
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()

            if row is None:
                return None

            return dict(row)

        finally:
            if close_connection:
                connection.close()

    # =========================================================
    # Get all
    # =========================================================

    def get_all(self):
        """
        Return control state for all jobs.
        """

        connection = self._get_connection()
        close_connection = (
            connection is not self.connection
        )

        try:
            rows = connection.execute(
                """
                SELECT
                    job_id,
                    control_status,
                    manual_run,
                    confirmation,
                    override_datetime,
                    updated_at
                FROM job_control
                ORDER BY job_id
                """
            ).fetchall()

            return [
                dict(row)
                for row in rows
            ]

        finally:
            if close_connection:
                connection.close()

    # =========================================================
    # Initialize
    # =========================================================

    def ensure_job(self, job_id):
        """
        Ensure a control record exists.

        New jobs start in ACTIVE state.
        """

        connection = self._get_connection()
        close_connection = (
            connection is not self.connection
        )

        try:
            now = self._now()

            connection.execute(
                """
                INSERT INTO job_control (
                    job_id,
                    control_status,
                    manual_run,
                    confirmation,
                    override_datetime,
                    updated_at
                )
                VALUES (
                    ?,
                    'ACTIVE',
                    0,
                    0,
                    NULL,
                    ?
                )
                ON CONFLICT(job_id)
                DO NOTHING
                """,
                (
                    job_id,
                    now,
                ),
            )

            self._commit(connection)

            return self.get(
                job_id
            )

        finally:
            if close_connection:
                connection.close()

    # =========================================================
    # Pause
    # =========================================================

    def pause(self, job_id):
        """
        Pause a job.

        The scheduler will see PAUSED during the next
        scheduling cycle.
        """

        return self._update(
            job_id=job_id,
            control_status="PAUSED",
        )

    # =========================================================
    # Resume
    # =========================================================

    def resume(self, job_id):
        """
        Resume a paused job.

        The scheduler will reevaluate the job during the
        next scheduling cycle.
        """

        return self._update(
            job_id=job_id,
            control_status="ACTIVE",
        )

    # =========================================================
    # Manual run
    # =========================================================

    def request_manual_run(self, job_id):
        """
        Request a manual run.

        This does NOT execute the job.

        It only records the request. The scheduler processes
        the request during its next scheduling cycle.
        """

        return self._update(
            job_id=job_id,
            manual_run=1,
        )

    # =========================================================
    # Clear manual run
    # =========================================================

    def clear_manual_run(self, job_id):
        """
        Clear a pending manual-run request.
        """

        return self._update(
            job_id=job_id,
            manual_run=0,
        )

    # =========================================================
    # Confirmation
    # =========================================================

    def confirm(self, job_id):
        """
        Confirm a confirmation-required job.

        The scheduler will reevaluate the job during the next
        scheduling cycle.
        """

        return self._update(
            job_id=job_id,
            confirmation=1,
        )

    def set_occurrence_confirmation(self, job_id, occurrence_key, confirmed):
        """Approve one durable report date; never authorize a sibling/day."""
        connection = self._get_connection()
        try:
            with self._transaction_context(connection):
                connection.execute(
                    """INSERT INTO occurrence_confirmation
                           (occurrence_key, job_id, confirmed, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(occurrence_key) DO UPDATE SET
                           confirmed=excluded.confirmed, updated_at=excluded.updated_at""",
                    (str(occurrence_key), int(job_id), int(bool(confirmed)), self._now()),
                )
                # Scoped control must never leave the legacy global latch set.
                connection.execute(
                    "UPDATE job_control SET confirmation=0, updated_at=? WHERE job_id=?",
                    (self._now(), int(job_id)),
                )
            return self.get_for_occurrence(job_id, occurrence_key)
        finally:
            if connection is not self.connection:
                connection.close()

    def get_for_occurrence(self, job_id, occurrence_key):
        """Apply the occurrence approval to the job's other control fields."""
        control = self.get(job_id) or {"job_id": int(job_id), "control_status": "ACTIVE"}
        connection = self._get_connection()
        try:
            row = connection.execute(
                "SELECT confirmed FROM occurrence_confirmation WHERE occurrence_key=? AND job_id=?",
                (str(occurrence_key), int(job_id)),
            ).fetchone()
            control["confirmation"] = int(row["confirmed"]) if row is not None else 0
            control["occurrence_key"] = str(occurrence_key)
            return control
        finally:
            if connection is not self.connection:
                connection.close()

    def get_occurrence_confirmations(self):
        connection = self._get_connection()
        try:
            return [dict(row) for row in connection.execute(
                "SELECT occurrence_key, job_id, confirmed, updated_at FROM occurrence_confirmation"
            ).fetchall()]
        finally:
            if connection is not self.connection:
                connection.close()

    # =========================================================
    # Clear confirmation
    # =========================================================

    def clear_confirmation(self, job_id):
        """
        Remove confirmation from a job.
        """

        return self._update(
            job_id=job_id,
            confirmation=0,
        )

    # =========================================================
    # Cancel
    # =========================================================

    def cancel(self, job_id):
        """
        Cancel a job.

        Cancellation is represented through the same
        authoritative control state used by the scheduler.
        """

        return self._update(
            job_id=job_id,
            control_status="CANCELLED",
        )

    # =========================================================
    # Activate
    # =========================================================

    def activate(self, job_id):
        """
        Activate a previously cancelled job.

        The scheduler will reevaluate it during the next
        scheduling cycle.
        """

        return self._update(
            job_id=job_id,
            control_status="ACTIVE",
        )

    # =========================================================
    # Datetime override
    # =========================================================

    def set_override_datetime(
        self,
        job_id,
        override_datetime,
    ):
        """
        Set a job-specific scheduler datetime override.

        The scheduler uses this value instead of the normal
        scheduler cycle datetime for this job.

        The value is stored as an ISO datetime string.
        """

        if override_datetime is None:
            return self.clear_override_datetime(
                job_id
            )

        override_datetime = (
            self._normalize_override_datetime(
                override_datetime
            )
        )

        return self._update(
            job_id=job_id,
            override_datetime=override_datetime,
        )

    # =========================================================
    # Clear datetime override
    # =========================================================

    def clear_override_datetime(self, job_id):
        """
        Explicitly clear the datetime override.

        This must not use _update(), because None means
        "argument not supplied" in that method.
        """

        connection = self._get_connection()
        close_connection = (
            connection is not self.connection
        )

        try:
            self._ensure_job_on_connection(
                connection,
                job_id,
            )

            connection.execute(
                """
                UPDATE job_control
                SET
                    override_datetime = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    self._now(),
                    job_id,
                ),
            )

            self._commit(connection)

            return self._get_from_connection(
                connection,
                job_id,
            )

        finally:
            if close_connection:
                connection.close()

    # =========================================================
    # Backward-compatible alias
    # =========================================================

    def clear_override(self, job_id):
        """
        Backward-compatible alias.

        New code should use clear_override_datetime().
        """

        return self.clear_override_datetime(
            job_id
        )

    # =========================================================
    # Reset
    # =========================================================

    def reset(self, job_id):
        """
        Reset all temporary scheduler controls.

        Result:

            control_status    = ACTIVE
            manual_run        = 0
            confirmation      = 0
            override_datetime = NULL

        Schedule Master is not modified.
        """

        connection = self._get_connection()
        close_connection = (
            connection is not self.connection
        )

        try:
            self._ensure_job_on_connection(
                connection,
                job_id,
            )

            connection.execute(
                """
                UPDATE job_control
                SET
                    control_status = 'ACTIVE',
                    manual_run = 0,
                    confirmation = 0,
                    override_datetime = NULL,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    self._now(),
                    job_id,
                ),
            )

            connection.execute(
                "UPDATE occurrence_confirmation SET confirmed=0, updated_at=? WHERE job_id=?",
                (self._now(), job_id),
            )
            self._commit(connection)

            return self._get_from_connection(
                connection,
                job_id,
            )

        finally:
            if close_connection:
                connection.close()

    # =========================================================
    # Generic update
    # =========================================================

    def _update(
        self,
        job_id,
        control_status=None,
        manual_run=None,
        confirmation=None,
        override_datetime=None,
    ):
        """
        Update only values supplied by the caller.

        None means "not supplied" here.

        Therefore clearing override_datetime must use
        clear_override_datetime() rather than this method.
        """

        connection = self._get_connection()
        close_connection = (
            connection is not self.connection
        )

        try:
            self._ensure_job_on_connection(
                connection,
                job_id,
            )

            fields = []
            values = []

            # -------------------------------------------------
            # Control status
            # -------------------------------------------------

            if control_status is not None:
                fields.append(
                    "control_status = ?"
                )
                values.append(
                    control_status
                )

            # -------------------------------------------------
            # Manual run
            # -------------------------------------------------

            if manual_run is not None:
                fields.append(
                    "manual_run = ?"
                )
                values.append(
                    manual_run
                )

            # -------------------------------------------------
            # Confirmation
            # -------------------------------------------------

            if confirmation is not None:
                fields.append(
                    "confirmation = ?"
                )
                values.append(
                    confirmation
                )

            # -------------------------------------------------
            # Datetime override
            # -------------------------------------------------

            if override_datetime is not None:
                fields.append(
                    "override_datetime = ?"
                )
                values.append(
                    override_datetime
                )

            # -------------------------------------------------
            # Nothing to update
            # -------------------------------------------------

            if not fields:
                return self._get_from_connection(
                    connection,
                    job_id,
                )

            # -------------------------------------------------
            # Updated timestamp
            # -------------------------------------------------

            fields.append(
                "updated_at = ?"
            )

            values.append(
                self._now()
            )

            values.append(
                job_id
            )

            sql = f"""
                UPDATE job_control
                SET
                    {", ".join(fields)}
                WHERE job_id = ?
            """

            connection.execute(
                sql,
                values,
            )

            self._commit(connection)

            return self._get_from_connection(
                connection,
                job_id,
            )

        finally:
            if close_connection:
                connection.close()

    # =========================================================
    # Internal ensure using existing connection
    # =========================================================

    def _ensure_job_on_connection(
        self,
        connection,
        job_id,
    ):
        """
        Ensure a job-control row exists using the supplied
        connection.

        This avoids opening a second connection when the
        repository already owns a connection.
        """

        connection.execute(
            """
            INSERT INTO job_control (
                job_id,
                control_status,
                manual_run,
                confirmation,
                override_datetime,
                updated_at
            )
            VALUES (
                ?,
                'ACTIVE',
                0,
                0,
                NULL,
                ?
            )
            ON CONFLICT(job_id)
            DO NOTHING
            """,
            (
                job_id,
                self._now(),
            ),
        )

        self._commit(connection)

    # =========================================================
    # Internal get using existing connection
    # =========================================================

    @staticmethod
    def _get_from_connection(
        connection,
        job_id,
    ):
        """
        Read one control record using an existing connection.
        """

        row = connection.execute(
            """
            SELECT
                job_id,
                control_status,
                manual_run,
                confirmation,
                override_datetime,
                updated_at
            FROM job_control
            WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()

        if row is None:
            return None

        return dict(row)

    # =========================================================
    # Datetime normalization
    # =========================================================

    @staticmethod
    def _normalize_override_datetime(
        value,
    ):
        """
        Normalize a datetime override to an ISO string.

        Accepts:

            datetime
            ISO datetime string
        """

        if isinstance(
            value,
            datetime,
        ):
            return value.isoformat(
                timespec="seconds"
            )

        if isinstance(
            value,
            str,
        ):

            value = value.strip()

            if not value:
                raise ValueError(
                    "override_datetime cannot be empty."
                )

            try:
                parsed = (
                    parse_iso_datetime(
                        value
                    )
                )

            except ValueError as exc:
                raise ValueError(
                    "override_datetime must be a valid "
                    "ISO datetime."
                ) from exc

            return parsed.isoformat(
                timespec="seconds"
            )

        raise TypeError(
            "override_datetime must be a datetime "
            "or ISO datetime string."
        )
