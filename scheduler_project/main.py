import argparse
import json
import logging
import os
import sys
import time
import threading
from datetime import datetime, timedelta

from config.settings import (
    BASE_DIR,
    SCHEDULER_INTERVAL_SECONDS,
    MAX_EXECUTIONS_PER_CYCLE,
    MAX_RETRY_EXECUTIONS_PER_CYCLE,
    LOG_FILE,
    DATEMASTER_FILE,
    HOLIDAY_MASTER_FILE,
    CALENDAR_RETRY_SECONDS,
    SCHEDULE_EXTG_FILE,
    WORK_START_TIME,
    WORK_END_TIME,
)

from instance_lock import (
    SchedulerInstanceLock,
)

from database.sqlite_db import (
    create_tables,
    get_connection,
)

from repositories.schedule_master_repository import (
    ScheduleMasterRepository,
)

from repositories.master_configuration_repository import (
    MasterConfigurationRepository,
)

from repositories.oracle_schedule_master_repository import (
    OracleCalendarSnapshotRepository,
    OracleMasterSyncError,
)

from repositories.staging_repository import (
    StagingRepository,
)

from repositories.ready_repository import (
    ReadyRepository,
)

from repositories.occurrence_repository import (
    OccurrenceReadyRepository,
    OccurrenceStagingRepository,
)

from repositories.job_control_repository import (
    JobControlRepository,
)

from repositories.operations_repository import (
    OperationsRepository,
)

from repositories.execution_repository import (
    ExecutionRepository,
)
from repositories.oracle_logging_repository import OracleLoggingRepository
from repositories.worker_logging import WorkerLoggingObserver
from repositories.oracle_schedule_extg_mirror import OracleScheduleExtgMirror

from repositories.schedule_extg_repository import (
    ScheduleExtgRepository,
)

from scheduler.frequency import (
    FrequencyEvaluator,
)

from scheduler.holiday import (
    HolidayEvaluator,
)

from scheduler.margin import (
    MarginCalculator,
)

from scheduler.confirmation import (
    ConfirmationEvaluator,
)

from scheduler.time_window import (
    TimeWindowEvaluator,
)

from scheduler.eligibility import (
    EligibilityEvaluator,
)

from scheduler.priority import (
    PriorityCalculator,
)

from scheduler.staging import (
    StagingManager,
)

from scheduler.scheduler import (
    Scheduler,
)

from scheduler.occurrence import (
    OracleCompatibleOccurrencePlanner,
)

from scheduler.occurrence_scheduler import (
    OccurrenceScheduler,
)

from scheduler_queue.priority_queue import (
    PriorityQueue,
)

from scheduler.datemast import (
    DateMast,
)

from scheduler.upcoming import (
    UpcomingPlanner,
)

from execution.oracle_executor import (
    OracleExecutor,
)

from execution.execution_manager import (
    ExecutionManager,
)

from control_api import (
    SchedulerControlApi,
    start_control_api,
)


# =============================================================
# Logging
# =============================================================

def _configure_logging():
    """Prefer file logging without blocking a duplicate worker before locking.

    Some Windows configurations keep the service log open with restrictive
    sharing flags. A five-minute trigger must still reach the worker mutex and
    report a safe skip rather than failing during module import.
    """
    options = {
        "level": logging.INFO,
        "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
    }
    try:
        logging.basicConfig(filename=LOG_FILE, **options)
    except OSError:
        logging.basicConfig(stream=sys.stderr, **options)
        logging.getLogger("scheduler").warning(
            "Could not open scheduler log file; using standard error for this process."
        )


_configure_logging()

logger = logging.getLogger(
    "scheduler"
)


# =============================================================
# Application construction
# =============================================================

