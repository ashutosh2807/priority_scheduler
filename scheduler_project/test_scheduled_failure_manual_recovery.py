"""
Scheduled failure -> manual recovery integration test.

Scenario:
    1. Establish Job 2's scheduled occurrence for 10-Sep-2026.
    2. Its SAME_DAY=0 scheduled execution date is 11-Sep-2026.
    3. On 11-Sep, the scheduled occurrence becomes READY.
    4. First scheduled execution fails.
    5. READY must remain persistent and the FAILED attempt must remain
       in execution history.
    6. A manual run is then requested with a datetime override.
    7. The scheduler must preserve the same READY occurrence rather than
       creating a new occurrence.
    8. Manual execution succeeds against that same business report date.
    9. The one-shot manual_run and override_datetime controls must both
       be consumed automatically.
   10. The READY record is removed only after the successful manual
       execution.
   11. Execution history must contain exactly:
           attempt 1 -> FAILED
           attempt 2 -> SUCCESS

This test intentionally does NOT expect a second scheduled duplicate
after the manual recovery. Once the failed scheduled READY occurrence
is manually recovered successfully, that persistent READY occurrence has
been completed and consumed. A later scheduled duplicate test is already
covered separately by the manual-run-before-scheduled-execution scenario.

Only Oracle execution is replaced with a controlled fake executor.
SQLite, Scheduler, EligibilityEvaluator, STAGING, READY and PriorityQueue
remain real.
"""

import json
import shutil
from datetime import date, datetime
from pathlib import Path

from execution.oracle_executor import ExecutionResult
from main import close_application, create_application


PROJECT_ROOT = Path(__file__).resolve().parent

DB_FILE = PROJECT_ROOT / "scheduler.db"
EXTG_FILE = PROJECT_ROOT / "file_repository" / "Schedule_extg.json"
DATEMAST_FILE = PROJECT_ROOT / "file_repository" / "datemaster.json"

BACKUP_DB = PROJECT_ROOT / "scheduled_failure_manual_recovery_scheduler.db.bak"
BACKUP_EXTG = PROJECT_ROOT / "scheduled_failure_manual_recovery_extg.json.bak"
BACKUP_DATEMAST = (
    PROJECT_ROOT
    / "scheduled_failure_manual_recovery_datemaster.json.bak"
)

TEST_JOB_ID = 2
TEST_REPORT_DATE = date(2026, 9, 10)
TEST_EXECUTION_DATE = date(2026, 9, 11)

SCHEDULED_OCCURRENCE_TIME = datetime(
    2026,
    9,
    10,
    10,
    30,
    0,
)

SCHEDULED_EXECUTION_TIME = datetime(
    2026,
    9,
    11,
    10,
    30,
    0,
)

MANUAL_OVERRIDE = datetime(
    2026,
    9,
    11,
    15,
    0,
    0,
)


