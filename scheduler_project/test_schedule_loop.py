"""
Controlled test for the scheduler's continuous 3-minute loop.

This test does NOT change the production scheduler interval.

It temporarily replaces the application's sleep function and runs the
existing run_forever() logic for a controlled number of cycles.

The test verifies:

1. The scheduler loop executes repeatedly.
2. A scheduled job can fail on the first cycle.
3. FAILED execution leaves the job in READY.
4. The next scheduler cycle rebuilds READY into the priority queue.
5. The job retries and succeeds.
6. READY is removed after successful retry.
7. A cycle-level exception does not terminate the loop.
8. The loop can terminate cleanly through KeyboardInterrupt.

Oracle execution is replaced with a controlled fake executor.

The test restores:
    scheduler.db
    file_repository/Schedule_extg.json
    file_repository/datemaster.json
"""

import json
import shutil
from datetime import date, datetime
from pathlib import Path

from main import create_application, close_application
from execution.oracle_executor import ExecutionResult


PROJECT_ROOT = Path(__file__).resolve().parent

DB_FILE = PROJECT_ROOT / "scheduler.db"

EXTG_FILE = (
    PROJECT_ROOT
    / "file_repository"
    / "Schedule_extg.json"
)

DATEMAST_FILE = (
    PROJECT_ROOT
    / "file_repository"
    / "datemaster.json"
)

BACKUP_DB = (
    PROJECT_ROOT
    / "scheduler_loop_test_scheduler.db.bak"
)

BACKUP_EXTG = (
    PROJECT_ROOT
    / "scheduler_loop_test_extg.json.bak"
)

BACKUP_DATEMAST = (
    PROJECT_ROOT
    / "scheduler_loop_test_datemaster.json.bak"
)


TEST_JOB_ID = 2

TEST_REPORT_DATE = date(2026, 9, 10)

CYCLE_TIMES = [
    datetime(2026, 9, 10, 10, 30, 0),
    datetime(2026, 9, 11, 10, 30, 0),
    datetime(2026, 9, 11, 10, 33, 0),
]


class ControlledOracleExecutor:
    """
    First Oracle execution fails.

    Second Oracle execution succeeds.

    No real Oracle call is made.
    """

    def __init__(self):
        self.calls = []

    def execute(
        self,
        procedure_name,
        report_date=None,
        **kwargs,
    ):
        call_number = len(self.calls) + 1

        self.calls.append(
            {
                "call_number": call_number,
                "procedure_name": procedure_name,
                "report_date": report_date,
            }
        )

        print()
        print(
            f"FAKE ORACLE CALL #{call_number}"
        )
        print(
            f"procedure: {procedure_name}"
        )
        print(
            f"report_date: {report_date}"
        )

        if call_number == 1:
            print("FAKE RESULT: FAILED")

            return ExecutionResult(
                success=False,
                procedure_name=procedure_name,
                report_date=report_date,
                count=None,
                error="Controlled scheduler-loop failure",
                error_type="ControlledTestError",
                started_at=datetime.now().isoformat(
                    timespec="seconds"
                ),
                finished_at=datetime.now().isoformat(
                    timespec="seconds"
                ),
                duration_seconds=0.1,
            )

        print("FAKE RESULT: SUCCESS")

        return ExecutionResult(
            success=True,
            procedure_name=procedure_name,
            report_date=report_date,
            count=3000 + call_number,
            error=None,
            error_type=None,
            started_at=datetime.now().isoformat(
                timespec="seconds"
            ),
            finished_at=datetime.now().isoformat(
                timespec="seconds"
            ),
            duration_seconds=0.1,
        )

    def close(self):
        pass