def create_application():
    """
    Build the complete scheduler application.

    The application contains:

        Schedule Master
              |
              v
        Scheduler
              |
        +-----+------+
        |            |
        v            v
      STAGING      READY
                     |
                     v
              Priority Queue
                     |
                     v
             Execution Manager
                     |
              +------+------+
              |             |
              v             v
       SQLite execution   Oracle
              |
              v
       Schedule_extg.json
    """

    logger.info(
        "Creating scheduler application."
    )

    # ---------------------------------------------------------
    # SQLite
    # ---------------------------------------------------------

    create_tables()

    connection = get_connection()
    oracle_logging = OracleLoggingRepository(connection=connection)

    logger.info(
        "SQLite connection created."
    )

    # ---------------------------------------------------------
    # Repositories
    # ---------------------------------------------------------

    schedule_master_repository = (
        ScheduleMasterRepository()
    )

    # The web/CLI control plane receives only this bounded adapter.  It can
    # change an existing job's active state and RUN_CONFIG.RUN_BY window, not
    # arbitrary Scheduler Master fields or SQL.
    master_configuration_repository = (
        MasterConfigurationRepository(
            schedule_master_repository
        )
    )

    # Occurrence-scoped repositories retain one row per report date.  The
    # legacy job-keyed tables are migrated by create_tables() and remain only
    # for backwards-compatible readers; the active worker must not collapse
    # DAILY + FORTNIGHTLY rows into one job_id.
    staging_repository = OccurrenceStagingRepository(connection)
    ready_repository = OccurrenceReadyRepository(connection)

    job_control_repository = (
        JobControlRepository(
            connection
        )
    )

    operations_repository = (
        OperationsRepository(
            connection, event_logger=oracle_logging
        )
    )

    # IMPORTANT:
    # This is the active ExecutionRepository.
    #
    # Do not use:
    #
    #     execution.execution_repository
    #
    # The active repository is:
    #
    #     repositories.execution_repository
    #
    execution_repository = (
        ExecutionRepository(
            connection, event_logger=oracle_logging
        )
    )

    schedule_extg_repository = (
        ScheduleExtgRepository(
            SCHEDULE_EXTG_FILE,
            oracle_mirror=OracleScheduleExtgMirror(enabled=False) if oracle_logging.enabled else None,
        )
    )

    # ---------------------------------------------------------
    # Holiday evaluator
    # ---------------------------------------------------------
    #
    # The same evaluator supplies execution, margins and API day labels.
    # Bind the live DATEMAST provider below for observed dates through T-1;
    # bank defaults and configured holidays cover provisional dates.
    #
    # HolidayEvaluator determines:
    #
    #     WORKING_DAY
    #     SAT
    #     SUN
    #     HOLIDAY
    #
    # Actual holiday dates can later be loaded into this set
    # from the application's holiday source.
    # ---------------------------------------------------------

    holiday_evaluator = (
        HolidayEvaluator(
            holidays=set(),
            file_path=HOLIDAY_MASTER_FILE,
        )
    )

    # ---------------------------------------------------------
    # Frequency
    # ---------------------------------------------------------

    frequency_evaluator = (
        FrequencyEvaluator(
            working_day_checker=holiday_evaluator.is_working_day
        )
    )

    # ---------------------------------------------------------
    # Margin
    # ---------------------------------------------------------

    margin_calculator = (
        MarginCalculator(
            working_day_checker=(
                holiday_evaluator.is_working_day
            )
        )
    )

    # ---------------------------------------------------------
    # Confirmation
    # ---------------------------------------------------------

    confirmation_evaluator = (
        ConfirmationEvaluator()
    )

    # ---------------------------------------------------------
    # Time window
    # ---------------------------------------------------------

    time_window_evaluator = (
        TimeWindowEvaluator(
            work_start_time=WORK_START_TIME,
            work_end_time=WORK_END_TIME,
        )
    )

    # ---------------------------------------------------------
    # Eligibility
    # ---------------------------------------------------------

    eligibility_evaluator = (
        EligibilityEvaluator(
            frequency_evaluator=(
                frequency_evaluator
            ),
            holiday_evaluator=(
                holiday_evaluator
            ),
            margin_calculator=(
                margin_calculator
            ),
            confirmation_evaluator=(
                confirmation_evaluator
            ),
            time_window_evaluator=(
                time_window_evaluator
            ),
        )
    )

    # ---------------------------------------------------------
    # Priority
    # ---------------------------------------------------------

    priority_calculator = (
        PriorityCalculator()
    )

    # ---------------------------------------------------------
    # Staging manager
    # ---------------------------------------------------------

    staging_manager = (
        StagingManager(
            staging_repository=(
                staging_repository
            ),
            ready_repository=(
                ready_repository
            ),
            priority_calculator=(
                priority_calculator
            ),
        )
    )

    # ---------------------------------------------------------
    # Priority queue
    # ---------------------------------------------------------

    priority_queue = (
        PriorityQueue(order_provider=operations_repository.get_queue_order)
    )

    # ---------------------------------------------------------
    # DATEMAST
    # ---------------------------------------------------------
    #
    # DATEMAST comes from the JSON file.
    #
    # DateMast is responsible for loading and refreshing the
    # authoritative report-date information.
    #
    # The scheduler must NOT calculate the last working/report
    # date itself.
    # ---------------------------------------------------------

    datemast = (
        DateMast(
            file_path=DATEMASTER_FILE
        )
    )
    holiday_evaluator.bind_datemast(datemast)

    occurrence_planner = OracleCompatibleOccurrencePlanner(
        frequency_evaluator=frequency_evaluator,
        margin_calculator=margin_calculator,
    )

    # ---------------------------------------------------------
    # Scheduler
    # ---------------------------------------------------------

    scheduler = OccurrenceScheduler(
        schedule_master_repository=schedule_master_repository,
        job_control_repository=job_control_repository,
        occurrence_planner=occurrence_planner,
        eligibility_evaluator=eligibility_evaluator,
        staging_repository=staging_repository,
        ready_repository=ready_repository,
        priority_calculator=priority_calculator,
        priority_queue=priority_queue,
        datemast=datemast,
        schedule_extg_repository=schedule_extg_repository,
        execution_repository=execution_repository,
    )

    upcoming_planner = (
        UpcomingPlanner(
            frequency_evaluator=frequency_evaluator,
            holiday_evaluator=holiday_evaluator,
            margin_calculator=margin_calculator,
            time_window_evaluator=time_window_evaluator,
            occurrence_planner=occurrence_planner,
            datemast=datemast,
        )
    )

    scheduler.lifecycle.event_logger = oracle_logging

    # ---------------------------------------------------------
    # Oracle executor
    # ---------------------------------------------------------
    #
    # OracleExecutor reads the Oracle connection configuration
    # from the project's environment configuration.
    #
    # The executor is responsible only for Oracle execution.
    # ---------------------------------------------------------

    oracle_executor = (
        OracleExecutor()
    )

    # ---------------------------------------------------------
    # Execution manager
    # ---------------------------------------------------------
    #
    # READY
    #   |
    #   v
    # ExecutionManager
    #   |
    #   +----> SQLite execution_jobs
    #   |
    #   +----> Schedule_extg.json
    #   |
    #   +----> OracleExecutor
    #
    # The execution manager is deliberately separate from the
    # scheduler.
    # ---------------------------------------------------------

    execution_manager = (
        ExecutionManager(
            holiday_evaluator=holiday_evaluator,
            ready_repository=(
                ready_repository
            ),
            priority_queue=(
                priority_queue
            ),
            oracle_executor=(
                oracle_executor
            ),
            execution_repository=(
                execution_repository
            ),
            schedule_extg_repository=(
                schedule_extg_repository
            ),
            job_control_repository=(
                job_control_repository
            ),
            schedule_master_repository=(
                schedule_master_repository
            ),
        )
    )

    logger.info(
        "Scheduler application created successfully."
    )

    application = {
        "connection": connection,
        "oracle_logging": oracle_logging,
        "logging_observer": WorkerLoggingObserver(oracle_logging, connection),

        "scheduler": scheduler,

        "execution_manager": execution_manager,

        "priority_queue": priority_queue,

        "ready_repository": ready_repository,

        "staging_repository": staging_repository,

        "job_control_repository": (
            job_control_repository
        ),

        "operations_repository": (
            operations_repository
        ),

        "execution_repository": (
            execution_repository
        ),

        "schedule_extg_repository": (
            schedule_extg_repository
        ),

        "schedule_master_repository": (
            schedule_master_repository
        ),

        "master_configuration_repository": (
            master_configuration_repository
        ),

        "datemast": datemast,

        "holiday_evaluator": holiday_evaluator,

        "frequency_evaluator": frequency_evaluator,

        "time_window_evaluator": time_window_evaluator,

        "upcoming_planner": upcoming_planner,

        "oracle_executor": oracle_executor,

        "calendar_snapshot_repository": (
            OracleCalendarSnapshotRepository(
                DATEMASTER_FILE,
                HOLIDAY_MASTER_FILE,
            )
            if os.getenv("SCHEDULER_CALENDAR_SOURCE", "file").strip().lower() == "oracle"
            else None
        ),

        "next_calendar_refresh": 0.0,
    }
    application["refresh_calendar"] = lambda: _refresh_calendar_snapshots_if_due(application, force=True)
    return application


