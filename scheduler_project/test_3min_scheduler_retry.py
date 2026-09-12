"""
Realistic 3-minute scheduler-cycle retry test.

This test exercises:

    REAL Scheduler
        -> REAL EligibilityEvaluator
        -> REAL STAGING / READY persistence
        -> REAL PriorityCalculator
        -> REAL PriorityQueue
        -> REAL ExecutionManager

Only Oracle execution is replaced with a controlled fake executor.

The test creates a temporary DATEMAST containing a unique report date,
so the scheduled occurrence cannot collide with the existing execution
history in scheduler.db.

Scenario:

    Occurrence cycle - 10-Sep 10:30
        Job 2 creates/persists its 10-Sep occurrence.
        Because SAME_DAY=0, execution_date is 11-Sep.
        The occurrence must remain in STAGING until then.

    Cycle 1 - 11-Sep 10:30
        The persisted 10-Sep occurrence reaches its execution date.
        Job 2 becomes READY.
        Fake Oracle fails.
        READY must remain.

    Cycle 2 - 11-Sep 10:33
        Scheduler recalculates the same persisted occurrence.
        READY must remain and be rebuilt into the heap.
        Fake Oracle succeeds on retry.
        READY must then be removed.

All modified files are restored in finally:
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
    / "scheduler_3min_retry_scheduler.db.bak"
)
BACKUP_EXTG = (
    PROJECT_ROOT
    / "scheduler_3min_retry_extg.json.bak"
)
BACKUP_DATEMAST = (
    PROJECT_ROOT
    / "scheduler_3min_retry_datemaster.json.bak"
)


TEST_JOB_ID = 2

# This date is deliberately unique to this test.
# It is added temporarily to DATEMAST.
TEST_REPORT_DATE = date(2026, 9, 10)

OCCURRENCE_CYCLE = datetime(2026, 9, 10, 10, 30, 0)
CYCLE_1 = datetime(2026, 9, 11, 10, 30, 0)
CYCLE_2 = datetime(2026, 9, 11, 10, 33, 0)


class ControlledOracleExecutor:
    """
    First call fails.

    Second call succeeds.

    No real Oracle connection is made.
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
        print(f"FAKE ORACLE CALL #{call_number}")
        print(f"procedure: {procedure_name}")
        print(f"report_date: {report_date}")

        if call_number == 1:
            print("FAKE RESULT: FAILED")

            return ExecutionResult(
                success=False,
                procedure_name=procedure_name,
                report_date=report_date,
                count=None,
                error="Controlled 3-minute test failure",
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
            count=2000 + call_number,
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


def backup_files():
    shutil.copy2(DB_FILE, BACKUP_DB)
    shutil.copy2(EXTG_FILE, BACKUP_EXTG)
    shutil.copy2(DATEMAST_FILE, BACKUP_DATEMAST)


def restore_files():
    if BACKUP_DB.exists():
        shutil.copy2(BACKUP_DB, DB_FILE)

    if BACKUP_EXTG.exists():
        shutil.copy2(BACKUP_EXTG, EXTG_FILE)

    if BACKUP_DATEMAST.exists():
        shutil.copy2(
            BACKUP_DATEMAST,
            DATEMAST_FILE,
        )


def prepare_unique_datemast():
    """
    Add TEST_REPORT_DATE to DATEMAST temporarily.

    DATEMAST is stored as strings in DD-MM-YYYY format.
    """
    with DATEMAST_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        values = json.load(file)

    test_value = (
        TEST_REPORT_DATE.strftime("%d-%m-%Y")
    )

    if test_value not in values:
        values.insert(0, test_value)

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
    """
    Remove only test state from the SQLite database.

    The test occurrence is unique, so this does not remove existing
    production/control history for other report dates.
    """
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


def print_state(application, title):
    ready_repository = application[
        "ready_repository"
    ]

    staging_repository = application[
        "staging_repository"
    ]

    execution_repository = application[
        "execution_repository"
    ]

    priority_queue = application[
        "priority_queue"
    ]

    print()
    print("=" * 60)
    print(title)
    print("=" * 60)

    ready = ready_repository.get_by_id(
        TEST_JOB_ID
    )

    staging = staging_repository.get_by_id(
        TEST_JOB_ID
    )

    print("READY:")
    print(ready)

    print()
    print("STAGING:")
    print(staging)

    print()
    print("QUEUE SIZE:")
    print(priority_queue.size())

    print()
    print("QUEUE:")

    for item in priority_queue.get_all():
        print(item)

    print()
    print("EXECUTION HISTORY:")

    for row in execution_repository.get_all():
        if (
            row.job_id == TEST_JOB_ID
            and row.report_date
            == TEST_REPORT_DATE
        ):
            print(row)


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


def main():
    backup_files()

    application = None

    try:
        print("=" * 60)
        print("3-MINUTE SCHEDULED RETRY TEST")
        print("=" * 60)

        # ---------------------------------------------------------
        # Add a unique report date to DATEMAST.
        #
        # The original DATEMAST is restored in finally.
        # ---------------------------------------------------------

        prepare_unique_datemast()

        application = create_application()

        clean_test_occurrence(
            application
        )

        controls = application[
            "job_control_repository"
        ]

        # Ensure this is a normal scheduled occurrence.
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

        # =========================================================
        # OCCURRENCE ESTABLISHMENT
        #
        # On 10-Sep the non-SAME_DAY occurrence is created and its
        # execution context is fixed. Job 2 executes on the next
        # working day, so it must still be WAITING_EXECUTION_DATE.
        # =========================================================

        print()
        print("=" * 60)
        print("OCCURRENCE ESTABLISHMENT - 10-SEP 10:30")
        print("=" * 60)

        occurrence_summary = (
            application[
                "scheduler"
            ].run_cycle(
                current_datetime=OCCURRENCE_CYCLE
            )
        )

        print()
        print("SCHEDULER SUMMARY:")
        print(occurrence_summary)

        print_state(
            application,
            "STATE AFTER OCCURRENCE ESTABLISHMENT",
        )

        staging_before_execution = application[
            "staging_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert staging_before_execution is not None, (
            "Job 2 occurrence should be persisted in STAGING."
        )

        assert (
            str(staging_before_execution.occurrence_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Unexpected occurrence date after establishment: "
            f"{staging_before_execution.occurrence_date}"
        )

        assert (
            str(staging_before_execution.execution_date)
            == "2026-09-11"
        ), (
            "Expected next working day 2026-09-11 as execution date."
        )

        assert (
            str(staging_before_execution.t_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "T date must remain fixed to the occurrence date."
        )

        assert (
            str(staging_before_execution.target_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Target date must remain fixed for this test job."
        )

        assert (
            staging_before_execution.state
            == "WAITING_EXECUTION_DATE"
        ), (
            "Job 2 should wait for its execution date."
        )

        assert (
            application[
                "ready_repository"
            ].get_by_id(TEST_JOB_ID)
            is None
        ), (
            "Job 2 must not be READY before execution_date."
        )

        # =========================================================
        # CYCLE 1 - execution date reached
        # =========================================================

        print()
        print("=" * 60)
        print("CYCLE 1 - 11-SEP 10:30")
        print("=" * 60)

        scheduler_summary_1 = (
            application[
                "scheduler"
            ].run_cycle(
                current_datetime=CYCLE_1
            )
        )

        print()
        print("SCHEDULER SUMMARY:")
        print(scheduler_summary_1)

        print_state(
            application,
            "STATE BEFORE FIRST EXECUTION",
        )

        ready_before = application[
            "ready_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert ready_before is not None, (
            "Job 2 should be READY on its execution date."
        )

        assert (
            str(ready_before.report_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Unexpected report date before first execution: "
            f"{ready_before.report_date}"
        )

        assert (
            str(ready_before.occurrence_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "The pending occurrence must remain the original report date."
        )

        assert (
            str(ready_before.execution_date)
            == "2026-09-11"
        ), (
            "The execution date must remain fixed at 2026-09-11."
        )

        first_results = (
            application[
                "execution_manager"
            ].execute_available()
        )

        print()
        print("FIRST EXECUTION RESULTS:")

        for result in first_results:
            print(result)

        assert len(first_results) == 1, (
            "Expected exactly one execution result."
        )

        first_result = first_results[0]

        assert (
            first_result["status"]
            == "FAILED"
        ), (
            "Expected FAILED, got "
            f"{first_result['status']}"
        )

        assert first_result["executed"] is True
        assert first_result["duplicate"] is False

        ready_after_failure = (
            application[
                "ready_repository"
            ].get_by_id(
                TEST_JOB_ID
            )
        )

        assert ready_after_failure is not None, (
            "READY must remain after a scheduled "
            "execution failure."
        )

        assert (
            str(
                ready_after_failure.report_date
            )
            == TEST_REPORT_DATE.isoformat()
        ), (
            "The failed READY occurrence must retain "
            "its report date."
        )

        history_after_failure = (
            get_test_history(
                application
            )
        )

        assert len(
            history_after_failure
        ) == 1

        assert (
            history_after_failure[0].status
            == "FAILED"
        )

        assert (
            history_after_failure[0].attempt_no
            == 1
        )

        print_state(
            application,
            "STATE AFTER FIRST FAILURE",
        )

        # =========================================================
        # CYCLE 2
        #
        # Exactly three minutes later, on the same execution date.
        # =========================================================

        print()
        print("=" * 60)
        print("CYCLE 2 - 11-SEP 10:33")
        print("=" * 60)

        scheduler_summary_2 = (
            application[
                "scheduler"
            ].run_cycle(
                current_datetime=CYCLE_2
            )
        )

        print()
        print("SCHEDULER SUMMARY:")
        print(scheduler_summary_2)

        print_state(
            application,
            "STATE BEFORE RETRY",
        )

        ready_before_retry = (
            application[
                "ready_repository"
            ].get_by_id(
                TEST_JOB_ID
            )
        )

        assert ready_before_retry is not None, (
            "READY must survive the second scheduler "
            "cycle until retry succeeds."
        )

        assert (
            str(
                ready_before_retry.report_date
            )
            == TEST_REPORT_DATE.isoformat()
        ), (
            "The scheduler changed the pending report "
            "date between retry cycles."
        )

        queue_items = (
            application[
                "priority_queue"
            ].get_all()
        )

        assert any(
            item.job_id == TEST_JOB_ID
            for item in queue_items
        ), (
            "The failed READY occurrence must be rebuilt "
            "into the priority queue during cycle 2."
        )

        second_results = (
            application[
                "execution_manager"
            ].execute_available()
        )

        print()
        print("RETRY EXECUTION RESULTS:")

        for result in second_results:
            print(result)

        assert len(second_results) == 1, (
            "Expected exactly one retry execution result."
        )

        second_result = second_results[0]

        assert (
            second_result["status"]
            == "SUCCESS"
        ), (
            "Expected SUCCESS, got "
            f"{second_result['status']}"
        )

        assert second_result["executed"] is True
        assert second_result["duplicate"] is False

        # =========================================================
        # FINAL STATE
        # =========================================================

        ready_after_success = (
            application[
                "ready_repository"
            ].get_by_id(
                TEST_JOB_ID
            )
        )

        assert ready_after_success is None, (
            "READY must be removed after successful retry."
        )

        final_history = (
            get_test_history(
                application
            )
        )

        assert len(final_history) == 2, (
            "Expected exactly two execution attempts."
        )

        attempts = sorted(
            final_history,
            key=lambda row: row.attempt_no,
        )

        assert (
            attempts[0].attempt_no
            == 1
        )

        assert (
            attempts[0].status
            == "FAILED"
        )

        assert (
            attempts[1].attempt_no
            == 2
        )

        assert (
            attempts[1].status
            == "SUCCESS"
        )

        assert (
            attempts[1].count
            == 2002
        )

        assert len(
            fake_oracle.calls
        ) == 2

        assert all(
            call["report_date"]
            == TEST_REPORT_DATE
            for call in fake_oracle.calls
        ), (
            "Both Oracle attempts must use the same "
            "scheduled report date."
        )

        print_state(
            application,
            "FINAL STATE AFTER SUCCESSFUL RETRY",
        )

        print()
        print("=" * 60)
        print("FINAL VALIDATION")
        print("=" * 60)

        print(
            "PASS: Cycle 1 created the scheduled "
            "READY occurrence."
        )

        print(
            "PASS: First execution failed through "
            "fake Oracle."
        )

        print(
            "PASS: FAILED execution remained in history."
        )

        print(
            "PASS: READY survived the failure."
        )

        print(
            "PASS: Three-minute scheduler cycle "
            "preserved the occurrence."
        )

        print(
            "PASS: READY was rebuilt into the "
            "priority queue."
        )

        print(
            "PASS: Retry executed on cycle 2."
        )

        print(
            "PASS: Retry succeeded."
        )

        print(
            "PASS: Attempt numbers are "
            "1 FAILED -> 2 SUCCESS."
        )

        print(
            "PASS: Both attempts used the same "
            "report date."
        )

        print(
            "PASS: READY was removed after "
            "successful retry."
        )

        print(
            "PASS: Fake Oracle was called exactly twice."
        )

        print()
        print(
            "3-MINUTE SCHEDULED RETRY TEST PASSED."
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

        try:
            BACKUP_DB.unlink()
        except FileNotFoundError:
            pass

        try:
            BACKUP_EXTG.unlink()
        except FileNotFoundError:
            pass

        try:
            BACKUP_DATEMAST.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
