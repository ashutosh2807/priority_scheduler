class JobControl:
    """
    High-level control interface for scheduler jobs.

    All user-facing scheduler control operations go through
    this class.

    Supported operations:

        - pause
        - resume
        - manual run
        - clear manual run
        - confirmation
        - clear confirmation
        - cancel
        - activate
        - datetime override
        - clear datetime override
        - reset

    Important
    ---------
    This class does not manipulate:

        - STAGING
        - READY
        - Priority Queue

    directly.

    It changes authoritative control state only.

    The scheduler observes the control state during its next
    scheduling cycle and recalculates the job.
    """

    def __init__(self, repository):
        self.repository = repository

    # =========================================================
    # Pause / Resume
    # =========================================================

    def pause(self, job_id):
        """
        Pause a job.

        The scheduler will not allow a paused job to become
        READY.
        """

        return self.repository.pause(
            job_id
        )

    def resume(self, job_id):
        """
        Resume a previously paused job.

        The scheduler will reevaluate the job during the next
        scheduling cycle.
        """

        return self.repository.resume(
            job_id
        )

    # =========================================================
    # Manual Run
    # =========================================================

    def manual_run(self, job_id):
        """
        Request a manual execution.

        The job is NOT inserted directly into the heap.

        Instead, manual_run is stored as authoritative control
        state and the scheduler processes it during its next
        cycle.
        """

        return self.repository.request_manual_run(
            job_id
        )

    def clear_manual_run(self, job_id):
        """
        Clear a pending manual-run request.
        """

        return self.repository.clear_manual_run(
            job_id
        )

    # =========================================================
    # Confirmation
    # =========================================================

    def confirm(self, job_id):
        """
        Confirm a job that requires confirmation.

        The scheduler will reevaluate the job during the next
        scheduling cycle.
        """

        return self.repository.confirm(
            job_id
        )

    def clear_confirmation(self, job_id):
        """
        Remove an existing confirmation.
        """

        return self.repository.clear_confirmation(
            job_id
        )

    # =========================================================
    # Cancel
    # =========================================================

    def cancel(self, job_id):
        """
        Cancel a job.

        Cancellation is represented by the authoritative
        control state.

        The scheduler will process the cancellation during
        its next cycle.
        """

        return self.repository.cancel(
            job_id
        )

    # =========================================================
    # Activate
    # =========================================================

    def activate(self, job_id):
        """
        Activate a cancelled job again.

        The scheduler will reevaluate the job from the
        beginning during its next cycle.
        """

        return self.repository.activate(
            job_id
        )

    # =========================================================
    # Datetime Override
    # =========================================================

    def set_datetime_override(
        self,
        job_id,
        override_datetime,
    ):
        """
        Set the datetime used by the scheduler for this
        particular job.

        The override does not change the operating-system
        clock.

        It only changes the scheduling/evaluation datetime
        for this job.

        Accepted values:

            datetime object
            ISO formatted datetime string
        """

        return self.repository.set_override_datetime(
            job_id,
            override_datetime,
        )

    def clear_datetime_override(
        self,
        job_id,
    ):
        """
        Remove an existing datetime override.

        The job will return to using the normal scheduler
        cycle datetime.
        """

        return self.repository.clear_override_datetime(
            job_id
        )

    # =========================================================
    # State Access
    # =========================================================

    def get(self, job_id):
        """
        Return the complete control state for one job.
        """

        return self.repository.get(
            job_id
        )

    def get_all(self):
        """
        Return control state for all jobs.
        """

        return self.repository.get_all()

    # =========================================================
    # Initialization
    # =========================================================

    def ensure_job(self, job_id):
        """
        Ensure that a control record exists for the job.

        New jobs are ACTIVE by default.
        """

        return self.repository.ensure_job(
            job_id
        )

    # =========================================================
    # Reset
    # =========================================================

    def reset(self, job_id):
        """
        Reset all temporary scheduler controls.

        The resulting state is:

            control_status    = ACTIVE
            manual_run        = 0
            confirmation      = 0
            override_datetime = NULL

        Schedule Master is not modified.

        STAGING and READY are not directly manipulated here.
        The scheduler will reevaluate the job on its next cycle.
        """

        return self.repository.reset(
            job_id
        )