# =============================================================
# One complete scheduler cycle
# =============================================================

def run_once(application):
    """
    Run one complete scheduler cycle.

    Order:

        1. Evaluate schedule
        2. Move eligible jobs to READY
        3. Rebuild/update priority queue
        4. Execute READY jobs
        5. Persist execution state
    """

    if application.get("execution_enabled") is False:
        return run_monitor_once(application)

    current_datetime = datetime.now()

    _refresh_calendar_snapshots_if_due(application)

    operations_repository = application.get("operations_repository")
    if (
        operations_repository is not None
        and not operations_repository.is_scheduler_enabled()
    ):
        summary = {
            "timestamp": current_datetime.isoformat(),
            "scheduler": {
                "status": "STOPPED",
                "reason": "A scheduler operator stopped future cycles safely.",
            },
            "execution": _build_execution_summary([]),
            "retry": {
                "candidates": 0,
                "processed": 0,
                "reason": "Global scheduler control is stopped.",
            },
        }
        logger.info("Scheduler cycle skipped because global service control is stopped.")
        _capture_logging(application)
        return summary

    scheduler = (
        application["scheduler"]
    )

    execution_manager = (
        application["execution_manager"]
    )

    logger.info(
        "================================================="
    )

    logger.info(
        "Starting scheduler cycle at %s",
        current_datetime,
    )

    # ---------------------------------------------------------
    # Scheduler phase
    # ---------------------------------------------------------

    scheduler_summary = (
        scheduler.run_cycle(
            current_datetime=current_datetime
        )
    )

    logger.info(
        "Scheduler cycle completed: %s",
        scheduler_summary,
    )

    # ---------------------------------------------------------
    # Execution phase
    # ---------------------------------------------------------

    # A scheduler tick makes one priority decision.  Leaving the remaining
    # READY records persistent makes their true queue order visible to
    # operators and lets the next three-minute tick select again.
    # Capture historic retry candidates *before* normal READY work claims an
    # occurrence.  A normal run that changes the latest execution state makes
    # the captured retry stale and it is safely skipped later in this cycle.
    retry_candidates = execution_manager.prepare_retry_candidates(
        current_datetime=current_datetime,
        max_jobs=MAX_RETRY_EXECUTIONS_PER_CYCLE,
    )

    execution_results = execution_manager.execute_available(
        max_jobs=MAX_EXECUTIONS_PER_CYCLE,
        current_datetime=current_datetime,
    )

    retry_results = execution_manager.execute_retry_candidates(
        retry_candidates,
        current_datetime=current_datetime,
        max_jobs=MAX_RETRY_EXECUTIONS_PER_CYCLE,
    )

    execution_results.extend(retry_results)

    execution_summary = (
        _build_execution_summary(
            execution_results
        )
    )

    logger.info(
        "Execution cycle completed: %s",
        execution_summary,
    )

    # ---------------------------------------------------------
    # Complete summary
    # ---------------------------------------------------------

    summary = {
        "timestamp": (
            current_datetime.isoformat()
        ),
        "scheduler": scheduler_summary,
        "execution": execution_summary,
        "retry": {
            "candidates": len(retry_candidates),
            "processed": len(retry_results),
            "states": [result.get("status") for result in retry_results],
        },
    }

    logger.info(
        "Complete scheduler cycle completed: %s",
        summary,
    )

    _capture_logging(application)
    return summary


