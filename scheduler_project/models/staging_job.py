class StagingJob:
    """
    Persistent scheduler state for a job that is not yet READY.

    A staging record represents one specific scheduled occurrence.
    The occurrence, T date, target date, report date, and execution
    date belong to that occurrence and must survive repeated
    scheduler cycles.
    """

    def __init__(
        self,
        occurrence_key=None,
        job_id=None,
        job_name=None,
        state=None,
        occurrence_date=None,
        execution_date=None,
        t_date=None,
        report_date=None,
        target_date=None,
        margin=None,
        confirmation_required=0,
        confirmation_status=None,
        time_flag=0,
        from_time=None,
        to_time=None,
        waiting_for=None,
        reason=None,
        next_evaluation=None,
        calculated_at=None,
        updated_at=None,
    ):
        # Stable identity for one scheduled report-date occurrence.  A master
        # job can create more than one occurrence (for example DAILY and
        # FORTNIGHTLY on the same scheduler date), so ``job_id`` alone is not
        # a sufficient persistence key.
        self.occurrence_key = occurrence_key
        self.job_id = job_id
        self.job_name = job_name
        self.state = state

        # Scheduled occurrence this staging record belongs to.
        self.occurrence_date = occurrence_date

        # Actual date on which this occurrence is allowed to execute.
        #
        # This is deliberately separate from occurrence_date/report_date.
        # For SAME_DAY=0, for example:
        #
        #     occurrence_date = 2026-09-15
        #     execution_date = 2026-09-17
        #
        # The Oracle procedure must still receive report_date=2026-09-15.
        self.execution_date = execution_date

        # DATEMAST T date used to calculate target_date.
        self.t_date = t_date

        # Business/report date associated with this occurrence.
        # This must not be replaced by the latest DATEMAST date while
        # the occurrence is waiting for its threshold.
        self.report_date = report_date

        # Fixed target date for this occurrence.
        self.target_date = target_date

        self.margin = margin

        self.confirmation_required = confirmation_required
        self.confirmation_status = confirmation_status

        self.time_flag = time_flag
        self.from_time = from_time
        self.to_time = to_time

        self.waiting_for = waiting_for
        self.reason = reason
        self.next_evaluation = next_evaluation

        self.calculated_at = calculated_at
        self.updated_at = updated_at

    def __repr__(self):
        return (
            f"StagingJob("
            f"occurrence_key={self.occurrence_key!r}, "
            f"job_id={self.job_id}, "
            f"job_name={self.job_name!r}, "
            f"state={self.state!r}, "
            f"occurrence_date={self.occurrence_date!r}, "
            f"execution_date={self.execution_date!r}, "
            f"t_date={self.t_date!r}, "
            f"report_date={self.report_date!r}, "
            f"target_date={self.target_date!r}"
            f")"
        )