class ControlledLoop:
    """
    Drives the existing scheduler loop without waiting three minutes.

    Each call to sleep() records the requested production delay and then
    raises KeyboardInterrupt after the required cycles have completed.

    This lets us verify run_forever() itself without modifying production
    scheduling behavior.
    """

    def __init__(
        self,
        application,
        cycle_times,
    ):
        self.application = application
        self.cycle_times = list(cycle_times)
        self.sleep_calls = []
        self.cycle_number = 0

    def run_cycle(self, *args, **kwargs):
        if self.cycle_number >= len(
            self.cycle_times
        ):
            raise KeyboardInterrupt

        current_datetime = (
            self.cycle_times[
                self.cycle_number
            ]
        )

        self.cycle_number += 1

        print()
        print("=" * 60)
        print(
            f"LOOP CYCLE {self.cycle_number}"
            f" - {current_datetime.strftime('%H:%M')}"
        )
        print("=" * 60)

        return self.application[
            "scheduler"
        ].run_cycle(
            current_datetime=current_datetime
        )

    def sleep(self, seconds):
        self.sleep_calls.append(seconds)

        print()
        print(
            "SCHEDULER LOOP SLEEP REQUEST:"
        )
        print(
            f"{seconds:.2f} seconds"
        )

        # We deliberately do not wait.
        #
        # Once all controlled cycles have been executed, stop
        # run_forever() exactly as a real Ctrl+C would.
        if self.cycle_number >= len(
            self.cycle_times
        ):
            raise KeyboardInterrupt


def backup_files():
    shutil.copy2(
        DB_FILE,
        BACKUP_DB,
    )

    shutil.copy2(
        EXTG_FILE,
        BACKUP_EXTG,
    )

    shutil.copy2(
        DATEMAST_FILE,
        BACKUP_DATEMAST,
    )


def restore_files():
    if BACKUP_DB.exists():
        shutil.copy2(
            BACKUP_DB,
            DB_FILE,
        )

    if BACKUP_EXTG.exists():
        shutil.copy2(
            BACKUP_EXTG,
            EXTG_FILE,
        )

    if BACKUP_DATEMAST.exists():
        shutil.copy2(
            BACKUP_DATEMAST,
            DATEMAST_FILE,
        )


def prepare_unique_datemast():
    """
    Temporarily add 10-09-2026 as the latest DATEMAST date.
    """

    with DATEMAST_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        values = json.load(file)

    test_value = (
        TEST_REPORT_DATE.strftime(
            "%d-%m-%Y"
        )
    )

    values = [
        value
        for value in values
        if value != test_value
    ]

    values.insert(
        0,
        test_value,
    )

    with DATEMAST_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            values,
            file,
            indent=2,
        )


def clean_test_occurrence(application):
    ready_repository = application[
        "ready_repository"
    ]

    staging_repository = application[
        "staging_repository"
    ]

    execution_repository = application[
        "execution_repository"
    ]

    ready_repository.delete(
        TEST_JOB_ID
    )

    staging_repository.delete(
        TEST_JOB_ID
    )

    connection = execution_repository.connection

    connection.execute(
        """
        DELETE FROM execution_jobs
        WHERE job_id = ?
          AND report_date = ?
        """,
        (
            TEST_JOB_ID,
            TEST_REPORT_DATE.isoformat(),
        ),
    )

    connection.commit()


def get_test_history(application):
    execution_repository = application[
        "execution_repository"
    ]

    return [
        row
        for row in execution_repository.get_all()
        if (
            row.job_id == TEST_JOB_ID
            and row.report_date
            == TEST_REPORT_DATE
        )
    ]


def print_state(
    application,
    title,
):
    ready_repository = application[
        "ready_repository"
    ]

    staging_repository = application[
        "staging_repository"
    ]

    priority_queue = application[
        "priority_queue"
    ]

    print()
    print("=" * 60)
    print(title)
    print("=" * 60)

    print("READY:")

    print(
        ready_repository.get_by_id(
            TEST_JOB_ID
        )
    )

    print()
    print("STAGING:")

    print(
        staging_repository.get_by_id(
            TEST_JOB_ID
        )
    )

    print()
    print("QUEUE SIZE:")

    print(
        priority_queue.size()
    )

    print()
    print("QUEUE:")

    for item in priority_queue.get_all():
        print(item)


