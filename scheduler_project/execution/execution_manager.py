from datetime import date, datetime
import logging

from config.settings import (
    RETRY_LOOKBACK_DAYS,
    RETRY_WINDOWS,
)
from scheduler.retry_policy import RetryPolicy
from scheduler.confirmation import ConfirmationEvaluator
from scheduler.holiday import HolidayEvaluator
from repositories.worker_logging import WorkerLoggingObserver, consume_manual_request


logger = logging.getLogger(
    "scheduler.execution"
)


class ExecutionManager:
    """
    Manage execution of persistent READY jobs.

    Execution flow:

        READY
          |
          v
        validate READY
          |
          v
        validate Job Control
          |
          v
        resolve Schedule Master
          |
          v
        duplicate protection
          |
          v
        Schedule_extg -> PENDING
          |
          v
        SQLite execution_jobs -> RUNNING
          |
          v
        Schedule_extg -> RUNNING
          |
          v
        OracleExecutor
          |
          +------------------+
          |                  |
          v                  v
       SUCCESS             FAILED
          |                  |
          v                  v
    execution_jobs      execution_jobs
       SUCCESS             FAILED
          |                  |
          v                  v
    Schedule_extg       Schedule_extg
       SUCCESS             FAILED


    Design rules
    ------------

    1. READY is persistent SQLite state.

    2. The priority heap is derived state.

    3. A heap entry must still exist in ready_jobs before execution.

    4. PAUSED and CANCELLED jobs never execute.

    5. A scheduled occurrence with SUCCESS must never execute again.

    6. A scheduled occurrence already RUNNING must not execute
       concurrently.

    7. Every Oracle execution must first have a persistent SQLite
       RUNNING execution record.

    8. Failure to create the SQLite execution record prevents
       Oracle execution.

    9. FAILED executions remain in execution history.

    10. Each retry gets a new attempt number.

    11. Schedule_extg is execution-facing/logging state.

    12. Manual executions are one-shot control requests.

    13. Manual executions are not subject to scheduled
        report-date duplicate protection.

    14. Manual execution does not bypass PAUSED or CANCELLED state.

    15. Scheduler execution and Oracle execution are deliberately
        kept in separate layers.
    """

    # =====================================================================
    # CONTROL STATES
    # =====================================================================

    CONTROL_ACTIVE = "ACTIVE"
    CONTROL_PAUSED = "PAUSED"
    CONTROL_CANCELLED = "CANCELLED"

    # =====================================================================
    # EXECUTION STATES
    # =====================================================================

    STATUS_PENDING = "PENDING"
    STATUS_RUNNING = "RUNNING"
    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILED = "FAILED"

    # =====================================================================
    # INITIALIZATION
    # =====================================================================

    def __init__(
        self,
        ready_repository,
        priority_queue,
        oracle_executor,
        execution_repository,
        job_control_repository=None,
        schedule_master_repository=None,
        schedule_extg_repository=None,
        retry_policy=None,
        holiday_evaluator=None,
    ):
        if ready_repository is None:
            raise ValueError(
                "ready_repository is required."
            )

        if priority_queue is None:
            raise ValueError(
                "priority_queue is required."
            )

        if oracle_executor is None:
            raise ValueError(
                "oracle_executor is required."
            )

        if execution_repository is None:
            raise ValueError(
                "execution_repository is required."
            )

        self.ready_repository = (
            ready_repository
        )

        self.priority_queue = (
            priority_queue
        )

        self.oracle_executor = (
            oracle_executor
        )

        self.execution_repository = (
            execution_repository
        )

        self.job_control_repository = (
            job_control_repository
        )

        self.schedule_master_repository = (
            schedule_master_repository
        )

        self.schedule_extg_repository = (
            schedule_extg_repository
        )

        self.retry_policy = (
            retry_policy
            or RetryPolicy(
                windows=RETRY_WINDOWS,
                lookback_days=RETRY_LOOKBACK_DAYS,
            )
        )
        self.holiday_evaluator = holiday_evaluator or HolidayEvaluator()

    # =====================================================================
    # EXECUTE AVAILABLE
    # =====================================================================

    def execute_available(
        self,
        max_jobs=None,
        current_datetime=None,
    ):
        """
        Execute jobs available in the priority queue.

        Parameters
        ----------
        max_jobs:
            Maximum number of jobs to execute.

            None:
                Execute until no executable job remains.

        Returns
        -------
        list
            Monitoring dictionaries.
        """

        if max_jobs is not None:

            if not isinstance(
                max_jobs,
                int,
            ):
                raise TypeError(
                    "max_jobs must be an integer or None."
                )

            if max_jobs <= 0:
                raise ValueError(
                    "max_jobs must be greater than zero."
                )

        results = []

        attempts = 0

        while not self.priority_queue.is_empty():

            if (
                max_jobs is not None
                and attempts >= max_jobs
            ):
                break

            result = self.execute_next(
                current_datetime=current_datetime,
            )

            if result is None:
                break

            results.append(
                result
            )

            attempts += 1

        return results

    # =====================================================================
    # EXECUTE NEXT
    # =====================================================================

    def execute_next(self, current_datetime=None):
        result = self._execute_next(current_datetime)
        self._capture_logging_decision(result)
        return result

    def _capture_logging_decision(self, result):
        event_logger = vars(self.execution_repository).get('event_logger')
        connection = getattr(self.execution_repository, 'connection', None)
        if event_logger is not None and connection is not None:
            WorkerLoggingObserver(event_logger, connection).capture_decision(result)

    def _execute_next(self, current_datetime=None):
        """
        Execute the highest-priority valid READY job.

        Returns
        -------
        dict or None
            Monitoring-friendly result.

        None means there is no executable job currently available.
        """

        current_datetime = current_datetime or datetime.now()

        while not self.priority_queue.is_empty():

            queue_job = (
                self.priority_queue.peek()
            )

            if queue_job is None:
                return None

            job_id = getattr(
                queue_job,
                "job_id",
                None,
            )

            if job_id is None:

                self.priority_queue.pop()

                continue

            # -------------------------------------------------------------
            # Re-read persistent READY state.
            #
            # The heap is derived state and may contain stale entries.
            # -------------------------------------------------------------

            occurrence_key = getattr(
                queue_job,
                "occurrence_key",
                None,
            )

            # Occurrence-aware workers carry the stable report-date key in
            # the derived heap.  Fall back to the original job-level lookup
            # for legacy repositories/queues so existing integrations remain
            # compatible.
            if (
                occurrence_key
                and hasattr(self.ready_repository, "get_by_key")
            ):
                ready_job = self.ready_repository.get_by_key(occurrence_key)
            else:
                ready_job = self.ready_repository.get_by_id(job_id)

            if ready_job is None:

                self.priority_queue.pop()

                continue

            # -------------------------------------------------------------
            # Control state
            # -------------------------------------------------------------

            control = self._get_control(
                job_id
            )

            control_status = (
                self._get_control_status(
                    control
                )
            )

            if control_status == self.CONTROL_PAUSED:

                self.priority_queue.pop()

                return {
                    "job_id": job_id,
                    "job_name": getattr(
                        ready_job,
                        "job_name",
                        None,
                    ),
                    "status": self.CONTROL_PAUSED,
                    "executed": False,
                    "reason": "Job is PAUSED.",
                }

            if control_status == self.CONTROL_CANCELLED:

                self.priority_queue.pop()

                return {
                    "job_id": job_id,
                    "job_name": getattr(
                        ready_job,
                        "job_name",
                        None,
                    ),
                    "status": self.CONTROL_CANCELLED,
                    "executed": False,
                    "reason": "Job is CANCELLED.",
                }

            # -------------------------------------------------------------
            # Resolve Schedule Master
            # -------------------------------------------------------------

            schedule_job = (
                self._get_schedule_job(
                    job_id
                )
            )

            if schedule_job is None:

                self.priority_queue.pop()

                return {
                    "job_id": job_id,
                    "job_name": getattr(
                        ready_job,
                        "job_name",
                        None,
                    ),
                    "status": self.STATUS_FAILED,
                    "executed": False,
                    "reason": (
                        "Schedule Master job "
                        "could not be found."
                    ),
                }

            max_attempts = self.retry_policy.max_attempts_for(schedule_job)

            # -------------------------------------------------------------
            # Resolve Oracle procedure
            # -------------------------------------------------------------

            if not self.holiday_evaluator.can_run_on_day(schedule_job, current_datetime.date()):
                self.priority_queue.pop()
                return {"job_id": job_id, "status": "CALENDAR_POLICY_BLOCKED", "executed": False,
                        "reason": "Job is not allowed to execute on this banking calendar day."}

            if not self._confirmation_allowed(schedule_job, job_id, occurrence_key):
                self.priority_queue.pop()
                return {"job_id": job_id, "status": "WAITING_CONFIRMATION", "executed": False,
                        "reason": "This occurrence needs confirmation before execution."}

            procedure_name = (
                self._get_procedure_name(
                    schedule_job
                )
            )

            if not procedure_name:

                self.priority_queue.pop()

                return {
                    "job_id": job_id,
                    "job_name": getattr(
                        ready_job,
                        "job_name",
                        None,
                    ),
                    "status": self.STATUS_FAILED,
                    "executed": False,
                    "reason": (
                        "No Oracle procedure/package "
                        "is configured for the job."
                    ),
                }

            # -------------------------------------------------------------
            # Report date
            # -------------------------------------------------------------

            report_date = getattr(
                ready_job,
                "report_date",
                None,
            )

            report_date = self._to_date(
                report_date
            )

            # -------------------------------------------------------------
            # Manual run
            # -------------------------------------------------------------

            manual_run = (
                self._is_manual_run(
                    control
                )
            )

            # -------------------------------------------------------------
            # Scheduled duplicate protection
            #
            # Manual runs deliberately bypass this section.
            # -------------------------------------------------------------

            if not manual_run:
                day_block = self._execution_day_block(job_id, report_date, current_datetime,
                                                      getattr(ready_job, "execution_date", None))
                if day_block:
                    self.priority_queue.pop()
                    return day_block

                if report_date is not None:

                    if self.execution_repository.has_success(
                        job_id=job_id,
                        report_date=report_date,
                    ):

                        self.priority_queue.pop()

                        self._delete_ready_occurrence(ready_job)

                        logger.info(
                            "Skipping duplicate successful "
                            "occurrence: job_id=%s report_date=%s",
                            job_id,
                            report_date,
                        )

                        return {
                            "job_id": job_id,
                            "job_name": getattr(
                                ready_job,
                                "job_name",
                                None,
                            ),
                            "status": self.STATUS_SUCCESS,
                            "executed": False,
                            "duplicate": True,
                            "reason": (
                                "Scheduled occurrence "
                                "already completed successfully."
                            ),
                        }

                    if self.execution_repository.has_running(
                        job_id=job_id,
                        report_date=report_date,
                    ):

                        self.priority_queue.pop()

                        logger.warning(
                            "Skipping already-running "
                            "occurrence: job_id=%s report_date=%s",
                            job_id,
                            report_date,
                        )

                        return {
                            "job_id": job_id,
                            "job_name": getattr(
                                ready_job,
                                "job_name",
                                None,
                            ),
                            "status": self.STATUS_RUNNING,
                            "executed": False,
                            "duplicate": True,
                            "reason": (
                                "Scheduled occurrence "
                                "is already running."
                            ),
                        }

            # -------------------------------------------------------------
            # Scheduled retry exhaustion
            #
            # Manual runs are deliberately not subject to the scheduled
            # retry limit. For scheduled occurrences, a FAILED attempt
            # consumes one attempt. Once the maximum has been reached,
            # keep the persistent READY record for monitoring but remove
            # the derived heap entry so Oracle is not called again.
            # -------------------------------------------------------------

            if not manual_run:

                latest_execution = (
                    self.execution_repository.get_latest(
                        job_id=job_id,
                        report_date=report_date,
                    )
                )

                if latest_execution is not None:

                    latest_status = str(
                        getattr(
                            latest_execution,
                            "status",
                            "",
                        )
                    ).strip().upper()

                    latest_attempt = int(
                        getattr(
                            latest_execution,
                            "attempt_no",
                            0,
                        )
                        or 0
                    )

                    if (
                        latest_attempt >= max_attempts
                    ):

                        self.priority_queue.pop()

                        logger.warning(
                            "Scheduled occurrence has exhausted retry "
                            "attempts: job_id=%s report_date=%s "
                            "attempt_no=%s max_attempts=%s",
                            job_id,
                            report_date,
                            latest_attempt,
                                                )

                        return {
                            "job_id": job_id,
                            "job_name": getattr(
                                ready_job,
                                "job_name",
                                None,
                            ),
                            "status": self.STATUS_FAILED,
                            "executed": False,
                            "duplicate": False,
                            "retry_exhausted": True,
                            "attempt_no": latest_attempt,
                            "max_attempts": max_attempts,
                            "report_date": report_date,
                            "reason": (
                                "Scheduled occurrence has "
                                "exhausted the maximum retry attempts."
                            ),
                        }

                    # A previous scheduled failure is never consumed through
                    # the normal business-time queue.  It remains visible in
                    # READY/history and is retried independently only in the
                    # global Oracle-compatible retry slots.  This prevents an
                    # old failed report date from interrupting an active/new
                    # job merely because it has a high age priority.
                    if latest_status == self.STATUS_FAILED:
                        if not self.retry_policy.is_retry_window(current_datetime):
                            self.priority_queue.pop()
                            return {
                                "job_id": job_id,
                                "job_name": getattr(ready_job, "job_name", None),
                                "status": "WAITING_RETRY_WINDOW",
                                "executed": False,
                                "report_date": report_date,
                                "reason": self.retry_policy.next_window_hint(current_datetime),
                            }


            # -------------------------------------------------------------
            # Remove from derived heap.
            #
            # Persistent READY state remains until execution succeeds.
            # -------------------------------------------------------------

            self.priority_queue.pop()

            # -------------------------------------------------------------
            # Schedule_extg -> PENDING
            #
            # Best effort only.
            # -------------------------------------------------------------

            extg_record = (
                self._create_extg_pending(
                    schedule_job=schedule_job,
                    ready_job=ready_job,
                    report_date=report_date,
                )
            )

            # -------------------------------------------------------------
            # SQLite execution_jobs -> RUNNING
            #
            # This is mandatory.
            #
            # Oracle MUST NOT execute if this fails.
            # -------------------------------------------------------------

            try:

                execution_id = (
                    self.execution_repository.start_execution(
                        job_id=job_id,
                        job_name=getattr(
                            ready_job,
                            "job_name",
                            None,
                        ),
                        procedure_name=procedure_name,
                        report_date=report_date,
                    )
                )

            except Exception as exc:

                logger.exception(
                    "Could not create SQLite RUNNING "
                    "execution record for job_id=%s.",
                    job_id,
                )

                self._mark_extg_failed(
                    extg_record=extg_record,
                    error=(
                        "Could not create SQLite "
                        "execution record: "
                        f"{exc}"
                    ),
                    error_type="ExecutionTrackingError",
                )

                # ---------------------------------------------------------
                # READY remains persistent.
                #
                # The next scheduler cycle can rebuild/re-evaluate it.
                # ---------------------------------------------------------

                return {
                    "job_id": job_id,
                    "job_name": getattr(
                        ready_job,
                        "job_name",
                        None,
                    ),
                    "status": self.STATUS_FAILED,
                    "executed": False,
                    "duplicate": False,
                    "reason": (
                        "SQLite execution tracking "
                        "could not be started."
                    ),
                    "error": str(exc),
                }

            execution_record = (
                self.execution_repository.get_by_id(
                    execution_id
                )
            )

            attempt_no = int(
                getattr(
                    execution_record,
                    "attempt_no",
                    1,
                )
                or 1
            )

            # -------------------------------------------------------------
            # Schedule_extg -> RUNNING
            # -------------------------------------------------------------

            self._mark_extg_running(
                extg_record=extg_record
            )

            # -------------------------------------------------------------
            # Oracle execution
            # -------------------------------------------------------------

            try:

                oracle_result = (
                    self._execute_oracle(
                        procedure_name=procedure_name,
                        report_date=report_date,
                    )
                )

            except Exception as exc:

                logger.exception(
                    "Unexpected OracleExecutor exception "
                    "for job_id=%s.",
                    job_id,
                )

                # ---------------------------------------------------------
                # Convert an unexpected executor exception into a normal
                # failed execution result.
                # ---------------------------------------------------------

                self._mark_execution_failed(
                    execution_id=execution_id,
                    error=str(exc),
                    error_type=(
                        "OracleExecutorException"
                    ),
                )

                self._mark_extg_failed(
                    extg_record=extg_record,
                    error=str(exc),
                    error_type=(
                        "OracleExecutorException"
                    ),
                )

                self._consume_manual_run_if_needed(
                    job_id=job_id,
                    manual_run=manual_run,
                )

                return {
                    "job_id": job_id,
                    "job_name": getattr(
                        ready_job,
                        "job_name",
                        None,
                    ),
                    "status": self.STATUS_FAILED,
                    "executed": True,
                    "duplicate": False,
                    "execution_id": execution_id,
                    "report_date": report_date,
                    "procedure_name": procedure_name,
                    "error": str(exc),
                    "error_type": (
                        "OracleExecutorException"
                    ),
                    "attempt_no": attempt_no,
                    "max_attempts": (
                        None
                        if manual_run
                        else max_attempts
                    ),
                    "retry_exhausted": (
                        (not manual_run)
                        and attempt_no >= max_attempts
                    ),
                }

            # -------------------------------------------------------------
            # Oracle returned a normal result
            # -------------------------------------------------------------

            if getattr(
                oracle_result,
                "success",
                False,
            ):

                count = getattr(
                    oracle_result,
                    "count",
                    None,
                )

                duration_seconds = getattr(
                    oracle_result,
                    "duration_seconds",
                    None,
                )

                # ---------------------------------------------------------
                # SQLite -> SUCCESS
                # ---------------------------------------------------------

                self._mark_execution_success(
                    execution_id=execution_id,
                    count=count,
                    duration_seconds=(
                        duration_seconds
                    ),
                )

                # ---------------------------------------------------------
                # Schedule_extg -> SUCCESS
                # ---------------------------------------------------------

                self._mark_extg_success(
                    extg_record=extg_record,
                    count=count,
                )

                # ---------------------------------------------------------
                # Remove persistent READY state.
                # ---------------------------------------------------------

                self._delete_ready_occurrence(ready_job)

                # ---------------------------------------------------------
                # Consume manual-run request.
                # ---------------------------------------------------------

                self._consume_manual_run_if_needed(
                    job_id=job_id,
                    manual_run=manual_run,
                )

                logger.info(
                    "Execution successful: "
                    "job_id=%s report_date=%s procedure=%s",
                    job_id,
                    report_date,
                    procedure_name,
                )

                return {
                    "job_id": job_id,
                    "job_name": getattr(
                        ready_job,
                        "job_name",
                        None,
                    ),
                    "status": self.STATUS_SUCCESS,
                    "executed": True,
                    "duplicate": False,
                    "execution_id": execution_id,
                    "report_date": report_date,
                    "procedure_name": procedure_name,
                    "count": count,
                    "duration_seconds": (
                        duration_seconds
                    ),
                    "attempt_no": attempt_no,
                    "max_attempts": (
                        None
                        if manual_run
                        else max_attempts
                    ),
                    "retry_exhausted": False,
                }

            # -------------------------------------------------------------
            # Oracle FAILED
            # -------------------------------------------------------------

            error = getattr(
                oracle_result,
                "error",
                None,
            )

            error_type = getattr(
                oracle_result,
                "error_type",
                None,
            )

            duration_seconds = getattr(
                oracle_result,
                "duration_seconds",
                None,
            )

            # -------------------------------------------------------------
            # SQLite -> FAILED
            # -------------------------------------------------------------

            self._mark_execution_failed(
                execution_id=execution_id,
                error=error,
                error_type=error_type,
                duration_seconds=(
                    duration_seconds
                ),
            )

            # -------------------------------------------------------------
            # Schedule_extg -> FAILED
            # -------------------------------------------------------------

            self._mark_extg_failed(
                extg_record=extg_record,
                error=error,
                error_type=error_type,
            )

            # -------------------------------------------------------------
            # Manual request is one-shot even when Oracle fails.
            # A new manual run must be explicitly requested.
            # -------------------------------------------------------------

            self._consume_manual_run_if_needed(
                job_id=job_id,
                manual_run=manual_run,
            )

            logger.error(
                "Execution failed: "
                "job_id=%s report_date=%s procedure=%s "
                "error=%s",
                job_id,
                report_date,
                procedure_name,
                error,
            )

            return {
                "job_id": job_id,
                "job_name": getattr(
                    ready_job,
                    "job_name",
                    None,
                ),
                "status": self.STATUS_FAILED,
                "executed": True,
                "duplicate": False,
                "execution_id": execution_id,
                "report_date": report_date,
                "procedure_name": procedure_name,
                "error": error,
                "error_type": error_type,
                "duration_seconds": (
                    duration_seconds
                ),
                "attempt_no": attempt_no,
                "max_attempts": (
                    None
                    if manual_run
                    else max_attempts
                ),
                "retry_exhausted": (
                    (not manual_run)
                    and attempt_no >= max_attempts
                ),
            }

        return None

    # =====================================================================
    # HISTORICAL FAILURE RETRY
    # =====================================================================

    def prepare_retry_candidates(self, current_datetime, max_jobs=None):
        """Capture retry candidates before normal READY work is executed.

        The capture makes retry processing non-disruptive: a normal run that
        succeeds or starts an occurrence in the same cycle changes the latest
        execution row, and ``execute_retry_candidates`` then safely skips the
        stale captured candidate.
        """
        current_datetime = current_datetime or datetime.now()
        if not self.retry_policy.is_retry_window(current_datetime):
            return []
        limit = max_jobs if max_jobs is not None else 100
        return self.execution_repository.get_retry_candidates(
            current_date=current_datetime.date(),
            lookback_days=self.retry_policy.lookback_days,
            limit=limit,
        )

    def execute_retry_candidates(self, candidates, current_datetime=None, max_jobs=1):
        """Retry captured historical failures serially in the global window.

        A retry bypasses a job's business ``RUN_BY`` window exactly as the
        former Oracle package did. It still honours active/paused/cancelled
        controls and rechecks execution history immediately before claiming
        Oracle work. No active Oracle work is interrupted.
        """
        current_datetime = current_datetime or datetime.now()
        if not self.retry_policy.is_retry_window(current_datetime):
            return []
        if max_jobs is None:
            max_jobs = 1
        max_jobs = max(1, int(max_jobs))

        results = []
        for candidate in list(candidates or []):
            if len(results) >= max_jobs:
                break
            result = self._execute_retry_candidate(candidate, current_datetime)
            if result is not None:
                results.append(result)
        return results

    def _execute_retry_candidate(self, candidate, current_datetime):
        result = self._execute_retry_candidate_impl(candidate, current_datetime)
        self._capture_logging_decision(result)
        return result

    def _execute_retry_candidate_impl(self, candidate, current_datetime):
        job_id = getattr(candidate, "job_id", None)
        report_date = self._to_date(getattr(candidate, "report_date", None))
        candidate_id = getattr(candidate, "id", None)
        if job_id is None or report_date is None or candidate_id is None:
            return None
        latest = self.execution_repository.get_latest(job_id, report_date)
        if (
            latest is None
            or getattr(latest, "id", None) != candidate_id
            or str(getattr(latest, "status", "")).upper() != self.STATUS_FAILED
        ):
            return None
        if self.execution_repository.has_success(job_id, report_date) or self.execution_repository.has_running(job_id, report_date):
            return None

        control = self._get_control(job_id)
        control_status = self._get_control_status(control)
        if control_status != self.CONTROL_ACTIVE:
            return {
                "job_id": job_id,
                "job_name": getattr(candidate, "job_name", None),
                "status": control_status,
                "executed": False,
                "report_date": report_date,
                "reason": "Historical retry respects the current job control state.",
            }

        schedule_job = self._get_schedule_job(job_id)
        if schedule_job is None or not self._is_master_active(schedule_job):
            return {
                "job_id": job_id,
                "job_name": getattr(candidate, "job_name", None),
                "status": "RETRY_CONFIGURATION_UNAVAILABLE",
                "executed": False,
                "report_date": report_date,
                "reason": "No active Scheduler Master configuration is available for this failed occurrence.",
            }
        day_block = self._execution_day_block(job_id, report_date, current_datetime)
        if day_block:
            return day_block
        max_attempts = self.retry_policy.max_attempts_for(schedule_job)
        latest_attempt = int(getattr(latest, "attempt_no", 0) or 0)
        if latest_attempt >= max_attempts:
            return {"job_id": job_id, "status": self.STATUS_FAILED, "executed": False,
                    "report_date": report_date, "retry_exhausted": True,
                    "attempt_no": latest_attempt, "max_attempts": max_attempts,
                    "reason": "Scheduled occurrence has exhausted its total attempt limit."}
        if not self._confirmation_allowed(schedule_job, job_id, f"{job_id}:{report_date.isoformat()}"):
            return {"job_id": job_id, "status": "WAITING_CONFIRMATION", "executed": False,
                    "reason": "This failed occurrence needs confirmation before retry."}
        if not self.holiday_evaluator.can_run_on_day(schedule_job, current_datetime.date()):
            return {"job_id": job_id, "status": "CALENDAR_POLICY_BLOCKED", "executed": False,
                    "report_date": report_date, "reason": "Historical retry respects the execution-day banking calendar."}
        procedure_name = self._get_procedure_name(schedule_job)
        if not procedure_name:
            return {
                "job_id": job_id,
                "job_name": getattr(candidate, "job_name", None),
                "status": "RETRY_CONFIGURATION_UNAVAILABLE",
                "executed": False,
                "report_date": report_date,
                "reason": "The active Scheduler Master row has no Oracle procedure reference.",
            }

        # Schedule_extg identifies the occurrence, not individual attempts.
        # Its create_pending implementation reopens the prior FAILED record
        # for the same name/report date and the detailed attempt remains in
        # SQLite execution history.
        extg_record = self._create_extg_pending(
            schedule_job=schedule_job,
            ready_job=candidate,
            report_date=report_date,
        )
        try:
            execution_id = self.execution_repository.start_execution(
                job_id=job_id,
                job_name=getattr(candidate, "job_name", None) or getattr(schedule_job, "name", None),
                procedure_name=procedure_name,
                report_date=report_date,
            )
        except Exception as exc:
            logger.exception("Could not create retry execution tracking for job_id=%s.", job_id)
            self._mark_extg_failed(
                extg_record,
                error="Could not create local retry execution tracking.",
                error_type="ExecutionTrackingError",
            )
            return {
                "job_id": job_id,
                "job_name": getattr(candidate, "job_name", None),
                "status": self.STATUS_FAILED,
                "executed": False,
                "retry": True,
                "report_date": report_date,
                "reason": "Local retry execution tracking could not be started.",
            }

        execution_record = self.execution_repository.get_by_id(execution_id)
        attempt_no = int(getattr(execution_record, "attempt_no", 1) or 1)
        self._mark_extg_running(extg_record)
        try:
            oracle_result = self._execute_oracle(
                procedure_name=procedure_name,
                report_date=report_date,
            )
        except Exception as exc:
            logger.exception("Unexpected Oracle retry exception for job_id=%s.", job_id)
            self._mark_execution_failed(
                execution_id=execution_id,
                error=str(exc),
                error_type="OracleExecutorException",
            )
            self._mark_extg_failed(extg_record, error=str(exc), error_type="OracleExecutorException")
            return {
                "job_id": job_id,
                "job_name": getattr(candidate, "job_name", None),
                "status": self.STATUS_FAILED,
                "executed": True,
                "retry": True,
                "execution_id": execution_id,
                "attempt_no": attempt_no,
            "max_attempts": max_attempts,
            "retry_exhausted": attempt_no >= max_attempts,
                "max_attempts": max_attempts,
                "retry_exhausted": attempt_no >= max_attempts,
                "report_date": report_date,
                "procedure_name": procedure_name,
                "error": str(exc),
                "error_type": "OracleExecutorException",
            }

        if getattr(oracle_result, "success", False):
            count = getattr(oracle_result, "count", None)
            duration_seconds = getattr(oracle_result, "duration_seconds", None)
            self._mark_execution_success(execution_id, count=count, duration_seconds=duration_seconds)
            self._mark_extg_success(extg_record, count=count)
            logger.info("Historical retry succeeded: job_id=%s report_date=%s", job_id, report_date)
            return {
                "job_id": job_id,
                "job_name": getattr(candidate, "job_name", None),
                "status": self.STATUS_SUCCESS,
                "executed": True,
                "retry": True,
                "execution_id": execution_id,
                "attempt_no": attempt_no,
            "max_attempts": max_attempts,
            "retry_exhausted": attempt_no >= max_attempts,
                "max_attempts": max_attempts,
                "retry_exhausted": attempt_no >= max_attempts,
                "report_date": report_date,
                "procedure_name": procedure_name,
                "count": count,
                "duration_seconds": duration_seconds,
            }

        error = getattr(oracle_result, "error", None)
        error_type = getattr(oracle_result, "error_type", None)
        duration_seconds = getattr(oracle_result, "duration_seconds", None)
        self._mark_execution_failed(
            execution_id=execution_id,
            error=error,
            error_type=error_type,
            duration_seconds=duration_seconds,
        )
        self._mark_extg_failed(extg_record, error=error, error_type=error_type)
        logger.error("Historical retry failed: job_id=%s report_date=%s", job_id, report_date)
        return {
            "job_id": job_id,
            "job_name": getattr(candidate, "job_name", None),
            "status": self.STATUS_FAILED,
            "executed": True,
            "retry": True,
            "execution_id": execution_id,
            "attempt_no": attempt_no,
            "max_attempts": max_attempts,
            "retry_exhausted": attempt_no >= max_attempts,
            "report_date": report_date,
            "procedure_name": procedure_name,
            "error": error,
            "error_type": error_type,
            "duration_seconds": duration_seconds,
        }

    # =====================================================================
    # RECOVER RUNNING EXECUTIONS
    # =====================================================================

    def _execution_day_block(self, job_id, report_date, current_datetime, planned_date=None):
        """Fence automatic work to its durable planned day using the actual cycle clock."""
        if planned_date is None:
            getter = getattr(self.execution_repository, "get_planned_execution_date", None)
            planned_date = getter(job_id, report_date) if callable(getter) else None
        try:
            planned_date = self._to_date(planned_date)
        except (TypeError, ValueError):
            planned_date = None
        common = {"job_id": job_id, "report_date": report_date, "execution_date": planned_date,
                  "executed": False}
        if planned_date is None:
            return {**common, "status": "EXECUTION_DATE_UNKNOWN", "manual_run_required": True,
                    "reason": "The planned execution date is unknown; automatic execution is disabled."}
        today = current_datetime.date()
        if today > planned_date:
            return {**common, "status": "MANUAL_REQUIRED", "manual_run_required": True,
                    "reason": "The planned execution day has passed; an explicit manual run is required."}
        if today < planned_date:
            return {**common, "status": "WAITING_EXECUTION_DATE",
                    "reason": "Waiting for the planned execution day."}
        return None

    def _confirmation_allowed(self, schedule_job, job_id, occurrence_key):
        evaluator = ConfirmationEvaluator()
        if not evaluator.is_required(schedule_job):
            return True
        getter = getattr(self.job_control_repository, "get_for_occurrence", None)
        control = getter(job_id, occurrence_key) if occurrence_key and callable(getter) else self._get_control(job_id)
        return evaluator.can_proceed(schedule_job, control)

    def _execute_oracle(self, procedure_name, report_date):
        """Keep the control plane live only during the external procedure call.

        The worker has committed RUNNING and removed the heap entry before
        this boundary. All SQLite/heap work still takes the shared lock;
        another scheduler worker cannot enter because the process guard holds.
        """
        lock = getattr(self, "oracle_io_lock", None)
        if lock is not None:
            lock.release()
        try:
            return self.oracle_executor.execute(procedure_name=procedure_name, report_date=report_date)
        finally:
            if lock is not None:
                lock.acquire()

    def recover_running_executions(self):
        """
        Recover orphaned RUNNING SQLite executions after a process restart.

        The current ExecutionRepository.recover_running() returns the
        number of recovered records.

        Returns
        -------
        int
            Number of recovered executions.
        """

        try:

            recovered = (
                self.execution_repository.recover_running()
            )

        except Exception:

            logger.exception(
                "Failed to recover orphaned RUNNING executions."
            )

            raise

        if recovered:

            logger.warning(
                "Recovered %s orphaned RUNNING execution(s).",
                recovered,
            )

        return recovered

    # =====================================================================
    # REBUILD QUEUE
    # =====================================================================

    def _delete_ready_occurrence(self, ready_job):
        """Remove only the READY occurrence that has completed/duplicated.

        Old READY repositories are keyed by ``job_id``.  The occurrence-aware
        repository provides ``delete_by_key`` so a successful DAILY report
        cannot remove a concurrently queued FORTNIGHTLY report for the same
        master job.
        """

        occurrence_key = getattr(ready_job, "occurrence_key", None)
        if occurrence_key and hasattr(self.ready_repository, "delete_by_key"):
            return self.ready_repository.delete_by_key(occurrence_key)
        return self.ready_repository.delete(getattr(ready_job, "job_id", None))

    def rebuild_queue(self):
        """
        Rebuild the derived priority heap from persistent READY state.

        Useful after:

            - process restart
            - queue corruption
            - manual queue manipulation
        """

        ready_jobs = (
            self.ready_repository.get_all()
        )

        if hasattr(
            self.priority_queue,
            "rebuild",
        ):

            self.priority_queue.rebuild(
                ready_jobs
            )

            return ready_jobs

        if hasattr(
            self.priority_queue,
            "clear",
        ):

            self.priority_queue.clear()

        for job in ready_jobs:

            self.priority_queue.push(
                job
            )

        return ready_jobs

    # =====================================================================
    # CONTROL
    # =====================================================================

    def _get_control(
        self,
        job_id,
    ):
        """
        Read current Job Control state.
        """

        if self.job_control_repository is None:
            return None

        if hasattr(
            self.job_control_repository,
            "get",
        ):

            return self.job_control_repository.get(
                job_id
            )

        if hasattr(
            self.job_control_repository,
            "get_by_id",
        ):

            return self.job_control_repository.get_by_id(
                job_id
            )

        return None

    # =====================================================================
    # CONTROL STATUS
    # =====================================================================

    @classmethod
    def _get_control_status(
        cls,
        control,
    ):
        """
        Extract control_status from either a model or dictionary.

        The current SQLite column is:

            control_status

        not:

            status
        """

        if control is None:
            return cls.CONTROL_ACTIVE

        if isinstance(
            control,
            dict,
        ):

            value = control.get(
                "control_status"
            )

            if value is None:

                value = control.get(
                    "status"
                )

        else:

            value = getattr(
                control,
                "control_status",
                None,
            )

            if value is None:

                value = getattr(
                    control,
                    "status",
                    None,
                )

        if value is None:
            return cls.CONTROL_ACTIVE

        return str(
            value
        ).strip().upper()

    # =====================================================================
    # MANUAL RUN
    # =====================================================================

    @staticmethod
    def _is_manual_run(
        control,
    ):
        """
        Determine whether manual execution was requested.
        """

        if control is None:
            return False

        if isinstance(
            control,
            dict,
        ):

            value = control.get(
                "manual_run",
                0,
            )

        else:

            value = getattr(
                control,
                "manual_run",
                0,
            )

        if isinstance(
            value,
            bool,
        ):

            return value

        if isinstance(
            value,
            int,
        ):

            return value == 1

        return str(
            value
        ).strip().upper() in {
            "1",
            "TRUE",
            "YES",
            "Y",
        }

    # =====================================================================
    # CLEAR MANUAL RUN
    # =====================================================================

    def _clear_manual_run(
        self,
        job_id,
    ):
        """
        Clear one-shot manual execution request.

        Cleanup failure must not turn a successful Oracle execution
        into a failed execution.
        """

        if self.job_control_repository is None:
            return

        method = getattr(
            self.job_control_repository,
            "clear_manual_run",
            None,
        )

        if method is None:
            return

        try:

            method(
                job_id
            )

        except Exception:

            logger.exception(
                "Failed to clear manual_run for job_id=%s.",
                job_id,
            )

    # =====================================================================
    # CLEAR DATETIME OVERRIDE
    # =====================================================================

    def _clear_override_datetime(
        self,
        job_id,
    ):
        """
        Clear a one-shot manual datetime override.

        Cleanup failure must not turn a completed Oracle execution
        into a failed execution.
        """

        if self.job_control_repository is None:
            return

        method = getattr(
            self.job_control_repository,
            "clear_override_datetime",
            None,
        )

        if method is None:
            method = getattr(
                self.job_control_repository,
                "clear_override",
                None,
            )

        if method is None:
            return

        try:
            method(
                job_id
            )
        except Exception:
            logger.exception(
                "Failed to clear override_datetime for job_id=%s.",
                job_id,
            )

    # =====================================================================
    # MANUAL RUN CONSUMPTION
    # =====================================================================

    def _consume_manual_run_if_needed(
        self,
        job_id,
        manual_run,
    ):
        """
        Consume the complete one-shot manual execution request.

        A manual request consists of the manual_run flag and, when
        supplied, a datetime override. Both are temporary controls
        and must be consumed together after the manual execution
        attempt reaches Oracle.
        """

        if not manual_run:
            return

        event_logger = vars(self.execution_repository).get('event_logger')
        connection = getattr(self.execution_repository, 'connection', None)
        if event_logger is not None and connection is not None:
            consume_manual_request(event_logger, connection, job_id)
            return

        self._clear_manual_run(
            job_id
        )

        self._clear_override_datetime(
            job_id
        )

    # =====================================================================
    # SCHEDULE MASTER
    # =====================================================================

    def _get_schedule_job(
        self,
        job_id,
    ):
        """
        Resolve Schedule Master by ID.
        """

        if self.schedule_master_repository is None:
            return None

        if hasattr(
            self.schedule_master_repository,
            "get_by_id",
        ):

            return self.schedule_master_repository.get_by_id(
                job_id
            )

        if hasattr(
            self.schedule_master_repository,
            "get_all",
        ):

            jobs = (
                self.schedule_master_repository.get_all()
            )

            for job in jobs:

                if getattr(
                    job,
                    "id",
                    None,
                ) == job_id:

                    return job

        return None

    @staticmethod
    def _is_master_active(schedule_job):
        """Read the Oracle/file master active flag without truthiness traps."""
        if isinstance(schedule_job, dict):
            value = schedule_job.get("is_active", 1)
        else:
            value = getattr(schedule_job, "is_active", 1)
        if isinstance(value, str):
            return value.strip().upper() in {"1", "Y", "YES", "TRUE", "ACTIVE"}
        return bool(value)

    # =====================================================================
    # PROCEDURE NAME
    # =====================================================================

    @staticmethod
    def _get_procedure_name(
        schedule_job,
    ):
        """
        Resolve Oracle procedure/package reference.

        Schedule Master currently stores this as package_name.

        Example:

            SCHEDULE_EXTRACTS.GPB
        """

        if schedule_job is None:
            return None

        if isinstance(
            schedule_job,
            dict,
        ):

            procedure_name = (
                schedule_job.get(
                    "package_name"
                )
            )

            if procedure_name is None:

                procedure_name = (
                    schedule_job.get(
                        "procedure_name"
                    )
                )

        else:

            procedure_name = getattr(
                schedule_job,
                "package_name",
                None,
            )

            if procedure_name is None:

                procedure_name = getattr(
                    schedule_job,
                    "procedure_name",
                    None,
                )

        if procedure_name is None:
            return None

        procedure_name = str(
            procedure_name
        ).strip()

        if not procedure_name:
            return None

        return procedure_name

    # =====================================================================
    # SCHEDULE_EXTG - PENDING
    # =====================================================================

    def _create_extg_pending(
        self,
        schedule_job,
        ready_job,
        report_date,
    ):
        """
        Create Schedule_extg PENDING record.

        Schedule_extg is best-effort.
        """

        repository = (
            self.schedule_extg_repository
        )

        if repository is None:
            return None

        name = getattr(
            ready_job,
            "job_name",
            None,
        )

        if not name:
            name = getattr(
                schedule_job,
                "name",
                None,
            )

        if not name:
            return None

        same_day = getattr(
            schedule_job,
            "same_day",
            0,
        )

        time_flag = getattr(
            schedule_job,
            "time_flag",
            0,
        )

        confirmation = getattr(
            schedule_job,
            "confirmation_needed",
            0,
        )

        run_config = getattr(
            schedule_job,
            "run_config",
            None,
        )

        try:

            return repository.create_pending(
                name=name,
                report_date=report_date,
                same_day=same_day,
                time_flag=time_flag,
                run_config=run_config,
                confirmation=confirmation,
            )

        except Exception:

            logger.exception(
                "Failed to create Schedule_extg PENDING "
                "record for job=%s report_date=%s.",
                name,
                report_date,
            )

            return None

    # =====================================================================
    # SCHEDULE_EXTG - RUNNING
    # =====================================================================

    def _mark_extg_running(
        self,
        extg_record,
    ):
        """
        Mark Schedule_extg RUNNING.

        Best effort only.
        """

        if (
            self.schedule_extg_repository is None
            or extg_record is None
        ):
            return

        record_id = (
            self._extg_record_id(
                extg_record
            )
        )

        try:

            if record_id is not None:

                self.schedule_extg_repository.mark_running(
                    record_id=record_id
                )

                return

            name = (
                self._extg_record_name(
                    extg_record
                )
            )

            report_date = (
                self._extg_record_report_date(
                    extg_record
                )
            )

            if name:

                self.schedule_extg_repository.mark_running(
                    name=name,
                    report_date=report_date,
                )

        except Exception:

            logger.exception(
                "Failed to mark Schedule_extg RUNNING."
            )

    # =====================================================================
    # SCHEDULE_EXTG - SUCCESS
    # =====================================================================

    def _mark_extg_success(
        self,
        extg_record,
        count=None,
    ):
        """
        Mark Schedule_extg SUCCESS.

        Best effort only.
        """

        if (
            self.schedule_extg_repository is None
            or extg_record is None
        ):
            return

        record_id = (
            self._extg_record_id(
                extg_record
            )
        )

        try:

            if record_id is not None:

                self.schedule_extg_repository.mark_success(
                    record_id=record_id,
                    count=count,
                )

                return

            name = (
                self._extg_record_name(
                    extg_record
                )
            )

            report_date = (
                self._extg_record_report_date(
                    extg_record
                )
            )

            if name:

                self.schedule_extg_repository.mark_success(
                    name=name,
                    report_date=report_date,
                    count=count,
                )

        except Exception:

            logger.exception(
                "Failed to mark Schedule_extg SUCCESS."
            )

    # =====================================================================
    # SCHEDULE_EXTG - FAILED
    # =====================================================================

    def _mark_extg_failed(
        self,
        extg_record,
        error=None,
        error_type=None,
    ):
        """
        Mark Schedule_extg FAILED.

        Best effort only.
        """

        if (
            self.schedule_extg_repository is None
            or extg_record is None
        ):
            return

        record_id = (
            self._extg_record_id(
                extg_record
            )
        )

        try:

            if record_id is not None:

                self.schedule_extg_repository.mark_failed(
                    record_id=record_id,
                    error=error,
                    error_type=error_type,
                )

                return

            name = (
                self._extg_record_name(
                    extg_record
                )
            )

            report_date = (
                self._extg_record_report_date(
                    extg_record
                )
            )

            if name:

                self.schedule_extg_repository.mark_failed(
                    name=name,
                    report_date=report_date,
                    error=error,
                    error_type=error_type,
                )

        except Exception:

            logger.exception(
                "Failed to mark Schedule_extg FAILED."
            )

    # =====================================================================
    # SCHEDULE_EXTG HELPERS
    # =====================================================================

    @staticmethod
    def _extg_record_id(
        record,
    ):
        if record is None:
            return None

        if isinstance(
            record,
            dict,
        ):

            return record.get(
                "id"
            )

        return getattr(
            record,
            "id",
            None,
        )

    @staticmethod
    def _extg_record_name(
        record,
    ):
        if record is None:
            return None

        if isinstance(
            record,
            dict,
        ):

            return record.get(
                "name"
            )

        return getattr(
            record,
            "name",
            None,
        )

    @classmethod
    def _extg_record_report_date(
        cls,
        record,
    ):
        if record is None:
            return None

        if isinstance(
            record,
            dict,
        ):

            value = record.get(
                "report_date"
            )

        else:

            value = getattr(
                record,
                "report_date",
                None,
            )

        return cls._to_date(
            value
        )

    # =====================================================================
    # SQLITE EXECUTION - SUCCESS
    # =====================================================================

    def _mark_execution_success(
        self,
        execution_id,
        count=None,
        duration_seconds=None,
    ):
        """
        Mark SQLite execution SUCCESS.

        Supports the current repository API.
        """

        self.execution_repository.mark_success(
            execution_id=execution_id,
            count=count,
            duration_seconds=duration_seconds,
        )

    # =====================================================================
    # SQLITE EXECUTION - FAILED
    # =====================================================================

    def _mark_execution_failed(
        self,
        execution_id,
        error,
        error_type=None,
        duration_seconds=None,
    ):
        """
        Mark SQLite execution FAILED.
        """

        self.execution_repository.mark_failed(
            execution_id=execution_id,
            error=error,
            error_type=error_type,
            duration_seconds=duration_seconds,
        )

    # =====================================================================
    # DATE CONVERSION
    # =====================================================================

    @staticmethod
    def _to_date(
        value,
    ):
        """
        Convert supported date values to datetime.date.
        """

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

            # ISO date.
            try:

                return date.fromisoformat(
                    value
                )

            except ValueError:
                pass

            # ISO datetime.
            try:

                return datetime.fromisoformat(
                    value
                ).date()

            except ValueError:
                pass

            # Common Oracle/application formats.
            for fmt in (
                "%d-%b-%Y",
                "%d-%B-%Y",
                "%d/%m/%Y",
                "%d-%m-%Y",
                "%Y/%m/%d",
                "%Y%m%d",
            ):

                try:

                    return datetime.strptime(
                        value,
                        fmt,
                    ).date()

                except ValueError:
                    continue

            raise ValueError(
                f"Unsupported date value: {value!r}"
            )

        raise TypeError(
            f"Unsupported date value: {value!r}"
        )
