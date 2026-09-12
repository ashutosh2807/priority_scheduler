from datetime import datetime
from datetime_compat import parse_iso_datetime

from models.staging_job import StagingJob
from models.ready_job import ReadyJob


class StagingManager:
    """
    Manages persistent STAGING and READY state.

    Responsibilities:

        EligibilityResult
              ↓
        STAGING / READY persistence

    This class does NOT decide:

        - which occurrence should run
        - what T is
        - what target date should be
        - whether a job is eligible
        - which job has the highest priority

    Those decisions belong to Scheduler, EligibilityEvaluator,
    and PriorityCalculator respectively.

    Persistent STAGING state is important because the scheduler
    runs repeatedly.

    For an existing occurrence, the following values are
    preserved:

        occurrence_date
        execution_date
        t_date
        target_date

    until Scheduler explicitly advances to a new occurrence.
    """

    def __init__(
        self,
        staging_repository,
        ready_repository,
        priority_calculator=None,
    ):
        self.staging_repository = (
            staging_repository
        )

        self.ready_repository = (
            ready_repository
        )

        self.priority_calculator = (
            priority_calculator
        )

    # =========================================================
    # Synchronize one job
    # =========================================================

    def synchronize(
        self,
        job,
        result,
        current_datetime,
    ):
        """
        Persist the state returned by EligibilityEvaluator.

        READY:
            remove STAGING and create/update READY.

        Everything else:
            remove READY and create/update STAGING.
        """

        if (
            result.eligible
            and result.state == "READY"
        ):
            return self._move_to_ready(
                job,
                result,
                current_datetime,
            )

        return self._move_to_staging(
            job,
            result,
            current_datetime,
        )

    # =========================================================
    # STAGING
    # =========================================================

    def _move_to_staging(
        self,
        job,
        result,
        current_datetime,
    ):
        """
        Create or update the persistent STAGING record.

        Existing occurrence/T/target values are preserved.

        This is essential for non-SAME_DAY jobs because DATEMAST
        can change between scheduler cycles while the same
        occurrence is still waiting.
        """

        job_id = job.id

        # -----------------------------------------------------
        # A job cannot simultaneously be READY and STAGING.
        # -----------------------------------------------------

        self.ready_repository.delete(
            job_id
        )

        now = self._to_datetime(
            current_datetime
        )

        existing = (
            self.staging_repository
            .get_by_id(
                job_id
            )
        )

        # =====================================================
        # Occurrence
        # =====================================================

        if (
            existing is not None
            and existing.occurrence_date
        ):
            occurrence_date = (
                existing.occurrence_date
            )

        else:
            occurrence_date = (
                self._date_to_string(
                    result.occurrence_date
                )
            )

        # =====================================================
        # Execution date
        # =====================================================
        #
        # This is the actual date on which this occurrence may
        # execute. It is deliberately separate from the
        # occurrence/report date.
        #
        # For SAME_DAY=0, for example:
        #
        #     occurrence_date = 2026-09-15
        #     execution_date = 2026-09-17
        #     report_date    = 2026-09-15
        #
        # Once established, preserve it while the occurrence
        # remains in STAGING.
        # =====================================================

        if (
            existing is not None
            and existing.execution_date
        ):
            execution_date = (
                existing.execution_date
            )

        else:
            execution_date = (
                self._date_to_string(
                    result.execution_date
                )
            )

        # =====================================================
        # T date
        # =====================================================

        if (
            existing is not None
            and existing.t_date
        ):
            t_date = (
                existing.t_date
            )

        else:
            t_date = (
                self._date_to_string(
                    result.t_date
                )
            )

        # =====================================================
        # Target date
        # =====================================================

        if (
            existing is not None
            and existing.target_date
        ):
            target_date = (
                existing.target_date
            )

        else:
            target_date = (
                self._date_to_string(
                    result.target_date
                )
            )

        # =====================================================
        # calculated_at
        # =====================================================
        #
        # This represents when this occurrence's T/target
        # calculation was initially established.
        #
        # Do not update it on every 3-minute cycle.
        # =====================================================

        if existing is not None:
            calculated_at = (
                existing.calculated_at
            )

        else:
            calculated_at = (
                now.isoformat()
            )

        # =====================================================
        # Build persistent STAGING model
        # =====================================================

        staging_job = StagingJob(
            job_id=job_id,

            job_name=job.name,

            state=result.state,

            occurrence_date=(
                occurrence_date
            ),

            execution_date=(
                execution_date
            ),

            t_date=(
                t_date
            ),

            report_date=(
                self._date_to_string(
                    result.report_date
                )
            ),

            target_date=(
                target_date
            ),

            margin=getattr(
                job,
                "margin",
                None,
            ),

            confirmation_required=(
                1
                if result.confirmation_required
                else 0
            ),

            confirmation_status=(
                result.confirmation_status
            ),

            time_flag=getattr(
                job,
                "time_flag",
                0,
            ),

            from_time=(
                self._time_to_string(
                    result.from_time
                )
            ),

            to_time=(
                self._time_to_string(
                    result.to_time
                )
            ),

            waiting_for=(
                result.waiting_for
            ),

            reason=(
                result.reason
            ),

            next_evaluation=(
                self._calculate_next_evaluation(
                    result=result,
                    current_datetime=now,
                )
            ),

            calculated_at=(
                calculated_at
            ),

            updated_at=(
                now.isoformat()
            ),
        )

        self.staging_repository.save(
            staging_job
        )

        return staging_job

    # =========================================================
    # MANUAL READY
    # =========================================================

    def synchronize_manual(
        self,
        job,
        result,
        current_datetime,
        existing_staging=None,
    ):
        """
        Create/update a READY record for a one-shot manual run.

        A manual run is an execution request against an existing
        scheduled occurrence.  When STAGING already contains a
        scheduled occurrence, that occurrence must remain untouched.

        Therefore manual READY is deliberately separate from the
        normal STAGING -> READY transition.

        The manual READY context is:

            occurrence_date = scheduled occurrence
            execution_date  = effective/manual execution date
            report_date     = original business/report date
            t_date          = None
            target_date     = scheduled target date, when available

        After the manual execution succeeds, READY is consumed while
        the original STAGING occurrence remains available for its
        normal scheduled execution lifecycle.
        """
        job_id = job.id
        now = self._to_datetime(current_datetime)

        if existing_staging is None:
            existing_staging = (
                self.staging_repository.get_by_id(job_id)
            )

        occurrence_date = None
        report_date = None
        target_date = None

        if existing_staging is not None:
            occurrence_date = existing_staging.occurrence_date
            report_date = existing_staging.report_date
            target_date = existing_staging.target_date

        if occurrence_date is None:
            occurrence_date = self._date_to_string(
                result.occurrence_date
            )

        if report_date is None:
            report_date = self._date_to_string(
                result.report_date
            )

        if target_date is None:
            target_date = self._date_to_string(
                result.target_date
            )

        # Manual execution uses the effective/override date.
        execution_date = self._date_to_string(
            result.execution_date
        )
        if execution_date is None:
            execution_date = now.date().isoformat()

        # Preserve a previously-created manual READY timestamp.
        existing_ready = self.ready_repository.get_by_id(job_id)
        if existing_ready is not None:
            ready_since = existing_ready.ready_since
        else:
            ready_since = now.isoformat()

        date_priority = 0
        time_priority = 0
        job_priority = 0
        priority_key = None
        time_state = getattr(result, "time_window_state", None)
        from_time = getattr(result, "from_time", None)
        to_time = getattr(result, "to_time", None)

        if self.priority_calculator is not None and report_date is not None:
            priority = self.priority_calculator.calculate(
                job=job,
                report_date=report_date,
                current_datetime=now,
            )

            date_priority = priority.date_priority
            time_priority = priority.time_priority
            job_priority = priority.job_priority
            priority_key = priority.priority_key
            time_state = priority.time_state
            from_time = priority.from_time
            to_time = priority.to_time

        ready_job = ReadyJob(
            job_id=job_id,
            job_name=job.name,
            occurrence_date=occurrence_date,
            execution_date=execution_date,
            t_date=None,
            report_date=report_date,
            target_date=target_date,
            ready_since=ready_since,
            time_priority=time_priority,
            date_priority=date_priority,
            job_priority=job_priority,
            priority_key=priority_key,
            from_time=self._time_to_string(from_time),
            to_time=self._time_to_string(to_time),
            time_state=time_state,
            updated_at=now.isoformat(),
        )

        # IMPORTANT: do NOT delete STAGING here.
        self.ready_repository.save(ready_job)
        return ready_job

    # =========================================================
    # READY
    # =========================================================

    def _move_to_ready(
        self,
        job,
        result,
        current_datetime,
    ):
        """
        Create or update the persistent READY record.

        READY is derived from successful eligibility evaluation.

        STAGING is removed because a job must not exist in both
        states at the same time.
        """

        job_id = job.id

        now = self._to_datetime(
            current_datetime
        )

        # -----------------------------------------------------
        # Existing READY record
        # -----------------------------------------------------

        existing_ready = (
            self.ready_repository
            .get_by_id(
                job_id
            )
        )

        # -----------------------------------------------------
        # Preserve original READY timestamp.
        #
        # This is important for monitoring and later execution.
        # A READY job that remains queued should retain its
        # original ready_since value.
        # -----------------------------------------------------

        if existing_ready is not None:

            ready_since = (
                existing_ready.ready_since
            )

        else:

            ready_since = (
                now.isoformat()
            )

        # =====================================================
        # Priority
        # =====================================================

        date_priority = 0
        time_priority = 0
        job_priority = 0
        priority_key = None

        time_state = (
            result.time_window_state
        )

        from_time = (
            result.from_time
        )

        to_time = (
            result.to_time
        )

        if (
            self.priority_calculator
            is not None
        ):

            priority = (
                self.priority_calculator
                .calculate(
                    job=job,
                    report_date=(
                        result.report_date
                    ),
                    current_datetime=now,
                )
            )

            date_priority = (
                priority.date_priority
            )

            time_priority = (
                priority.time_priority
            )

            job_priority = (
                priority.job_priority
            )

            priority_key = (
                priority.priority_key
            )

            time_state = (
                priority.time_state
            )

            from_time = (
                priority.from_time
            )

            to_time = (
                priority.to_time
            )

        # =====================================================
        # Preserve the complete scheduled execution context.
        # When the scheduler promotes STAGING -> READY, the READY
        # row must retain the original occurrence/execution/T/target
        # context. Only report_date is not sufficient for monitoring
        # and duplicate-safe scheduled execution.
        # =====================================================

        existing_staging = (
            self.staging_repository
            .get_by_id(
                job_id
            )
        )

        if existing_staging is not None:
            occurrence_date = existing_staging.occurrence_date
            execution_date = existing_staging.execution_date
            t_date = existing_staging.t_date
            target_date = existing_staging.target_date
        else:
            occurrence_date = self._date_to_string(
                result.occurrence_date
            )
            execution_date = self._date_to_string(
                result.execution_date
            )
            t_date = self._date_to_string(
                result.t_date
            )
            target_date = self._date_to_string(
                result.target_date
            )

        report_date = self._date_to_string(
            result.report_date
        )

        if report_date is None and existing_staging is not None:
            report_date = existing_staging.report_date

        # =====================================================
        # Build READY model
        # =====================================================

        ready_job = ReadyJob(
            job_id=job_id,

            job_name=job.name,

            occurrence_date=occurrence_date,

            execution_date=execution_date,

            t_date=t_date,

            report_date=report_date,

            target_date=target_date,

            ready_since=(
                ready_since
            ),

            time_priority=(
                time_priority
            ),

            date_priority=(
                date_priority
            ),

            job_priority=(
                job_priority
            ),

            priority_key=(
                priority_key
            ),

            from_time=(
                self._time_to_string(
                    from_time
                )
            ),

            to_time=(
                self._time_to_string(
                    to_time
                )
            ),

            time_state=(
                time_state
            ),

            updated_at=(
                now.isoformat()
            ),
        )

        # =====================================================
        # Remove STAGING
        # =====================================================

        self.staging_repository.delete(
            job_id
        )

        # =====================================================
        # Save READY
        # =====================================================

        self.ready_repository.save(
            ready_job
        )

        return ready_job

    # =========================================================
    # Synchronize all
    # =========================================================

    def synchronize_all(
        self,
        jobs,
        results,
        current_datetime,
    ):
        """
        Synchronize a collection of jobs.

        results may be:

            {job_id: EligibilityResult}

        or any iterable convertible to a dictionary.
        """

        if isinstance(
            results,
            dict,
        ):
            result_map = results

        else:
            result_map = dict(
                results
            )

        synchronized = []

        for job in jobs:

            result = result_map.get(
                job.id
            )

            if result is None:
                continue

            synchronized.append(
                self.synchronize(
                    job,
                    result,
                    current_datetime,
                )
            )

        return synchronized

    # =========================================================
    # Rebuild
    # =========================================================

    def rebuild(
        self,
        jobs,
        evaluator,
        controls,
        current_datetime,
        t_date_provider=None,
        datemast=None,
    ):
        """
        Re-evaluate a collection of jobs and synchronize their
        persistent state.

        This method is retained as a convenience API.

        The main Scheduler normally performs this orchestration
        itself because it also controls persistent occurrence
        lifecycle.
        """

        current_datetime = (
            self._to_datetime(
                current_datetime
            )
        )

        results = {}

        for job in jobs:

            control = controls.get(
                job.id
            )

            existing = (
                self.staging_repository
                .get_by_id(
                    job.id
                )
            )

            occurrence_date = None
            persisted_t_date = None
            persisted_target_date = None

            if existing is not None:

                occurrence_date = (
                    existing.occurrence_date
                )

                persisted_t_date = (
                    existing.t_date
                )

                persisted_target_date = (
                    existing.target_date
                )

            # -------------------------------------------------
            # T-date provider is only used for a NEW occurrence.
            # -------------------------------------------------

            t_date = None

            if (
                existing is None
                and t_date_provider is not None
            ):
                t_date = (
                    t_date_provider(
                        job
                    )
                )

            # -------------------------------------------------
            # Evaluate
            # -------------------------------------------------

            result = evaluator.evaluate(
                job=job,
                current_datetime=(
                    current_datetime
                ),
                control=control,
                t_date=t_date,
                datemast=datemast,
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

            results[job.id] = result

            # -------------------------------------------------
            # Persist
            # -------------------------------------------------

            self.synchronize(
                job,
                result,
                current_datetime,
            )

        return results

    # =========================================================
    # Next evaluation
    # =========================================================

    def _calculate_next_evaluation(
        self,
        result,
        current_datetime,
    ):
        """
        Calculate when this STAGING record should next be
        reconsidered.

        The scheduler currently runs every 3 minutes, so the
        safest persistent value is the next scheduler cycle.

        This field is primarily useful for monitoring/UI.
        It does not itself trigger execution.
        """

        current_datetime = (
            self._to_datetime(
                current_datetime
            )
        )

        if result.waiting_for in {
            "CONFIRMATION",
            "DATEMAST",
            "TIME_WINDOW",
            "FREQUENCY",
            "CALENDAR_DAY",
            "NEXT_OCCURRENCE",
            "WAITING_EXECUTION_DATE",
            "CONTROL",
        }:
            return (
                current_datetime.isoformat()
            )

        return None

    # =========================================================
    # Datetime helper
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
    # Date helper
    # =========================================================

    @staticmethod
    def _date_to_string(
        value,
    ):
        if value is None:
            return None

        if isinstance(
            value,
            datetime,
        ):
            return value.date().isoformat()

        if hasattr(
            value,
            "isoformat",
        ):
            return value.isoformat()

        return str(value)

    # =========================================================
    # Time helper
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