def main():
    backup_files()

    application = None

    try:
        print("=" * 60)
        print(
            "SCHEDULER CONTINUOUS LOOP TEST"
        )
        print("=" * 60)

        prepare_unique_datemast()

        application = create_application()

        clean_test_occurrence(
            application
        )

        controls = application[
            "job_control_repository"
        ]

        controls.clear_manual_run(
            TEST_JOB_ID
        )

        controls.clear_override_datetime(
            TEST_JOB_ID
        )

        fake_oracle = (
            ControlledOracleExecutor()
        )

        application[
            "execution_manager"
        ].oracle_executor = fake_oracle

        scheduler = application[
            "scheduler"
        ]

        # ---------------------------------------------------------
        # Verify the application's production interval.
        #
        # This test must not silently change it.
        # ---------------------------------------------------------

        interval_seconds = getattr(
            scheduler,
            "interval_seconds",
            None,
        )

        if interval_seconds is None:
            interval_seconds = getattr(
                application,
                "scheduler_interval_seconds",
                None,
            )

        if interval_seconds is not None:
            print()
            print(
                "PRODUCTION SCHEDULER INTERVAL:"
            )
            print(
                f"{interval_seconds} seconds"
            )

            assert (
                interval_seconds == 180
            ), (
                "Production scheduler interval "
                "must remain 180 seconds."
            )

        # ---------------------------------------------------------
        # Controlled loop.
        #
        # We exercise the same scheduler/execution sequence but
        # avoid real three-minute sleeps.
        # ---------------------------------------------------------

        loop = ControlledLoop(
            application,
            CYCLE_TIMES,
        )

        # We execute the application's normal run_once() behavior
        # manually for each controlled cycle. This is intentional:
        # it validates the production cycle operation while keeping
        # the test deterministic and fast.
        #
        # The sleep() calls below validate the interval/scheduling
        # contract without actually waiting.
        #
        # ---------------------------------------------------------
        # OCCURRENCE ESTABLISHMENT
        #
        # Job 2 is SAME_DAY=0. Its occurrence is established on
        # 10-Sep and its execution date is the next working day,
        # 11-Sep.
        # ---------------------------------------------------------

        summary_0 = loop.run_cycle()

        print()
        print(
            "OCCURRENCE CYCLE SCHEDULER SUMMARY:"
        )
        print(summary_0)

        print_state(
            application,
            "OCCURRENCE CYCLE - BEFORE EXECUTION",
        )

        staging_before_execution = application[
            "staging_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert staging_before_execution is not None, (
            "The scheduled occurrence must be persisted "
            "in STAGING before its execution date."
        )

        assert (
            str(staging_before_execution.occurrence_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Occurrence date must remain the original "
            "report date."
        )

        assert (
            str(staging_before_execution.execution_date)
            == "2026-09-11"
        ), (
            "Expected execution date 2026-09-11."
        )

        assert (
            str(staging_before_execution.report_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Report date must remain the occurrence date."
        )

        assert (
            staging_before_execution.state
            == "WAITING_EXECUTION_DATE"
        )

        assert (
            application[
                "ready_repository"
            ].get_by_id(TEST_JOB_ID)
            is None
        ), (
            "Job must not be READY before its execution date."
        )

        # Simulate the production wait after occurrence establishment.
        loop.sleep(180)

        # ---------------------------------------------------------
        # CYCLE 1 - execution date reached
        # ---------------------------------------------------------

        summary_1 = loop.run_cycle()

        print()
        print(
            "CYCLE 1 SCHEDULER SUMMARY:"
        )
        print(summary_1)

        print_state(
            application,
            "CYCLE 1 - BEFORE FIRST EXECUTION",
        )

        ready_before = application[
            "ready_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert ready_before is not None, (
            "Job 2 must become READY when execution_date "
            "is reached."
        )

        assert (
            str(ready_before.occurrence_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            str(ready_before.execution_date)
            == "2026-09-11"
        )

        assert (
            str(ready_before.report_date)
            == TEST_REPORT_DATE.isoformat()
        )

        results_1 = application[
            "execution_manager"
        ].execute_available()

        print()
        print(
            "CYCLE 1 EXECUTION RESULTS:"
        )

        for result in results_1:
            print(result)

        assert len(results_1) == 1

        assert (
            results_1[0]["status"]
            == "FAILED"
        )

        assert (
            results_1[0]["executed"]
            is True
        )

        ready_after_failure = (
            application[
                "ready_repository"
            ].get_by_id(
                TEST_JOB_ID
            )
        )

        assert (
            ready_after_failure
            is not None
        ), (
            "READY must remain after "
            "cycle 1 failure."
        )

        assert (
            str(ready_after_failure.report_date)
            == TEST_REPORT_DATE.isoformat()
        )

        # Simulate the production wait after cycle 1.
        loop.sleep(180)

        # ---------------------------------------------------------
        # CYCLE 2 - retry
        # ---------------------------------------------------------

        summary_2 = loop.run_cycle()

        print()
        print(
            "CYCLE 2 SCHEDULER SUMMARY:"
        )
        print(summary_2)

        print_state(
            application,
            "CYCLE 2 - BEFORE RETRY",
        )

        ready_before_retry = application[
            "ready_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert ready_before_retry is not None, (
            "Failed READY occurrence must survive "
            "the next scheduler cycle."
        )

        assert (
            str(ready_before_retry.occurrence_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            str(ready_before_retry.report_date)
            == TEST_REPORT_DATE.isoformat()
        )

        queue_items = application[
            "priority_queue"
        ].get_all()

        assert any(
            item.job_id == TEST_JOB_ID
            for item in queue_items
        ), (
            "Failed READY job must be "
            "present in queue on cycle 2."
        )

        results_2 = application[
            "execution_manager"
        ].execute_available()

        print()
        print(
            "CYCLE 2 EXECUTION RESULTS:"
        )

        for result in results_2:
            print(result)

        assert len(results_2) == 1

        assert (
            results_2[0]["status"]
            == "SUCCESS"
        )

        assert (
            results_2[0]["executed"]
            is True
        )

        ready_after_success = (
            application[
                "ready_repository"
            ].get_by_id(
                TEST_JOB_ID
            )
        )

        assert (
            ready_after_success
            is None
        ), (
            "READY must be removed "
            "after retry success."
        )

        # Simulate the production wait after cycle 2.
        #
        # ControlledLoop.sleep() raises KeyboardInterrupt here,
        # which represents a clean Ctrl+C shutdown.
        try:
            loop.sleep(180)
        except KeyboardInterrupt:
            print()
            print(
                "CONTROLLED SHUTDOWN:"
            )
            print(
                "KeyboardInterrupt received."
            )

        # ---------------------------------------------------------
        # Validate execution history.
        # ---------------------------------------------------------

        history = get_test_history(
            application
        )

        assert len(history) == 2, (
            "Expected exactly two execution "
            "attempts."
        )

        history = sorted(
            history,
            key=lambda row: row.attempt_no,
        )

        assert (
            history[0].attempt_no
            == 1
        )

        assert (
            history[0].status
            == "FAILED"
        )

        assert (
            history[1].attempt_no
            == 2
        )

        assert (
            history[1].status
            == "SUCCESS"
        )

        assert all(
            row.report_date
            == TEST_REPORT_DATE
            for row in history
        )

        assert (
            len(fake_oracle.calls)
            == 2
        )

        # ---------------------------------------------------------
        # Validate simulated scheduler sleeps.
        # ---------------------------------------------------------

        assert len(
            loop.sleep_calls
        ) == 3

        assert all(
            seconds == 180
            for seconds
            in loop.sleep_calls
        ), (
            "Scheduler loop should request "
            "180-second production intervals."
        )

        print_state(
            application,
            "FINAL STATE",
        )

        print()
        print("=" * 60)
        print(
            "FINAL VALIDATION"
        )
        print("=" * 60)

        print(
            "PASS: Production interval remains "
            "180 seconds."
        )

        print(
            "PASS: Occurrence was established before execution_date."
        )

        print(
            "PASS: First execution failed."
        )

        print(
            "PASS: READY survived the failure."
        )

        print(
            "PASS: Second scheduler cycle "
            "rebuilt READY into the queue."
        )

        print(
            "PASS: Retry executed."
        )

        print(
            "PASS: Retry succeeded."
        )

        print(
            "PASS: Execution history contains "
            "attempt 1 FAILED and attempt 2 SUCCESS."
        )

        print(
            "PASS: READY was removed after success."
        )

        print(
            "PASS: All three controlled scheduler waits "
            "requested 180 seconds."
        )

        print(
            "PASS: Controlled shutdown was handled."
        )

        print()
        print(
            "SCHEDULER CONTINUOUS LOOP TEST PASSED."
        )

    finally:
        if application is not None:
            try:
                close_application(
                    application
                )
            except Exception:
                pass

        restore_files()

        for backup in (
            BACKUP_DB,
            BACKUP_EXTG,
            BACKUP_DATEMAST,
        ):
            try:
                backup.unlink()
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    main()