class ControlledOracleExecutor:
    """
    Fake Oracle executor.

    Call 1:
        FAILED

    Call 2:
        SUCCESS
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

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        if call_number == 1:
            return ExecutionResult(
                success=False,
                procedure_name=procedure_name,
                report_date=report_date,
                count=None,
                error="Simulated scheduled Oracle failure.",
                error_type="SimulatedFailure",
                started_at=now,
                finished_at=now,
                duration_seconds=0.1,
            )

        return ExecutionResult(
            success=True,
            procedure_name=procedure_name,
            report_date=report_date,
            count=9000 + call_number,
            error=None,
            error_type=None,
            started_at=now,
            finished_at=now,
            duration_seconds=0.1,
        )

    def close(self):
        pass


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
    with DATEMAST_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        values = json.load(file)

    test_value = TEST_REPORT_DATE.strftime(
        "%d-%m-%Y"
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


def clear_test_occurrence(application):
    connection = application["connection"]

    application["ready_repository"].delete(
        TEST_JOB_ID
    )

    application["staging_repository"].delete(
        TEST_JOB_ID
    )

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


def get_history(application):
    rows = application[
        "execution_repository"
    ].get_all()

    return [
        row
        for row in rows
        if (
            row.job_id == TEST_JOB_ID
            and row.report_date == TEST_REPORT_DATE
        )
    ]


def print_state(
    application,
    title,
):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)

    staging = application[
        "staging_repository"
    ].get_by_id(
        TEST_JOB_ID
    )

    ready = application[
        "ready_repository"
    ].get_by_id(
        TEST_JOB_ID
    )

    control = application[
        "job_control_repository"
    ].get(
        TEST_JOB_ID
    )

    print("STAGING:")
    print(staging)

    print()
    print("READY:")
    print(ready)

    print()
    print("JOB CONTROL:")
    print(control)

    print()
    print("QUEUE SIZE:")
    print(
        application[
            "priority_queue"
        ].size()
    )

    print()
    print("EXECUTION HISTORY:")
    for row in get_history(application):
        print(row)


def main():
    backup_files()

    application = None

    try:
        print("=" * 72)
        print(
            "SCHEDULED FAILURE -> MANUAL RECOVERY TEST"
        )
        print("=" * 72)

        prepare_unique_datemast()

        application = create_application()

        clear_test_occurrence(
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

        fake_oracle = ControlledOracleExecutor()

        application[
            "execution_manager"
        ].oracle_executor = fake_oracle

        # ---------------------------------------------------------
        # STEP 1
        # Establish the scheduled occurrence.
        # ---------------------------------------------------------
        print()
        print("=" * 72)
        print(
            "STEP 1 - ESTABLISH SCHEDULED OCCURRENCE"
        )
        print("=" * 72)

        application[
            "scheduler"
        ].run_cycle(
            current_datetime=SCHEDULED_OCCURRENCE_TIME
        )

        staging = application[
            "staging_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert staging is not None, (
            "Scheduled occurrence should exist in STAGING."
        )

        assert (
            str(staging.occurrence_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Scheduled occurrence_date is incorrect."
        )

        assert (
            str(staging.execution_date)
            == TEST_EXECUTION_DATE.isoformat()
        ), (
            "Scheduled execution_date is incorrect."
        )

        assert (
            str(staging.report_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Scheduled report_date is incorrect."
        )

        print_state(
            application,
            "STATE AFTER STEP 1",
        )

        # ---------------------------------------------------------
        # STEP 2
        # Move scheduled occurrence to READY and fail it.
        # ---------------------------------------------------------
        print()
        print("=" * 72)
        print(
            "STEP 2 - SCHEDULED EXECUTION FAILS"
        )
        print("=" * 72)

        application[
            "scheduler"
        ].run_cycle(
            current_datetime=SCHEDULED_EXECUTION_TIME
        )

        ready_before_failure = application[
            "ready_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert ready_before_failure is not None, (
            "Scheduled occurrence should become READY."
        )

        assert (
            str(ready_before_failure.report_date)
            == TEST_REPORT_DATE.isoformat()
        )

        scheduled_results = application[
            "execution_manager"
        ].execute_available()

        print()
        print(
            "SCHEDULED EXECUTION RESULTS:"
        )

        for result in scheduled_results:
            print(result)

        assert len(scheduled_results) == 1

        scheduled_failure = scheduled_results[0]

        assert scheduled_failure["executed"] is True
        assert scheduled_failure["status"] == "FAILED"
        assert scheduled_failure["duplicate"] is False
        assert (
            scheduled_failure["attempt_no"] == 1
        )
        assert (
            scheduled_failure["report_date"]
            == TEST_REPORT_DATE
        )

        assert len(fake_oracle.calls) == 1
        assert (
            fake_oracle.calls[0]["report_date"]
            == TEST_REPORT_DATE
        )

        # FAILED scheduled execution must NOT consume READY.
        ready_after_failure = application[
            "ready_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert ready_after_failure is not None, (
            "READY must persist after a scheduled failure."
        )

        history_after_failure = get_history(
            application
        )

        assert len(history_after_failure) == 1, (
            "Exactly one execution attempt should exist "
            "after the first scheduled failure."
        )

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
            "STATE AFTER SCHEDULED FAILURE",
        )

        # ---------------------------------------------------------
        # STEP 3
        # Request manual recovery with an override.
        #
        # The existing READY occurrence is the same scheduled
        # occurrence. Manual execution must not create a new
        # report/occurrence.
        # ---------------------------------------------------------
        print()
        print("=" * 72)
        print(
            "STEP 3 - REQUEST MANUAL RECOVERY + OVERRIDE"
        )
        print("=" * 72)

        controls.set_override_datetime(
            TEST_JOB_ID,
            MANUAL_OVERRIDE.isoformat(),
        )

        controls.request_manual_run(
            TEST_JOB_ID
        )

        control_before_manual = controls.get(
            TEST_JOB_ID
        )

        assert (
            control_before_manual["manual_run"]
            in (1, "1", True)
        )

        assert (
            str(
                control_before_manual[
                    "override_datetime"
                ]
            )
            == MANUAL_OVERRIDE.isoformat()
        )

        print(
            "CONTROL BEFORE MANUAL RECOVERY:"
        )
        print(
            control_before_manual
        )

        # ---------------------------------------------------------
        # STEP 4
        # Scheduler sees the existing READY occurrence.
        #
        # It must not create a second occurrence.
        # ---------------------------------------------------------
        print()
        print("=" * 72)
        print(
            "STEP 4 - MANUAL RECOVERY SCHEDULER CYCLE"
        )
        print("=" * 72)

        application[
            "scheduler"
        ].run_cycle(
            current_datetime=MANUAL_OVERRIDE
        )

        ready_before_manual = application[
            "ready_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert ready_before_manual is not None, (
            "The failed scheduled READY occurrence must remain "
            "available for manual recovery."
        )

        assert (
            str(ready_before_manual.report_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Manual recovery must retain the original report_date."
        )

        print_state(
            application,
            "STATE BEFORE MANUAL RECOVERY EXECUTION",
        )

        # ---------------------------------------------------------
        # STEP 5
        # Manual recovery succeeds.
        # ---------------------------------------------------------
        print()
        print("=" * 72)
        print(
            "STEP 5 - MANUAL RECOVERY SUCCEEDS"
        )
        print("=" * 72)

        manual_results = application[
            "execution_manager"
        ].execute_available()

        print()
        print(
            "MANUAL RECOVERY RESULTS:"
        )

        for result in manual_results:
            print(result)

        assert len(manual_results) == 1

        manual_success = manual_results[0]

        assert manual_success["executed"] is True
        assert manual_success["status"] == "SUCCESS"
        assert manual_success["duplicate"] is False
        assert (
            manual_success["attempt_no"] == 2
        ), (
            "Manual recovery should create the next execution attempt "
            "for the same report_date."
        )
        assert (
            manual_success["report_date"]
            == TEST_REPORT_DATE
        )

        assert len(fake_oracle.calls) == 2

        assert (
            fake_oracle.calls[0]["report_date"]
            == TEST_REPORT_DATE
        )

        assert (
            fake_oracle.calls[1]["report_date"]
            == TEST_REPORT_DATE
        )

        # Successful manual recovery consumes READY.
        ready_after_success = application[
            "ready_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert ready_after_success is None, (
            "READY must be consumed after successful manual recovery."
        )

        # ---------------------------------------------------------
        # STEP 6
        # Both one-shot controls must be cleared automatically.
        # ---------------------------------------------------------
        print()
        print("=" * 72)
        print(
            "STEP 6 - VERIFY ONE-SHOT CONTROL CLEANUP"
        )
        print("=" * 72)

        control_after_manual = controls.get(
            TEST_JOB_ID
        )

        print(
            "CONTROL AFTER MANUAL RECOVERY:"
        )
        print(
            control_after_manual
        )

        assert control_after_manual is not None

        assert (
            control_after_manual["manual_run"]
            in (0, "0", False)
        ), (
            "manual_run must be cleared automatically."
        )

        assert (
            control_after_manual[
                "override_datetime"
            ]
            is None
        ), (
            "override_datetime must be cleared automatically."
        )

        # ---------------------------------------------------------
        # STEP 7
        # Verify complete execution history.
        # ---------------------------------------------------------
        print()
        print("=" * 72)
        print(
            "STEP 7 - VERIFY EXECUTION HISTORY"
        )
        print("=" * 72)

        history = get_history(
            application
        )

        for row in history:
            print(row)

        assert len(history) == 2, (
            "Expected exactly two attempts: "
            "scheduled FAILED + manual SUCCESS."
        )

        # Repository retrieval order is not the business ordering
        # contract.  Validate execution history by attempt number.
        history_by_attempt = {
            row.attempt_no: row
            for row in history
        }

        assert set(history_by_attempt) == {1, 2}, (
            "Expected execution attempts 1 and 2."
        )

        first_attempt = history_by_attempt[1]
        second_attempt = history_by_attempt[2]

        assert first_attempt.status == "FAILED"
        assert first_attempt.report_date == TEST_REPORT_DATE

        assert second_attempt.status == "SUCCESS"
        assert second_attempt.report_date == TEST_REPORT_DATE

        assert (
            application[
                "priority_queue"
            ].size() == 0
        ), (
            "The successful manual recovery should leave no "
            "READY item in the derived queue."
        )

        print()
        print("=" * 72)
        print(
            "FINAL VALIDATION"
        )
        print("=" * 72)

        print(
            "PASS: Scheduled occurrence was established."
        )

        print(
            "PASS: Scheduled execution failed on attempt 1."
        )

        print(
            "PASS: FAILED scheduled execution preserved READY."
        )

        print(
            "PASS: Manual recovery reused the same report_date."
        )

        print(
            "PASS: Manual recovery succeeded on attempt 2."
        )

        print(
            "PASS: Oracle was called exactly twice."
        )

        print(
            "PASS: manual_run was automatically cleared."
        )

        print(
            "PASS: override_datetime was automatically cleared."
        )

        print(
            "PASS: READY was consumed after successful recovery."
        )

        print(
            "PASS: Execution history contains FAILED + SUCCESS."
        )

        print()
        print(
            "SCHEDULED FAILURE -> MANUAL RECOVERY TEST PASSED."
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
