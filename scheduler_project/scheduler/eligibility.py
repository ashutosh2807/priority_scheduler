from dataclasses import dataclass
from datetime import date, datetime
from datetime_compat import parse_iso_datetime

from scheduler.confirmation import ConfirmationEvaluator
from scheduler.frequency import FrequencyEvaluator
from scheduler.holiday import HolidayEvaluator
from scheduler.margin import MarginCalculator
from scheduler.time_window import TimeWindowEvaluator


@dataclass
class EligibilityResult:
    state: str
    eligible: bool = False

    # Stable identity for a planned report-date occurrence.  Legacy callers
    # that evaluate one row per job can leave this unset; the occurrence-aware
    # runtime sets it so queue, execution and the UI never collapse two
    # frequencies belonging to the same master job.
    occurrence_key: str = None

    # ---------------------------------------------------------
    # Occurrence
    # ---------------------------------------------------------

    occurrence_date: object = None

    # Actual date on which this occurrence is allowed to execute.
    execution_date: object = None

    # ---------------------------------------------------------
    # DATEMAST / target information
    # ---------------------------------------------------------

    # DATEMAST T date used to calculate this occurrence.
    t_date: object = None

    # Current DATEMAST report date.
    report_date: object = None

    # Fixed target date for this occurrence.
    target_date: object = None

    # ---------------------------------------------------------
    # Waiting / reason
    # ---------------------------------------------------------

    waiting_for: str = None
    reason: str = None

    # ---------------------------------------------------------
    # Time window
    # ---------------------------------------------------------

    from_time: object = None
    to_time: object = None
    time_window_state: str = None

    # ---------------------------------------------------------
    # Confirmation
    # ---------------------------------------------------------

    confirmation_required: bool = False
    confirmation_status: str = None

    # ---------------------------------------------------------
    # Evaluation details
    # ---------------------------------------------------------

    frequency_matched: bool = False
    day_allowed: bool = False
    datemast_ready: bool = False