def configure_monitor(application):
    """Use the actual scheduler pipeline while fencing off every execution path."""
    application["mode"] = "monitor"
    application["execution_enabled"] = False
    application["cycle_interval_seconds"] = max(5, int(os.getenv("SCHEDULER_MONITOR_INTERVAL_SECONDS", "30")))
    # Monitor mode evaluates and logs real state without executing procedures.
    # The legacy JSON status export remains untouched by monitoring.
    application["scheduler"].lifecycle.schedule_extg_repository = None
    application["scheduler"].lifecycle.logging_source = "MONITOR"
    application["monitor_evaluate"] = lambda: run_monitor_once(application)


def run_monitor_once(application):
    """Evaluate real STAGING/READY/queue state; never execute or recover work."""
    current_datetime = datetime.now()
    _refresh_calendar_snapshots_if_due(application)
    operations = application.get("operations_repository")
    if operations is not None and not operations.is_scheduler_enabled():
        scheduler_summary = {"status": "STOPPED", "reason": "Operator service control is stopped."}
    else:
        scheduler_summary = application["scheduler"].run_cycle(current_datetime=current_datetime)
    _capture_logging(application)
    return {
        "timestamp": current_datetime.isoformat(), "mode": "monitor", "execution_enabled": False,
        "scheduler": scheduler_summary, "execution": _build_execution_summary([]),
        "retry": {"candidates": 0, "processed": 0, "reason": "Monitor mode never executes Oracle procedures."},
    }


