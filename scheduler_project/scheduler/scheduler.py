from datetime import datetime, date
from datetime_compat import parse_iso_datetime


from scheduler.eligibility import EligibilityResult


class Scheduler:
    """
    Main scheduler orchestrator.

    Flow:

        Schedule Master
              ↓
        Job Control
              ↓
        Persistent Occurrence
              ↓
        Eligibility Evaluation
              ↓
        STAGING / READY
              ↓
        Priority Calculation
              ↓
        Priority Heap
              ↓
        Later Execution

    Execution is intentionally outside this class.

    STAGING is persistent state.

    Once an occurrence has been staged, its:

        occurrence_date
        t_date
        target_date

    are preserved across scheduler cycles.

    A new T/target is calculated only when the scheduler
    advances to a genuinely new occurrence.

    Datetime overrides are job-specific. If a job has an
    override_datetime in Job Control, that datetime is used
    for that job's scheduling evaluation. Other jobs continue
    to use the scheduler cycle datetime.
    """

    def __init__(
        self,
        schedule_master_repository,
        job_control_repository,
        eligibility_evaluator,
        staging_manager,
        priority_calculator,
        ready_repository,
        priority_queue,
        datemast=None,
        frequency_evaluator=None,
    ):
        self.schedule_master_repository = (
            schedule_master_repository
        )

        self.job_control_repository = (
            job_control_repository
        )

        self.eligibility_evaluator = (
            eligibility_evaluator
        )

        self.staging_manager = (
            staging_manager
        )

        self.priority_calculator = (
            priority_calculator
        )

        self.ready_repository = (
            ready_repository
        )

        self.priority_queue = (
            priority_queue
        )

        self.datemast = datemast

        # Prefer the same FrequencyEvaluator instance used
        # by EligibilityEvaluator.
        self.frequency_evaluator = (
            frequency_evaluator
            or getattr(
                eligibility_evaluator,
                "frequency_evaluator",
                None,
            )
        )

    # =========================================================
    # Main cycle
    # =========================================================

    def run_cycle(
        self,
        current_datetime=None,
    ):
        """
        Execute one complete scheduling cycle.

        The scheduler is expected to call this every 3 minutes.

        Steps:

            1. Load active Schedule Master jobs.
            2. Ensure Job Control records exist.
            3. Load controls.
            4. Determine each job's effective datetime.
            5. Determine persistent occurrence.
            6. Evaluate eligibility.
            7. Persist STAGING / READY.
            8. Recalculate READY priorities.
            9. Rebuild the derived priority heap.
           10. Return monitoring summary.

        current_datetime is the scheduler's global cycle
        datetime.

        A job-specific Job Control override_datetime takes
        precedence for that job only.
        """

        if current_datetime is None:
            current_datetime = datetime.now()

        current_datetime = (
            self._to_datetime(
                current_datetime
            )
        )

        jobs = (
            self.schedule_master_repository
            .get_active()
        )

        # -----------------------------------------------------
        # Ensure control records exist.
        # -----------------------------------------------------

        for job in jobs:
            self.job_control_repository.ensure_job(
                job.id
            )

        # -----------------------------------------------------
        # Load controls.
        # -----------------------------------------------------

        controls = {}

        for job in jobs:
            controls[job.id] = (
                self.job_control_repository.get(
                    job.id
                )
            )

        results = {}

        # -----------------------------------------------------
        # Evaluate every active job.
        # -----------------------------------------------------

        for job in jobs:

            control = controls.get(
                job.id
            )

            # -------------------------------------------------
            # Determine the effective datetime for this job.
            #
            # Normally:
            #
            #     effective_datetime = cycle datetime
            #
            # With override:
            #
            #     effective_datetime = override datetime
            # -------------------------------------------------

            effective_datetime = (
                self._get_effective_datetime(
                    control=control,
                    fallback_datetime=current_datetime,
                )
            )

            effective_date = (
                effective_datetime.date()
            )

            # -------------------------------------------------
            # Manual run against an existing READY occurrence.
            #
            # A scheduled occurrence may already be READY because
            # an earlier scheduled execution failed. A subsequent
            # manual run is an execution request for that SAME
            # occurrence; it must not create a new occurrence or
            # replace the business report_date.
            #
            # Since READY is already executable state, there is
            # nothing further for EligibilityEvaluator to calculate
            # here. The existing READY record is preserved and the
            # ExecutionManager will consume the manual request.
            # -------------------------------------------------
            manual_run = self._is_manual_run(
                control
            )

            if manual_run:
                existing_ready = (
                    self.ready_repository.get_by_id(
                        job.id
                    )
                )

                if existing_ready is not None:
                    results[job.id] = EligibilityResult(
                        state="READY",
                        eligible=True,
                        occurrence_date=(
                            self._to_date(
                                getattr(
                                    existing_ready,
                                    "occurrence_date",
                                    None,
                                )
                            )
                        ),
                        execution_date=(
                            self._to_date(
                                getattr(
                                    existing_ready,
                                    "execution_date",
                                    None,
                                )
                            )
                        ),
                        t_date=(
                            self._to_date(
                                getattr(
                                    existing_ready,
                                    "t_date",
                                    None,
                                )
                            )
                        ),
                        report_date=(
                            self._to_date(
                                getattr(
                                    existing_ready,
                                    "report_date",
                                    None,
                                )
                            )
                        ),
                        target_date=(
                            self._to_date(
                                getattr(
                                    existing_ready,
                                    "target_date",
                                    None,
                                )
                            )
                        ),
                        reason=(
                            "Existing READY occurrence "
                            "preserved for manual execution."
                        ),
                        waiting_for=None,
                        from_time=(
                            getattr(
                                existing_ready,
                                "from_time",
                                None,
                            )
                        ),
                        to_time=(
                            getattr(
                                existing_ready,
                                "to_time",
                                None,
                            )
                        ),
                        time_window_state=(
                            getattr(
                                existing_ready,
                                "time_state",
                                None,
                            )
                        ),
                        confirmation_required=False,
                        confirmation_status=None,
                        frequency_matched=True,
                        day_allowed=True,
                        datemast_ready=True,
                    )

                    continue

            # -------------------------------------------------
            # Read persistent STAGING state.
            # -------------------------------------------------

            staging_job = (
                self.staging_manager
                .staging_repository
                .get_by_id(
                    job.id
                )
            )

            occurrence_date = None
            persisted_t_date = None
            persisted_target_date = None

            # -------------------------------------------------
            # Existing STAGING occurrence.
            # -------------------------------------------------

            if staging_job is not None:

                occurrence_date = (
                    self._to_date(
                        staging_job.occurrence_date
                    )
                )

                persisted_t_date = (
                    self._to_date(
                        staging_job.t_date
                    )
                )

                persisted_target_date = (
                    self._to_date(
                        staging_job.target_date
                    )
                )

                # -------------------------------------------------
                # Expired RUN_BY occurrence.
                #
                # Do NOT immediately replace it with a future
                # occurrence.
                #
                # Example:
                #
                #     effective date = 08-Sep
                #     occurrence     = 08-Sep
                #     RUN_BY         = 10:00-11:00
                #
                # After 11:00 the occurrence becomes:
                #
                #     STAGING
                #     waiting_for = NEXT_OCCURRENCE
                #
                # If next occurrence is 09-Sep, retain the
                # 08-Sep occurrence until 09-Sep actually
                # arrives.
                #
                # With a datetime override, "arrives" is
                # determined using the overridden date.
                # -------------------------------------------------

                if (
                    staging_job.waiting_for
                    == "NEXT_OCCURRENCE"
                ):

                    next_occurrence = (
                        self._find_next_occurrence(
                            job=job,
                            occurrence_date=(
                                occurrence_date
                            ),
                        )
                    )

                    # -------------------------------------------------
                    # Advance only when the next occurrence has
                    # actually arrived.
                    # -------------------------------------------------

                    if (
                        next_occurrence is not None
                        and next_occurrence
                        <= effective_date
                    ):

                        occurrence_date = (
                            next_occurrence
                        )

                        # -------------------------------------------------
                        # Genuinely new occurrence.
                        #
                        # Its T/target must be recalculated.
                        # -------------------------------------------------

                        persisted_t_date = None
                        persisted_target_date = None

                        self.staging_manager \
                            .staging_repository \
                            .delete(
                                job.id
                            )

                        staging_job = None

            # -------------------------------------------------
            # Manual run handling.
            # -------------------------------------------------
            #
            # A manual run is a control request, not a scheduled
            # frequency occurrence. Therefore it must not depend
            # on FrequencyEvaluator finding an occurrence first.
            #
            # When there is no existing persistent occurrence, use
            # the effective current date as the manual occurrence.
            # This allows EligibilityEvaluator to receive the
            # manual-run request instead of the scheduler stopping
            # at the "No scheduled occurrence found" branch.
            #
            # Confirmation remains a hard gate. If the job requires
            # confirmation and has not been confirmed, keep the
            # manual request in WAITING_CONFIRMATION rather than
            # allowing the manual path to become READY.
            # -------------------------------------------------

            if (
                manual_run
                and staging_job is None
            ):
                occurrence_date = effective_date
                persisted_t_date = None
                persisted_target_date = None

            # -------------------------------------------------
            # No persistent STAGING occurrence.
            # -------------------------------------------------

            if staging_job is None:

                occurrence_date = (
                    occurrence_date
                    or self._find_occurrence(
                        job=job,
                        current_date=effective_date,
                    )
                )

                # A newly selected occurrence must not inherit
                # another occurrence's T/target.

                if occurrence_date is not None:
                    persisted_t_date = None
                    persisted_target_date = None

            # -------------------------------------------------
            # Manual run + confirmation gate.
            # -------------------------------------------------

            if (
                manual_run
                and self._confirmation_is_blocked(
                    job=job,
                    control=control,
                )
            ):
                result = self._build_waiting_confirmation_result(
                    job=job,
                    control=control,
                    occurrence_date=occurrence_date,
                )

            # -------------------------------------------------
            # No occurrence found.
            # -------------------------------------------------

            elif occurrence_date is None:

                result = EligibilityResult(
                    state="STAGING",
                    eligible=False,
                    occurrence_date=None,
                    t_date=None,
                    report_date=None,
                    target_date=None,
                    reason=(
                        "No scheduled occurrence "
                        "found."
                    ),
                    waiting_for="FREQUENCY",
                    frequency_matched=False,
                    day_allowed=False,
                    datemast_ready=False,
                )

            else:

                # -------------------------------------------------
                # Evaluate the occurrence.
                #
                # Existing occurrence:
                #
                #     persisted_t_date
                #     persisted_target_date
                #
                # are supplied so they are preserved.
                #
                # New occurrence:
                #
                #     both are None
                #
                # allowing EligibilityEvaluator to establish T
                # and calculate target_date.
                # -------------------------------------------------

                result = (
                    self.eligibility_evaluator.evaluate(
                        job=job,
                        current_datetime=(
                            effective_datetime
                        ),
                        control=control,
                        datemast=self.datemast,
                        occurrence_date=(
                            occurrence_date
                        ),
                        persisted_t_date=(
                            persisted_t_date
                        ),
                        persisted_target_date=(
                            persisted_target_date
                        ),
                    )
                )

            results[job.id] = result

            # -------------------------------------------------
            # Persist STAGING / READY.
            #
            # Persist using the effective datetime because
            # this evaluation may be based on a job-specific
            # override.
            # -------------------------------------------------

            self.staging_manager.synchronize(
                job,
                result,
                effective_datetime,
            )

        # -----------------------------------------------------
        # Refresh READY priorities.
        #
        # Each READY job gets its own effective datetime.
        # -----------------------------------------------------

        self._refresh_ready_priorities(
            jobs=jobs,
            controls=controls,
            current_datetime=current_datetime,
        )

        # -----------------------------------------------------
        # Rebuild derived priority heap.
        #
        # READY repository is the persistent source of truth.
        # -----------------------------------------------------

        ready_jobs = (
            self.ready_repository.get_all()
        )

        self.priority_queue.rebuild(
            ready_jobs
        )

        # -----------------------------------------------------
        # Return monitoring summary.
        # -----------------------------------------------------

        return self._build_summary(
            jobs=jobs,
            results=results,
            current_datetime=current_datetime,
        )

    # =========================================================
    # Occurrence selection
    # =========================================================

    def _find_occurrence(
        self,
        job,
        current_date,
    ):
        """
        Find the most recent scheduled occurrence.

        This is used only when there is no persistent STAGING
        occurrence.

        SAME_DAY jobs:

            occurrence = current date

        Other jobs:

            FrequencyEvaluator determines the occurrence.
        """

        current_date = (
            self._to_date(
                current_date
            )
        )

        # -----------------------------------------------------
        # SAME_DAY
        # -----------------------------------------------------

        same_day = self._to_bool(
            getattr(
                job,
                "same_day",
                0,
            )
        )

        if same_day:
            return current_date

        # -----------------------------------------------------
        # Frequency evaluator
        # -----------------------------------------------------

        if self.frequency_evaluator is None:
            return None

        return (
            self.frequency_evaluator
            .get_previous_occurrence(
                job=job,
                current_date=current_date,
                include_current=True,
            )
        )

    # =========================================================
    # Next occurrence
    # =========================================================

    def _find_next_occurrence(
        self,
        job,
        occurrence_date,
    ):
        """
        Find the next configured occurrence after the
        supplied occurrence.

        The result is only a candidate.

        The main scheduler decides when that occurrence is
        actually allowed to replace the persistent occurrence.
        """

        if occurrence_date is None:
            return None

        if self.frequency_evaluator is None:
            return None

        occurrence_date = (
            self._to_date(
                occurrence_date
            )
        )

        next_occurrence = (
            self.frequency_evaluator
            .get_next_occurrence(
                job=job,
                current_date=occurrence_date,
            )
        )

        if next_occurrence is None:
            return None

        next_occurrence = (
            self._to_date(
                next_occurrence
            )
        )

        # Never move backwards.

        if (
            next_occurrence
            <= occurrence_date
        ):
            return None

        return next_occurrence

    # =========================================================
    # READY priority refresh
    # =========================================================

    def _refresh_ready_priorities(
        self,
        jobs,
        controls,
        current_datetime,
    ):
        """
        Recalculate priority for every persistent READY job.

        This must happen every scheduler cycle because
        RUN_BY priority is dynamic.

        Each READY job uses its own effective datetime.

        Example:

            Job A override = 10:30
            Job B override = no override

        Job A is evaluated at 10:30 while Job B is evaluated
        using the real scheduler cycle datetime.
        """

        job_map = {
            job.id: job
            for job in jobs
        }

        ready_jobs = (
            self.ready_repository.get_all()
        )

        for ready_job in ready_jobs:

            job = job_map.get(
                ready_job.job_id
            )

            # -------------------------------------------------
            # Job is no longer active.
            # -------------------------------------------------

            if job is None:

                self.ready_repository.delete(
                    ready_job.job_id
                )

                continue

            # -------------------------------------------------
            # Determine effective datetime.
            # -------------------------------------------------

            control = controls.get(
                ready_job.job_id
            )

            effective_datetime = (
                self._get_effective_datetime(
                    control=control,
                    fallback_datetime=current_datetime,
                )
            )

            report_date = (
                self._to_date(
                    ready_job.report_date
                )
            )

            # -------------------------------------------------
            # READY without report date.
            # -------------------------------------------------

            if report_date is None:
                continue

            priority = (
                self.priority_calculator.calculate(
                    job=job,
                    report_date=report_date,
                    current_datetime=(
                        effective_datetime
                    ),
                )
            )

            ready_job.time_priority = (
                priority.time_priority
            )

            ready_job.date_priority = (
                priority.date_priority
            )

            ready_job.job_priority = (
                priority.job_priority
            )

            ready_job.priority_key = (
                priority.priority_key
            )

            ready_job.time_state = (
                priority.time_state
            )

            ready_job.from_time = (
                self._time_to_string(
                    priority.from_time
                )
            )

            ready_job.to_time = (
                self._time_to_string(
                    priority.to_time
                )
            )

            ready_job.updated_at = (
                effective_datetime.isoformat()
            )

            self.ready_repository.save(
                ready_job
            )

    # =========================================================
    # Manual run helpers
    # =========================================================

    @staticmethod
    def _is_manual_run(
        control,
    ):
        """Return True when Job Control has a manual-run request."""

        if not control:
            return False

        value = control.get(
            "manual_run",
            0,
        )

        if isinstance(
            value,
            str,
        ):
            return value.strip().upper() in {
                "1",
                "Y",
                "YES",
                "TRUE",
                "T",
            }

        return bool(value)

    def _confirmation_is_blocked(
        self,
        job,
        control,
    ):
        """
        Return True when a confirmation-required job has not
        yet been confirmed.

        ConfirmationEvaluator remains the authoritative component.
        The scheduler only uses it here because the manual-run
        shortcut in EligibilityEvaluator intentionally bypasses the
        normal scheduled eligibility gates.
        """

        confirmation_evaluator = getattr(
            self.eligibility_evaluator,
            "confirmation_evaluator",
            None,
        )

        if confirmation_evaluator is None:
            return False

        return not confirmation_evaluator.can_proceed(
            job,
            control,
        )

    def _build_waiting_confirmation_result(
        self,
        job,
        control,
        occurrence_date,
    ):
        """Build the manual-run confirmation gate result."""

        confirmation_evaluator = getattr(
            self.eligibility_evaluator,
            "confirmation_evaluator",
            None,
        )

        confirmation_required = True
        confirmation_status = "WAITING_CONFIRMATION"
        reason = "Waiting for user confirmation."

        if confirmation_evaluator is not None:
            confirmation_required = (
                confirmation_evaluator.is_required(
                    job
                )
            )
            confirmation_status = (
                confirmation_evaluator.get_state(
                    job,
                    control,
                )
            )
            reason = (
                confirmation_evaluator.get_reason(
                    job,
                    control,
                )
            )

        return EligibilityResult(
            state="WAITING_CONFIRMATION",
            eligible=False,
            occurrence_date=occurrence_date,
            reason=reason,
            waiting_for="CONFIRMATION",
            confirmation_required=confirmation_required,
            confirmation_status=confirmation_status,
        )

    # =========================================================
    # Effective datetime
    # =========================================================

    @classmethod
    def _get_effective_datetime(
        cls,
        control,
        fallback_datetime,
    ):
        """
        Return the datetime that should be used for a
        particular job.

        Priority:

            1. Job Control override_datetime
            2. Scheduler cycle datetime

        The override is intentionally job-specific.

        Supported override values:

            datetime instance
            ISO formatted datetime string

        Invalid/empty override values fall back to the
        scheduler cycle datetime.
        """

        fallback_datetime = (
            cls._to_datetime(
                fallback_datetime
            )
        )

        if not control:
            return fallback_datetime

        override = control.get(
            "override_datetime"
        )

        if override is None:
            return fallback_datetime

        if isinstance(
            override,
            str,
        ):

            override = override.strip()

            if not override:
                return fallback_datetime

        try:
            return cls._to_datetime(
                override
            )

        except (TypeError, ValueError):
            # A malformed override should not stop the entire
            # scheduler cycle.
            #
            # The control value can subsequently be corrected
            # from the Django control interface.
            return fallback_datetime

    # =========================================================
    # Queue access
    # =========================================================

    def get_next_job(self):
        """
        Return and remove the highest-priority READY job
        from the derived heap.

        This does NOT execute the Oracle procedure.

        Execution belongs to the later execution layer.
        """

        return self.priority_queue.pop()

    def peek_next_job(self):
        """
        Return the highest-priority job without removing it.
        """

        return self.priority_queue.peek()

    def get_queue_size(self):
        """
        Return the current heap size.
        """

        return self.priority_queue.size()

    def is_queue_empty(self):
        """
        Return True when the heap contains no jobs.
        """

        return self.priority_queue.is_empty()

    # =========================================================
    # Summary
    # =========================================================

    def _build_summary(
        self,
        jobs,
        results,
        current_datetime,
    ):
        """
        Build a monitoring-friendly summary for the cycle.
        """

        staging_count = (
            self.staging_manager
            .staging_repository
            .count()
        )

        ready_count = (
            self.ready_repository
            .count()
        )

        states = {}

        for result in results.values():

            states[result.state] = (
                states.get(
                    result.state,
                    0,
                )
                + 1
            )

        return {
            "timestamp": (
                current_datetime.isoformat()
            ),
            "total_jobs": len(jobs),
            "staging_count": (
                staging_count
            ),
            "ready_count": (
                ready_count
            ),
            "queue_size": (
                self.priority_queue.size()
            ),
            "states": states,
        }

    # =========================================================
    # Datetime conversion
    # =========================================================

    @staticmethod
    def _to_datetime(
        value,
    ):
        if isinstance(
            value,
            datetime,
        ):
            return value

        if isinstance(
            value,
            str,
        ):

            value = value.strip()

            if not value:
                raise TypeError(
                    "current_datetime cannot be empty."
                )

            try:
                return parse_iso_datetime(
                    value
                )

            except ValueError as exc:
                raise TypeError(
                    "current_datetime must be datetime "
                    "or ISO datetime string."
                ) from exc

        raise TypeError(
            "current_datetime must be datetime "
            "or ISO datetime string."
        )

    # =========================================================
    # Date conversion
    # =========================================================

    @staticmethod
    def _to_date(
        value,
    ):
        if value is None:
            return None

        if isinstance(
            value,
            datetime,
        ):
            return value.date()

        if isinstance(
            value,
            date,
        ):
            return value

        if isinstance(
            value,
            str,
        ):

            value = value.strip()

            if not value:
                return None

            try:
                return date.fromisoformat(
                    value
                )

            except ValueError:
                pass

            try:
                return datetime.fromisoformat(
                    value
                ).date()

            except ValueError as exc:
                raise TypeError(
                    f"Unsupported date value: {value!r}"
                ) from exc

        raise TypeError(
            f"Unsupported date value: {value!r}"
        )

    # =========================================================
    # Time conversion
    # =========================================================

    @staticmethod
    def _time_to_string(
        value,
    ):
        if value is None:
            return None

        if hasattr(
            value,
            "strftime",
        ):
            return value.strftime(
                "%H:%M"
            )

        return str(value)

    # =========================================================
    # Boolean conversion
    # =========================================================

    @staticmethod
    def _to_bool(
        value,
    ):
        if value is None:
            return False

        if isinstance(
            value,
            bool,
        ):
            return value

        if isinstance(
            value,
            int,
        ):
            return value != 0

        if isinstance(
            value,
            str,
        ):

            return value.strip().upper() in {
                "1",
                "Y",
                "YES",
                "TRUE",
                "T",
            }

        return bool(value)