class ReadyJob:
    """
    Represents a job that has passed all eligibility gates
    and is currently READY for execution.

    This is a plain Python model.
    It is NOT a Django ORM model.

    The READY record keeps the complete scheduling context for the
    occurrence so that execution does not lose the distinction between:

        occurrence_date -> business occurrence
        execution_date  -> date on which execution is allowed
        t_date          -> DATEMAST T date
        report_date     -> date passed to Oracle
        target_date     -> fixed T + margin target
    """

    def __init__(
        self,
        occurrence_key=None,
        job_id=None,
        job_name=None,
        occurrence_date=None,
        execution_date=None,
        t_date=None,
        report_date=None,
        target_date=None,
        ready_since=None,

        # Priority information
        time_priority=0,
        date_priority=0,
        job_priority=0,
        priority_key=None,

        # Scheduling window
        from_time=None,
        to_time=None,

        # Scheduler state
        time_state=None,

        updated_at=None,
    ):
        # ``occurrence_key`` distinguishes simultaneous/overlapping
        # scheduled report dates belonging to the same master job.
        self.occurrence_key = occurrence_key
        self.job_id = job_id
        self.job_name = job_name

        # Scheduled occurrence represented by this READY record.
        self.occurrence_date = occurrence_date

        # Actual date on which this occurrence is allowed to execute.
        # This is deliberately separate from report_date.
        self.execution_date = execution_date

        # DATEMAST T date used for this occurrence.
        self.t_date = t_date

        # Business/report date passed to Oracle.
        self.report_date = report_date

        # Fixed T + margin target for this occurrence.
        self.target_date = target_date

        self.ready_since = ready_since

        # -----------------------------------------------------
        # Priority
        # -----------------------------------------------------

        self.time_priority = time_priority
        self.date_priority = date_priority
        self.job_priority = job_priority
        self.priority_key = priority_key

        # -----------------------------------------------------
        # RUN_BY window
        # -----------------------------------------------------

        self.from_time = from_time
        self.to_time = to_time

        self.time_state = time_state

        self.updated_at = updated_at

    def __repr__(self):
        return (
            f"ReadyJob("
            f"occurrence_key={self.occurrence_key!r}, "
            f"job_id={self.job_id}, "
            f"job_name={self.job_name!r}, "
            f"occurrence_date={self.occurrence_date!r}, "
            f"execution_date={self.execution_date!r}, "
            f"t_date={self.t_date!r}, "
            f"report_date={self.report_date!r}, "
            f"target_date={self.target_date!r}, "
            f"priority_key={self.priority_key!r}"
            f")"
        )