def _capture_logging(application):
    observer = application.get("logging_observer")
    if observer is not None:
        observer.capture(application)


def _start_logging_delivery(application):
    """Replay on a separate SQLite connection; never commit a business transaction."""
    if application.get("oracle_logging") is None:
        return
    stop = threading.Event()
    application["logging_stop"] = stop

    def deliver():
        repository = None
        try:
            while not stop.is_set():
                try:
                    if repository is None:
                        repository = OracleLoggingRepository()
                    result = repository.flush(limit=100)
                    delay = 0.1 if result["delivered"] and result["pending"] else 2.0
                except Exception:
                    if repository is not None:
                        repository.connection.rollback()
                    logger.exception("Oracle logging delivery paused; durable events remain queued.")
                    delay = 5.0
                stop.wait(delay)
        finally:
            if repository is not None:
                repository.connection.close()

    thread = threading.Thread(target=deliver, name="oracle-log-delivery", daemon=True)
    application["logging_thread"] = thread
    thread.start()


def _refresh_calendar_snapshots_if_due(application, *, force=False, current_datetime=None):
    """Read Oracle DATEMAST once per local calendar day, or on operator request.

    A successful read serves every cycle until the next local day. Failed reads
    retain known dates and retry with a short backoff; missing-date holiday
    inference stays disabled until Oracle recovers. Schedule Master refresh is
    independent, so editing a task never waits for tomorrow's calendar read.
    """
    repository = application.get("calendar_snapshot_repository")
    if repository is None:
        return {"refreshed": False, "available": False,
                "error": "Oracle calendar source is not configured."}
    current_datetime = current_datetime or datetime.now()
    today = current_datetime.date().isoformat()
    now = time.monotonic()
    if not force:
        if application.get("calendar_last_refresh_date") == today and not application.get("calendar_refresh_failed"):
            return _calendar_refresh_result(application, refreshed=False)
        if application.get("calendar_last_attempt_date") == today and now < application.get("next_calendar_refresh", 0.0):
            return _calendar_refresh_result(application, refreshed=False)
    application["calendar_last_attempt_date"] = today
    try:
        warning = None
        if application.get("execution_enabled") is False:
            values = repository.read_calendar()
            application["datemast"].replace_report_dates(
                values["report_dates"],
                coverage_start=values.get("coverage_start"),
                coverage_end=values.get("coverage_end"),
            )
            # The loaded file remains untouched; subsequent lookups use this
            # live Oracle snapshot until the next read-only refresh.
            application["datemast"].file_path = None
            holidays = values.get("holidays")
            warning = values.get("warning")
            if holidays is not None:
                evaluator = application["holiday_evaluator"]
                evaluator.holidays.clear()
                for value in holidays:
                    evaluator.add_holiday(value)
                evaluator.file_path = None
                application["holiday_source"] = "oracle"
            else:
                application["holiday_source"] = "retained_snapshot" if warning else None
            counts = {"datemast": len(values["report_dates"]), "holidays": len(holidays) if holidays is not None else None}
        else:
            counts = repository.refresh_snapshots()
            application["datemast"].available = True
            application["datemast"].reload(force=True)
            application["holiday_evaluator"].reload(force=True)
            application["holiday_source"] = "oracle" if counts.get("holidays") is not None else None
        application["calendar_last_refresh_at"] = current_datetime.isoformat(timespec="seconds")
        application["calendar_last_refresh_date"] = today
        application["calendar_refresh_failed"] = False
        application["calendar_refresh_error"] = warning
        application["calendar_next_refresh_at"] = (current_datetime + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat(timespec="seconds")
        logger.info("Refreshed Oracle calendar snapshots: %s", counts)
    except OracleMasterSyncError as exc:
        application["datemast"].available = False
        application["holiday_source"] = "retained_snapshot"
        application["calendar_refresh_error"] = "Oracle calendar refresh failed; known DATEMAST dates are retained, but missing dates use provisional bank rules."
        application["calendar_refresh_failed"] = True
        application["calendar_next_refresh_at"] = (current_datetime + timedelta(seconds=CALENDAR_RETRY_SECONDS)).isoformat(timespec="seconds")
        logger.warning("Oracle calendar refresh failed; using last known snapshots: %s", exc)
    finally:
        application["next_calendar_refresh"] = now + CALENDAR_RETRY_SECONDS
    return _calendar_refresh_result(application, refreshed=not application.get("calendar_refresh_failed"))


def _calendar_refresh_result(application, *, refreshed):
    return {
        "refreshed": refreshed,
        "available": bool(application["datemast"].available),
        "last_refresh_at": application.get("calendar_last_refresh_at"),
        "next_refresh_at": application.get("calendar_next_refresh_at"),
        "refresh_policy": "daily",
        "coverage_start": _calendar_coverage_value(application, "coverage_start"),
        "coverage_end": _calendar_coverage_value(application, "coverage_end"),
        "error": application.get("calendar_refresh_error"),
    }


def _calendar_coverage_value(application, attribute):
    value = getattr(application["datemast"], attribute, None)
    return value.isoformat() if value is not None else None


# =============================================================
# Execution summary
# =============================================================

def _build_execution_summary(
    execution_results
):
    """
    Build a monitoring-friendly execution summary.
    """

    states = {}
    total_processed = 0
    total_executed = 0
    total_skipped = 0
    duplicate_skipped = 0
    successful = 0
    failed = 0

    for result in execution_results:

        total_processed += 1

        if isinstance(result, dict):
            status = result.get(
                "status",
                "UNKNOWN",
            )
            executed = bool(
                result.get(
                    "executed",
                    False,
                )
            )
            duplicate = bool(
                result.get(
                    "duplicate",
                    False,
                )
            )
        else:
            status = getattr(
                result,
                "status",
                "UNKNOWN",
            )
            executed = bool(
                getattr(
                    result,
                    "executed",
                    False,
                )
            )
            duplicate = bool(
                getattr(
                    result,
                    "duplicate",
                    False,
                )
            )

        status = str(status).upper()

        states[status] = (
            states.get(
                status,
                0,
            )
            + 1
        )

        if executed:
            total_executed += 1

            if status == "SUCCESS":
                successful += 1
            elif status == "FAILED":
                failed += 1
        else:
            total_skipped += 1

            if duplicate:
                duplicate_skipped += 1

    return {
        "total_processed": total_processed,
        "total_executed": total_executed,
        "successful": successful,
        "failed": failed,
        "total_skipped": total_skipped,
        "duplicate_skipped": duplicate_skipped,
        "states": states,
    }


# =============================================================
# Continuous scheduler
# =============================================================

def run_forever(application, control_plane=None):
    """
    Run the scheduler continuously.

    The configured interval is currently 180 seconds
    (3 minutes).

    The sleep time is adjusted for the time consumed by the
    scheduler/execution cycle itself.
    """

    interval_seconds = application.get("cycle_interval_seconds", SCHEDULER_INTERVAL_SECONDS)
    logger.info(
        "Scheduler started. "
        "Interval=%s seconds.",
        interval_seconds,
    )

    while True:

        cycle_started = (
            time.monotonic()
        )

        try:

            summary = None
            if control_plane is None:
                summary = run_once(application)
            else:
                # Serialize every SQLite and heap change. Release only while
                # Oracle executes externally, after RUNNING is committed, so
                # operators can see live status and control remaining work.
                with control_plane.lock:
                    manager = application["execution_manager"]
                    manager.oracle_io_lock = control_plane.lock
                    try:
                        summary = run_once(application)
                    finally:
                        manager.oracle_io_lock = None

        except Exception as error:

            # A failure in one scheduler cycle must not terminate
            # the scheduler process.
            logger.exception(
                "Complete scheduler cycle failed. "
                "Continuing to next cycle."
            )
            if control_plane is not None:
                summary = control_plane.record_cycle_failure(error)

        elapsed = (
            time.monotonic()
            - cycle_started
        )

        sleep_seconds = max(
            0,
            interval_seconds
            - elapsed,
        )

        if control_plane is not None and summary is not None:
            control_plane.record_cycle(
                summary,
                next_cycle_at=(datetime.now() + timedelta(seconds=sleep_seconds)),
            )

        logger.debug(
            "Next scheduler cycle in %.2f seconds.",
            sleep_seconds,
        )

        time.sleep(
            sleep_seconds
        )


# =============================================================
# Application shutdown
# =============================================================

def close_application(
    application
):
    """
    Close external resources owned by the application.
    """

    if application is None:
        return

    if application.get("logging_stop") is not None:
        application["logging_stop"].set()
        application["logging_thread"].join(timeout=5)

    control_api_server = application.get("control_api_server")
    if control_api_server is not None:
        try:
            control_api_server.shutdown()
            control_api_server.server_close()
            logger.info("Scheduler control API stopped.")
        except Exception:
            logger.exception("Failed to stop scheduler control API.")

    # ---------------------------------------------------------
    # Oracle
    # ---------------------------------------------------------

    oracle_executor = (
        application.get(
            "oracle_executor"
        )
    )

    if oracle_executor is not None:

        try:

            oracle_executor.close()

            logger.info(
                "Oracle executor closed."
            )

        except Exception:

            logger.exception(
                "Failed to close Oracle executor."
            )

    # ---------------------------------------------------------
    # SQLite
    # ---------------------------------------------------------

    connection = (
        application.get(
            "connection"
        )
    )

    if connection is not None:

        try:

            connection.close()

            logger.info(
                "SQLite connection closed."
            )

        except Exception:

            logger.exception(
                "Failed to close SQLite connection."
            )


# =============================================================
# Entry point
# =============================================================

def _parse_arguments(argv=None):
    """Parse the small, operator-safe worker command surface.

    ``--once`` exists specifically for Windows Task Scheduler. It starts a
    complete application, performs one cycle, persists all resulting state,
    and exits. The default remains the long-running scheduler service for
    environments where the web control API must always be available.
    """
    parser = argparse.ArgumentParser(description="ITRP Oracle scheduler worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one scheduler/execution cycle and exit (Task Scheduler mode)",
    )
    parser.add_argument(
        "--monitor", action="store_true",
        help="evaluate real schedules and serve the API without executing Oracle procedures",
    )
    parser.add_argument(
        "--calendar-source", choices=("file", "oracle"),
        help="choose the calendar source for this process (monitor reads Oracle into memory only)",
    )
    parser.add_argument(
        "--no-control-api",
        action="store_true",
        help="do not start the local control API in continuous-service mode",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the one-cycle result as JSON (valid with --once)",
    )
    args = parser.parse_args(argv)
    if args.monitor and (args.once or args.no_control_api):
        parser.error("--monitor cannot be combined with --once or --no-control-api")
    return args


def _recover_orphaned_executions(application):
    """Recover a RUNNING record left by a terminated worker process."""
    recovered = application["execution_manager"].recover_running_executions()
    if recovered:
        logger.warning("Recovered %s orphaned RUNNING execution(s).", recovered)
    return recovered


def main(argv=None):
    """Application entry point for either a service or a single batch cycle."""
    args = _parse_arguments(argv)
    application = None
    instance_lock = SchedulerInstanceLock(BASE_DIR)

    # The lock covers both the continuous service and the five-minute batch
    # worker. If Task Scheduler fires while a prior Oracle call is still in
    # progress, the new trigger is deliberately treated as a successful skip
    # instead of becoming a second scheduler process.
    if not instance_lock.acquire():
        message = "Scheduler worker already running; this trigger was skipped."
        logger.warning(message)
        print(message)
        return 0

    try:
        if args.calendar_source:
            os.environ["SCHEDULER_CALENDAR_SOURCE"] = args.calendar_source
        application = create_application()
        if args.monitor:
            configure_monitor(application)
        else:
            application["mode"] = "worker"
            application["execution_enabled"] = True

        if application.get("logging_observer") is not None:
            application["logging_observer"].backfill()

        # If a prior worker process died mid-execution, mark the old record as
        # recoverable before accepting new work. The single-instance lock makes
        # this safe in both service and Task Scheduler modes.
        if not args.monitor:
            _recover_orphaned_executions(application)

        if args.once:
            summary = run_once(application)
            summary["oracle_logging"] = application["oracle_logging"].flush(limit=100)
            if args.json:
                print(json.dumps(summary, default=str, indent=2))
            else:
                execution = summary.get("execution", {})
                print(
                    "Scheduler cycle complete: "
                    f"processed={execution.get('total_processed', 0)}, "
                    f"executed={execution.get('total_executed', 0)}, "
                    f"successful={execution.get('successful', 0)}, "
                    f"failed={execution.get('failed', 0)}"
                )
            return 0

        _start_logging_delivery(application)
        control_plane = None
        if not args.no_control_api:
            # The UI and operator CLI only talk to this scheduler-owned API.
            # They never open the scheduler database or update the priority
            # queue directly.
            control_plane = SchedulerControlApi(
                application,
                lock=threading.RLock(),
            )
            application["control_api_server"] = start_control_api(
                application,
                control_plane,
            )

        run_forever(application, control_plane=control_plane)
        return 0

    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user.")
        return 130
    except Exception:
        logger.exception("Scheduler failed to start.")
        return 1
    finally:
        close_application(application)
        instance_lock.release()


# =============================================================
# Python entry point
# =============================================================

if __name__ == "__main__":
    raise SystemExit(main())