class EligibilityEvaluator:

    STAGING = "STAGING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    WAITING_TIME = "WAITING_TIME"
    WAITING_DATEMAST = "WAITING_DATEMAST"
    WAITING_EXECUTION_DATE = "WAITING_EXECUTION_DATE"
    READY = "READY"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"

    def __init__(
        self,
        frequency_evaluator=None,
        holiday_evaluator=None,
        margin_calculator=None,
        confirmation_evaluator=None,
        time_window_evaluator=None,
    ):

        self.frequency_evaluator = (
            frequency_evaluator
            or FrequencyEvaluator()
        )

        self.holiday_evaluator = (
            holiday_evaluator
            or HolidayEvaluator()
        )

        self.margin_calculator = (
            margin_calculator
            or MarginCalculator(
                working_day_checker=(
                    self.holiday_evaluator.is_working_day
                )
            )
        )

        self.confirmation_evaluator = (
            confirmation_evaluator
            or ConfirmationEvaluator()
        )

        self.time_window_evaluator = (
            time_window_evaluator
            or TimeWindowEvaluator()
        )

    # =========================================================
    # Main evaluation
    # =========================================================

    def evaluate(
        self,
        job,
        current_datetime,
        control=None,
        t_date=None,
        datemast=None,
        occurrence_date=None,
        persisted_t_date=None,
        persisted_target_date=None,
        persisted_execution_date=None,
    ):
        """
        Evaluate whether a scheduled job is eligible.

        Parameters
        ----------
        job:
            ScheduleMaster instance.

        current_datetime:
            Current scheduler datetime.

        control:
            Job control state.

        t_date:
            Optional authoritative DATEMAST T date supplied
            by the caller for a new occurrence.

        datemast:
            DATEMAST provider.

        occurrence_date:
            Scheduled occurrence being evaluated.

        persisted_t_date:
            T date already stored for an existing staging
            occurrence.

        persisted_target_date:
            Target date already stored for an existing staging
            occurrence.

        persisted_execution_date:
            Execution date already stored for an existing staging
            occurrence.

        Important
        ---------
        If persisted_target_date is supplied, it is NEVER
        recalculated from the current DATEMAST T date.
        """

        current_datetime = self._to_datetime(
            current_datetime
        )

        current_date = current_datetime.date()

        # =====================================================
        # Determine occurrence
        # =====================================================

        if occurrence_date is None:
            occurrence_date = current_date

        occurrence_date = self._to_date(occurrence_date)

        # =====================================================
        # Control
        # =====================================================

        # Resolve the occurrence first so PAUSED/CANCELLED can preserve
        # the scheduled occurrence and its persisted T/target context.
        control_result = self._evaluate_control(
            control=control,
            occurrence_date=occurrence_date,
            persisted_t_date=persisted_t_date,
            persisted_target_date=persisted_target_date,
            persisted_execution_date=persisted_execution_date,
            t_date=t_date,
            job=job,
        )

        if control_result is not None:
            return control_result

        # =====================================================
        # Manual run
        # =====================================================

        if self._is_manual_run(control):
            return EligibilityResult(
                state=self.READY,
                eligible=True,
                occurrence_date=occurrence_date,
                execution_date=current_date,
                t_date=None,
                report_date=occurrence_date,
                target_date=occurrence_date,
                reason="Manual run requested.",
            )

        # =====================================================
        # Frequency
        # =====================================================

        frequency_matched = (
            self.frequency_evaluator.is_scheduled(
                job,
                occurrence_date,
                reference_date=t_date,
            )
        )

        if not frequency_matched:
            return EligibilityResult(
                state=self.STAGING,
                eligible=False,
                occurrence_date=occurrence_date,
                reason="Frequency does not match.",
                waiting_for="FREQUENCY",
            )

        # =====================================================
        # SAME_DAY flag / calendar policy
        # =====================================================

        same_day = self._to_bool(getattr(job, "same_day", 0))

        # SAME_DAY=1 runs on the occurrence itself and therefore uses
        # HOLIDAY_RUN. SAME_DAY=0 deliberately allows an occurrence to
        # fall on a weekend/holiday; it executes on the next working day.
        if same_day:
            day_allowed = self.holiday_evaluator.can_run_on_day(
                job, occurrence_date
            )

            if not day_allowed:
                return EligibilityResult(
                    state=self.STAGING,
                    eligible=False,
                    occurrence_date=occurrence_date,
                    execution_date=occurrence_date,
                    t_date=None,
                    report_date=occurrence_date,
                    target_date=occurrence_date,
                    reason=(
                        "Job is not allowed to run on "
                        "this calendar day."
                    ),
                    waiting_for="CALENDAR_DAY",
                    frequency_matched=True,
                    day_allowed=False,
                )

        # =====================================================
        # SAME_DAY
        # =====================================================

        if same_day:
            return self._evaluate_same_day(
                job=job,
                occurrence_date=occurrence_date,
                current_datetime=current_datetime,
                control=control,
            )

        # =====================================================
        # NON SAME_DAY
        # =====================================================

        return self._evaluate_non_same_day(
            job=job,
            occurrence_date=occurrence_date,
            current_datetime=current_datetime,
            control=control,
            t_date=t_date,
            datemast=datemast,
            persisted_t_date=persisted_t_date,
            persisted_target_date=persisted_target_date,
            persisted_execution_date=persisted_execution_date,
            frequency_matched=frequency_matched,
        )

    # =========================================================
    # PRE-PLANNED OCCURRENCE EVALUATION
    # =========================================================

    def evaluate_occurrence(
        self,
        job,
        current_datetime,
        occurrence,
        control=None,
        actual_datetime=None,
    ):
        """Evaluate a durable occurrence planned by the scheduler.

        ``OracleCompatibleOccurrencePlanner`` deliberately owns the legacy
        report/run-date selection rules.  In particular, a normal DAILY run
        receives the prior DATEMAST report date while a periodic boundary is
        run on its next working day.  Re-running the generic ``evaluate``
        method would recalculate that context and silently turn a daily
        report date into the current date, so this method only applies
        controls, confirmation, calendar policy and the RUN_BY gate.

        Past occurrences remain visible for manual recovery. Automatic queue
        admission ends at midnight on their original planned execution day.
        """

        if occurrence is None:
            raise ValueError("occurrence is required.")

        current_datetime = self._to_datetime(current_datetime)
        occurrence_date = self._to_date(occurrence.occurrence_date)
        execution_date = self._to_date(occurrence.execution_date)
        t_date = self._to_date(occurrence.t_date)
        report_date = self._to_date(occurrence.report_date)
        target_date = self._to_date(occurrence.target_date)
        occurrence_key = getattr(occurrence, "occurrence_key", None)

        # Older job-keyed STAGING rows also represented an unscheduled master
        # placeholder. Their null dates are not an executable occurrence and
        # must not be guessed from today's clock or admitted by a manual flag.
        if occurrence_date is None or execution_date is None or report_date is None:
            return EligibilityResult(
                state=self.STAGING,
                eligible=False,
                occurrence_key=occurrence_key,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                waiting_for="OCCURRENCE_CONTEXT",
                reason="Legacy staging has incomplete business dates; waiting for an authoritative scheduled occurrence.",
                datemast_ready=False,
            )

        control_result = self._evaluate_control(
            control=control,
            occurrence_date=occurrence_date,
            persisted_t_date=t_date,
            persisted_target_date=target_date,
            persisted_execution_date=execution_date,
            job=job,
        )
        if control_result is not None:
            return self._apply_occurrence_context(
                control_result,
                occurrence_key=occurrence_key,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
            )

        execution_clock = self._to_datetime(actual_datetime or current_datetime)
        if execution_clock.date() > execution_date and not self._is_manual_run(control):
            return EligibilityResult(
                state="MANUAL_REQUIRED", eligible=False,
                occurrence_key=occurrence_key, occurrence_date=occurrence_date,
                execution_date=execution_date, t_date=t_date, report_date=report_date,
                target_date=target_date, waiting_for="MANUAL_RUN",
                reason="The planned execution day has passed; request a manual run.",
                frequency_matched=True, datemast_ready=True,
            )

        same_day = self._to_bool(getattr(job, "same_day", 0))
        if not self.holiday_evaluator.can_run_on_day(job, current_datetime.date()) or (
            same_day and not self.holiday_evaluator.can_run_on_day(job, occurrence_date)
        ):
            return EligibilityResult(
                state=self.STAGING,
                eligible=False,
                occurrence_key=occurrence_key,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason="Job is not allowed to run on this calendar day.",
                waiting_for="CALENDAR_DAY",
                frequency_matched=True,
                day_allowed=False,
                datemast_ready=True,
            )

        confirmation_required = self.confirmation_evaluator.is_required(job)
        confirmation_status = self.confirmation_evaluator.get_state(job, control)
        if not self.confirmation_evaluator.can_proceed(job, control):
            return EligibilityResult(
                state=self.WAITING_CONFIRMATION,
                eligible=False,
                occurrence_key=occurrence_key,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason=self.confirmation_evaluator.get_reason(job, control),
                waiting_for="CONFIRMATION",
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=True,
                day_allowed=True,
                datemast_ready=True,
            )

        # A deliberate manual request can run an existing/new occurrence now
        # once confirmation has passed.  PAUSED/CANCELLED were handled above.
        if self._is_manual_run(control):
            return EligibilityResult(
                state=self.READY,
                eligible=True,
                occurrence_key=occurrence_key,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason="Manual run requested.",
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=True,
                day_allowed=True,
                datemast_ready=True,
            )

        if execution_clock.date() < execution_date:
            return EligibilityResult(
                state=self.WAITING_EXECUTION_DATE,
                eligible=False,
                occurrence_key=occurrence_key,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason="Waiting for the scheduled execution date.",
                waiting_for="EXECUTION_DATE",
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=True,
                day_allowed=True,
                datemast_ready=True,
            )

        time_state = self.time_window_evaluator.get_job_time_state(
            job,
            current_datetime,
        )
        from_time, to_time = self.time_window_evaluator.get_run_by(job)
        if time_state == self.time_window_evaluator.WAITING_FOR_TIME_WINDOW:
            return EligibilityResult(
                state=self.WAITING_TIME,
                eligible=False,
                occurrence_key=occurrence_key,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason="Waiting for the job's RUN_BY time window.",
                waiting_for="TIME_WINDOW",
                from_time=from_time,
                to_time=to_time,
                time_window_state=time_state,
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=True,
                day_allowed=True,
                datemast_ready=True,
            )

        if time_state == self.time_window_evaluator.TIME_WINDOW_EXPIRED:
            return EligibilityResult(
                state=self.STAGING,
                eligible=False,
                occurrence_key=occurrence_key,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason=(
                    "The job's RUN_BY window has elapsed; the pending "
                    "occurrence will be revisited in a later time slot."
                ),
                waiting_for="TIME_WINDOW",
                from_time=from_time,
                to_time=to_time,
                time_window_state=time_state,
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=True,
                day_allowed=True,
                datemast_ready=True,
            )

        return EligibilityResult(
            state=self.READY,
            eligible=True,
            occurrence_key=occurrence_key,
            occurrence_date=occurrence_date,
            execution_date=execution_date,
            t_date=t_date,
            report_date=report_date,
            target_date=target_date,
            reason="All eligibility conditions satisfied.",
            confirmation_required=confirmation_required,
            confirmation_status=confirmation_status,
            frequency_matched=True,
            day_allowed=True,
            datemast_ready=True,
            from_time=from_time,
            to_time=to_time,
            time_window_state=time_state,
        )

    @staticmethod
    def _apply_occurrence_context(
        result,
        *,
        occurrence_key,
        occurrence_date,
        execution_date,
        t_date,
        report_date,
        target_date,
    ):
        """Keep a control result attached to its original occurrence."""

        result.occurrence_key = occurrence_key
        result.occurrence_date = occurrence_date
        result.execution_date = execution_date
        result.t_date = t_date
        result.report_date = report_date
        result.target_date = target_date
        return result

    # =========================================================
    # SAME_DAY evaluation
    # =========================================================

    def _evaluate_same_day(
        self,
        job,
        occurrence_date,
        current_datetime,
        control,
    ):
        """
        SAME_DAY jobs do not use DATEMAST.

        Their report/target date is the occurrence date.
        """

        target_date = occurrence_date
        report_date = occurrence_date
        datemast_ready = True

        # -----------------------------------------------------
        # Confirmation
        # -----------------------------------------------------

        confirmation_required = (
            self.confirmation_evaluator.is_required(
                job
            )
        )

        confirmation_status = (
            self.confirmation_evaluator.get_state(
                job,
                control,
            )
        )

        if not self.confirmation_evaluator.can_proceed(
            job,
            control,
        ):

            return EligibilityResult(
                state=self.WAITING_CONFIRMATION,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=occurrence_date,
                t_date=None,
                report_date=report_date,
                target_date=target_date,
                reason=(
                    self.confirmation_evaluator.get_reason(
                        job,
                        control,
                    )
                ),
                waiting_for="CONFIRMATION",
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=True,
                day_allowed=True,
                datemast_ready=datemast_ready,
            )

        # -----------------------------------------------------
        # Time window
        # -----------------------------------------------------

        time_state = (
            self.time_window_evaluator.get_job_time_state(
                job,
                current_datetime,
            )
        )

        from_time, to_time = (
            self.time_window_evaluator.get_run_by(
                job
            )
        )

        if (
            time_state
            == self.time_window_evaluator.WAITING_FOR_TIME_WINDOW
        ):

            return EligibilityResult(
                state=self.WAITING_TIME,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=occurrence_date,
                t_date=None,
                report_date=report_date,
                target_date=target_date,
                reason=(
                    "Waiting for the job's RUN_BY "
                    "time window."
                ),
                waiting_for="TIME_WINDOW",
                from_time=from_time,
                to_time=to_time,
                time_window_state=time_state,
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=True,
                day_allowed=True,
                datemast_ready=True,
            )

        if (
            time_state
            == self.time_window_evaluator.TIME_WINDOW_EXPIRED
        ):

            return EligibilityResult(
                state=self.STAGING,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=occurrence_date,
                t_date=None,
                report_date=report_date,
                target_date=target_date,
                reason=(
                    "The job's RUN_BY window has elapsed; the pending "
                    "occurrence will be revisited in a later time slot."
                ),
                # The legacy package retained PENDING rows with
                # run_date <= today and retried them during later RUN_BY
                # windows.  Do not let an elapsed clock window discard this
                # occurrence in favour of a future frequency occurrence.
                waiting_for="TIME_WINDOW",
                from_time=from_time,
                to_time=to_time,
                time_window_state=time_state,
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=True,
                day_allowed=True,
                datemast_ready=True,
            )

        # -----------------------------------------------------
        # READY
        # -----------------------------------------------------

        return EligibilityResult(
            state=self.READY,
            eligible=True,
            occurrence_date=occurrence_date,
            execution_date=occurrence_date,
            t_date=None,
            report_date=report_date,
            target_date=target_date,
            reason="All eligibility conditions satisfied.",
            waiting_for=None,
            from_time=from_time,
            to_time=to_time,
            time_window_state=time_state,
            confirmation_required=confirmation_required,
            confirmation_status=confirmation_status,
            frequency_matched=True,
            day_allowed=True,
            datemast_ready=True,
        )

    # =========================================================
    # NON SAME_DAY evaluation
    # =========================================================

    def _evaluate_non_same_day(
        self,
        job,
        occurrence_date,
        current_datetime,
        control,
        t_date,
        datemast,
        persisted_t_date,
        persisted_target_date,
        persisted_execution_date,
        frequency_matched,
    ):
        """
        Evaluate a non-SAME_DAY job.

        Existing staged occurrences reuse their persisted
        T and target date.

        New occurrences obtain T from DATEMAST and calculate
        target_date exactly once.
        """

        # -----------------------------------------------------
        # Execution date
        # -----------------------------------------------------
        # SAME_DAY=0 means the occurrence executes on the next
        # working day strictly after the occurrence date.
        if persisted_execution_date is not None:
            execution_date = self._to_date(
                persisted_execution_date
            )
        else:
            execution_date = self.margin_calculator.next_working_day(
                occurrence_date
            )

        # The occurrence may be a weekend/holiday.  The execution
        # date is already guaranteed to be a working day by the
        # next_working_day calculation for a new occurrence.
        # For an existing occurrence, the persisted execution date
        # is authoritative and must not be silently recalculated.
        day_allowed = True

        # -----------------------------------------------------
        # Normalize persisted values
        # -----------------------------------------------------

        if persisted_t_date is not None:
            persisted_t_date = self._to_date(
                persisted_t_date
            )

        if persisted_target_date is not None:
            persisted_target_date = self._to_date(
                persisted_target_date
            )

        # =====================================================
        # Establish the fixed T / target context
        # =====================================================
        #
        # T belongs to the scheduled occurrence, not to the
        # scheduler's current date.
        #
        # Therefore a new occurrence on 15-Sep must obtain T
        # using 15-Sep even when the current scheduler cycle is
        # 16-Sep, 17-Sep, etc.
        #
        # Once target_date is persisted, it is never recalculated
        # for that occurrence.
        # =====================================================

        if persisted_target_date is not None:

            target_date = persisted_target_date

            if persisted_t_date is not None:
                t_date = persisted_t_date

            elif t_date is not None:
                t_date = self._to_date(t_date)

        else:

            if t_date is None and datemast is not None:
                t_date = (
                    datemast.get_report_date_for(
                        occurrence_date
                    )
                )

            if t_date is not None:
                t_date = self._to_date(t_date)

                margin = getattr(
                    job,
                    "margin",
                    None,
                )

                target_date = (
                    self.margin_calculator.calculate(
                        t_date,
                        margin,
                    )
                )
            else:
                target_date = None

        # =====================================================
        # Execution-date gate
        # =====================================================
        #
        # Execution date is checked before the DATEMAST readiness
        # threshold. T/target are carried when available so a
        # repeated scheduler cycle cannot recalculate T from the
        # newer current date.
        # =====================================================

        if current_datetime.date() < execution_date:

            return EligibilityResult(
                state=self.WAITING_EXECUTION_DATE,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=occurrence_date,
                target_date=target_date,
                reason=(
                    "Waiting for the next working execution date."
                ),
                waiting_for="EXECUTION_DATE",
                frequency_matched=frequency_matched,
                day_allowed=day_allowed,
                datemast_ready=False,
            )

        if t_date is None:

            return EligibilityResult(
                state=self.WAITING_DATEMAST,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=None,
                report_date=occurrence_date,
                target_date=None,
                reason=(
                    "Authoritative DATEMAST T date "
                    "is not available for the occurrence."
                ),
                waiting_for="DATEMAST",
                frequency_matched=frequency_matched,
                day_allowed=day_allowed,
                datemast_ready=False,
            )

        # =====================================================
        # DATEMAST threshold
        # =====================================================
        # DATEMAST's latest date is used ONLY to decide whether the
        # fixed target has become available.  It must never replace
        # the occurrence/report date sent to Oracle.

        datemast_latest = (
            self._get_datemast_report_date(
                datemast
            )
        )

        report_date = occurrence_date

        if datemast_latest is None:
            return EligibilityResult(
                state=self.WAITING_DATEMAST,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason=(
                    "Waiting for DATEMAST report date "
                    "to reach target date."
                ),
                waiting_for="DATEMAST",
                frequency_matched=frequency_matched,
                day_allowed=day_allowed,
                datemast_ready=False,
            )

        datemast_latest = self._to_date(
            datemast_latest
        )

        # -----------------------------------------------------
        # DATEMAST gate
        # -----------------------------------------------------

        if datemast_latest < target_date:

            return EligibilityResult(
                state=self.WAITING_DATEMAST,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason=(
                    "DATEMAST report date has not "
                    "reached the target date."
                ),
                waiting_for="DATEMAST",
                frequency_matched=frequency_matched,
                day_allowed=True,
                datemast_ready=False,
            )

        datemast_ready = True

        # =====================================================
        # Confirmation
        # =====================================================

        confirmation_required = (
            self.confirmation_evaluator.is_required(
                job
            )
        )

        confirmation_status = (
            self.confirmation_evaluator.get_state(
                job,
                control,
            )
        )

        if not self.confirmation_evaluator.can_proceed(
            job,
            control,
        ):

            return EligibilityResult(
                state=self.WAITING_CONFIRMATION,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason=(
                    self.confirmation_evaluator.get_reason(
                        job,
                        control,
                    )
                ),
                waiting_for="CONFIRMATION",
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=frequency_matched,
                day_allowed=True,
                datemast_ready=datemast_ready,
            )

        # =====================================================
        # Time window
        # =====================================================

        time_state = (
            self.time_window_evaluator.get_job_time_state(
                job,
                current_datetime,
            )
        )

        from_time, to_time = (
            self.time_window_evaluator.get_run_by(
                job
            )
        )

        # -----------------------------------------------------
        # Future time window
        # -----------------------------------------------------

        if (
            time_state
            == self.time_window_evaluator.WAITING_FOR_TIME_WINDOW
        ):

            return EligibilityResult(
                state=self.WAITING_TIME,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason=(
                    "Waiting for the job's RUN_BY "
                    "time window."
                ),
                waiting_for="TIME_WINDOW",
                from_time=from_time,
                to_time=to_time,
                time_window_state=time_state,
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=frequency_matched,
                day_allowed=True,
                datemast_ready=datemast_ready,
            )

        # -----------------------------------------------------
        # Expired time window
        # -----------------------------------------------------

        if (
            time_state
            == self.time_window_evaluator.TIME_WINDOW_EXPIRED
        ):

            return EligibilityResult(
                state=self.STAGING,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_date,
                report_date=report_date,
                target_date=target_date,
                reason=(
                    "The job's RUN_BY window has elapsed; the pending "
                    "occurrence will be revisited in a later time slot."
                ),
                waiting_for="TIME_WINDOW",
                from_time=from_time,
                to_time=to_time,
                time_window_state=time_state,
                confirmation_required=confirmation_required,
                confirmation_status=confirmation_status,
                frequency_matched=frequency_matched,
                day_allowed=True,
                datemast_ready=datemast_ready,
            )

        # =====================================================
        # READY
        # =====================================================

        return EligibilityResult(
            state=self.READY,
            eligible=True,
            occurrence_date=occurrence_date,
            execution_date=execution_date,
            t_date=t_date,
            report_date=report_date,
            target_date=target_date,
            reason="All eligibility conditions satisfied.",
            waiting_for=None,
            from_time=from_time,
            to_time=to_time,
            time_window_state=time_state,
            confirmation_required=confirmation_required,
            confirmation_status=confirmation_status,
            frequency_matched=frequency_matched,
            day_allowed=True,
            datemast_ready=datemast_ready,
        )

    # =========================================================
    # Control
    # =========================================================

    def _evaluate_control(
        self,
        control,
        occurrence_date=None,
        persisted_t_date=None,
        persisted_target_date=None,
        persisted_execution_date=None,
        t_date=None,
        job=None,
    ):
        """Apply Django control state without losing occurrence context."""
        if not control:
            return None

        occurrence_date = (
            self._to_date(occurrence_date)
            if occurrence_date is not None
            else None
        )
        t_source = (
            persisted_t_date
            if persisted_t_date is not None
            else t_date
        )

        t_value = (
            self._to_date(t_source)
            if t_source is not None
            else None
        )
        target_value = (
            self._to_date(persisted_target_date)
            if persisted_target_date is not None
            else None
        )

        if (
            target_value is None
            and t_value is not None
            and job is not None
        ):
            target_value = self.margin_calculator.calculate(
                t_value,
                getattr(job, "margin", None),
            )

        status = str(
            control.get("control_status", "ACTIVE")
        ).strip().upper()

        if status == "PAUSED":
            same_day = self._to_bool(
                getattr(job, "same_day", 0)
            ) if job is not None else False
            execution_date = (
                self._to_date(persisted_execution_date)
                if persisted_execution_date is not None
                else occurrence_date
            )
            if (
                execution_date is None
                and occurrence_date is not None
                and not same_day
            ):
                execution_date = self.margin_calculator.next_working_day(
                    occurrence_date
                )

            return EligibilityResult(
                state=self.PAUSED,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_value,
                report_date=occurrence_date,
                target_date=target_value,
                reason="Job is paused.",
                waiting_for="CONTROL",
            )

        if status == "CANCELLED":
            same_day = self._to_bool(
                getattr(job, "same_day", 0)
            ) if job is not None else False
            execution_date = (
                self._to_date(persisted_execution_date)
                if persisted_execution_date is not None
                else occurrence_date
            )
            if (
                execution_date is None
                and occurrence_date is not None
                and not same_day
            ):
                execution_date = self.margin_calculator.next_working_day(
                    occurrence_date
                )

            return EligibilityResult(
                state=self.CANCELLED,
                eligible=False,
                occurrence_date=occurrence_date,
                execution_date=execution_date,
                t_date=t_value,
                report_date=occurrence_date,
                target_date=target_value,
                reason="Job is cancelled.",
                waiting_for="CONTROL",
            )

        return None

    # =========================================================
    # Manual run
    # =========================================================

    @staticmethod
    def _is_manual_run(control):

        if not control:
            return False

        value = control.get(
            "manual_run",
            0,
        )

        if isinstance(value, str):

            return value.strip().upper() in {
                "1",
                "Y",
                "YES",
                "TRUE",
                "T",
            }

        return bool(value)

    # =========================================================
    # DATEMAST
    # =========================================================

    @staticmethod
    def _get_datemast_report_date(datemast):

        if datemast is None:
            return None

        if hasattr(
            datemast,
            "get_latest_report_date",
        ):

            return (
                datemast.get_latest_report_date()
            )

        return None

    # =========================================================
    # Date conversion
    # =========================================================

    @staticmethod
    def _to_date(value):

        if value is None:
            return None

        if isinstance(value, datetime):
            return value.date()

        if isinstance(value, date):
            return value

        if isinstance(value, str):

            value = value.strip()

            try:
                return date.fromisoformat(value)

            except ValueError:
                pass

            try:
                return datetime.fromisoformat(
                    value
                ).date()

            except ValueError:
                pass

        raise TypeError(
            f"Unsupported date value: {value!r}"
        )

    # =========================================================
    # Datetime conversion
    # =========================================================

    @staticmethod
    def _to_datetime(value):

        if isinstance(value, datetime):
            return value

        if isinstance(value, str):

            try:
                return parse_iso_datetime(
                    value
                )

            except ValueError:
                pass

        raise TypeError(
            "current_datetime must be datetime "
            "or ISO datetime string."
        )

    # =========================================================
    # Boolean conversion
    # =========================================================

    @staticmethod
    def _to_bool(value):

        if value is None:
            return False

        if isinstance(value, bool):
            return value

        if isinstance(value, int):
            return value != 0

        if isinstance(value, str):

            return value.strip().upper() in {
                "1",
                "Y",
                "YES",
                "TRUE",
                "T",
            }

        return bool(value)
