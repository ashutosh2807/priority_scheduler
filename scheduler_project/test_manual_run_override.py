"""
Manual-run / datetime-override integration test.

Scenario:
    1. Establish Job 2's scheduled occurrence for 10-Sep-2026.
    2. The occurrence is SAME_DAY=0, so its scheduled execution date is
       11-Sep-2026.
    3. On 10-Sep, request a manual run with an override datetime of
       10-Sep-2026 15:00.
    4. Scheduler must make that manual request READY immediately.
    5. ExecutionManager must execute it using the original business
       report_date = 10-Sep-2026.
    6. The override/manual action must not create a second occurrence,
       move the report date, or change the scheduled execution context.
    7. After the manual run is consumed, the manual control is cleared.
    8. The same scheduled occurrence must not be re-executed as a duplicate.

Only Oracle execution is replaced with a controlled fake executor.
SQLite, Scheduler, EligibilityEvaluator, STAGING, READY and PriorityQueue
remain real.
"""

import json
import shutil
from datetime import date, datetime
from pathlib import Path

from main import create_application, close_application
from execution.oracle_executor import ExecutionResult


PROJECT_ROOT = Path(__file__).resolve().parent

DB_FILE = PROJECT_ROOT / "scheduler.db"
EXTG_FILE = PROJECT_ROOT / "file_repository" / "Schedule_extg.json"
DATEMAST_FILE = PROJECT_ROOT / "file_repository" / "datemaster.json"

BACKUP_DB = PROJECT_ROOT / "manual_override_test_scheduler.db.bak"
BACKUP_EXTG = PROJECT_ROOT / "manual_override_test_extg.json.bak"
BACKUP_DATEMAST = PROJECT_ROOT / "manual_override_test_datemaster.json.bak"

TEST_JOB_ID = 2
TEST_REPORT_DATE = date(2026, 9, 10)
TEST_EXECUTION_DATE = date(2026, 9, 11)

SCHEDULED_TIME = datetime(2026, 9, 10, 10, 30, 0)
MANUAL_OVERRIDE = datetime(2026, 9, 10, 15, 0, 0)
POST_MANUAL_TIME = datetime(2026, 9, 10, 15, 3, 0)
SCHEDULED_EXECUTION_TIME = datetime(2026, 9, 11, 10, 30, 0)


class ControlledOracleExecutor:
    """Fake Oracle executor. Every manual execution succeeds."""

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

        return ExecutionResult(
            success=True,
            procedure_name=procedure_name,
            report_date=report_date,
            count=5000 + call_number,
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
        shutil.copy2(BACKUP_DATEMAST, DATEMAST_FILE)


def prepare_unique_datemast():
    with DATEMAST_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        values = json.load(file)

    test_value = TEST_REPORT_DATE.strftime("%d-%m-%Y")

    values = [
        value
        for value in values
        if value != test_value
    ]

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


def clear_test_occurrence(application):
    connection = application["connection"]
    ready_repository = application["ready_repository"]
    staging_repository = application["staging_repository"]

    ready_repository.delete(TEST_JOB_ID)
    staging_repository.delete(TEST_JOB_ID)

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
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)

    ready = application["ready_repository"].get_by_id(TEST_JOB_ID)
    staging = application["staging_repository"].get_by_id(TEST_JOB_ID)
    control = application["job_control_repository"].get(TEST_JOB_ID)

    print("READY:")
    print(ready)

    print()
    print("STAGING:")
    print(staging)

    print()
    print("JOB CONTROL:")
    print(control)

    print()
    print("QUEUE SIZE:")
    print(application["priority_queue"].size())

    print()
    print("QUEUE:")
    for item in application["priority_queue"].get_all():
        print(item)


def get_history(application):
    execution_repository = application["execution_repository"]

    return [
        row
        for row in execution_repository.get_all()
        if (
            row.job_id == TEST_JOB_ID
            and row.report_date == TEST_REPORT_DATE
        )
    ]


def main():
    backup_files()
    application = None

    try:
        print("=" * 60)
        print("MANUAL RUN + DATETIME OVERRIDE TEST")
        print("=" * 60)

        prepare_unique_datemast()

        application = create_application()
        clear_test_occurrence(application)

        controls = application["job_control_repository"]
        controls.clear_manual_run(TEST_JOB_ID)
        controls.clear_override_datetime(TEST_JOB_ID)

        fake_oracle = ControlledOracleExecutor()
        application[
            "execution_manager"
        ].oracle_executor = fake_oracle

        # ---------------------------------------------------------
        # STEP 1: Establish the normal scheduled occurrence.
        # ---------------------------------------------------------
        print()
        print("=" * 60)
        print("STEP 1 - ESTABLISH SCHEDULED OCCURRENCE")
        print("=" * 60)

        summary_1 = application[
            "scheduler"
        ].run_cycle(
            current_datetime=SCHEDULED_TIME
        )

        print(summary_1)
        print_state(
            application,
            "STATE AFTER SCHEDULED OCCURRENCE ESTABLISHMENT",
        )

        staging_before_manual = application[
            "staging_repository"
        ].get_by_id(TEST_JOB_ID)

        assert staging_before_manual is not None, (
            "Expected the scheduled occurrence to be persisted "
            "in STAGING."
        )

        assert (
            str(staging_before_manual.occurrence_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            str(staging_before_manual.execution_date)
            == TEST_EXECUTION_DATE.isoformat()
        )

        assert (
            str(staging_before_manual.report_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            str(staging_before_manual.t_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            str(staging_before_manual.target_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            staging_before_manual.state
            == "WAITING_EXECUTION_DATE"
        )

        assert (
            application[
                "ready_repository"
            ].get_by_id(TEST_JOB_ID)
            is None
        )

        # ---------------------------------------------------------
        # STEP 2: Django-like manual request with datetime override.
        # ---------------------------------------------------------
        print()
        print("=" * 60)
        print("STEP 2 - REQUEST MANUAL RUN + OVERRIDE")
        print("=" * 60)

        controls.set_override_datetime(
            TEST_JOB_ID,
            MANUAL_OVERRIDE.isoformat(),
        )
        controls.request_manual_run(TEST_JOB_ID)

        control_before = controls.get(TEST_JOB_ID)

        print("CONTROL BEFORE MANUAL CYCLE:")
        print(control_before)

        assert (
            control_before["manual_run"]
            in (1, "1", True)
        )

        assert (
            str(control_before["override_datetime"])
            == MANUAL_OVERRIDE.isoformat()
        )

        # ---------------------------------------------------------
        # STEP 3: Scheduler processes manual request.
        # ---------------------------------------------------------
        print()
        print("=" * 60)
        print("STEP 3 - MANUAL SCHEDULER CYCLE")
        print("=" * 60)

        summary_2 = application[
            "scheduler"
        ].run_cycle(
            current_datetime=SCHEDULED_TIME
        )

        print(summary_2)
        print_state(
            application,
            "STATE AFTER MANUAL SCHEDULER CYCLE",
        )

        ready_manual = application[
            "ready_repository"
        ].get_by_id(TEST_JOB_ID)

        assert ready_manual is not None, (
            "Manual run must become READY immediately."
        )

        # The critical business invariant:
        assert (
            str(ready_manual.occurrence_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Manual override must not replace the occurrence date."
        )

        assert (
            str(ready_manual.report_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Manual override must not replace the business report date."
        )

        assert (
            str(ready_manual.execution_date)
            == TEST_REPORT_DATE.isoformat()
        ), (
            "Manual override should use the override date as the "
            "manual execution date."
        )

        assert ready_manual.t_date is None, (
            "A manual run should not manufacture a scheduled T date."
        )

        assert (
            str(ready_manual.target_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            application["priority_queue"].size() == 1
        )

        # ---------------------------------------------------------
        # STEP 4: Execute manual READY occurrence.
        # ---------------------------------------------------------
        print()
        print("=" * 60)
        print("STEP 4 - EXECUTE MANUAL RUN")
        print("=" * 60)

        results = application[
            "execution_manager"
        ].execute_available()

        for result in results:
            print(result)

        assert len(results) == 1

        result = results[0]

        assert result["executed"] is True
        assert result["status"] == "SUCCESS"
        assert result["duplicate"] is False
        assert (
            result["report_date"]
            == TEST_REPORT_DATE
        )

        assert len(fake_oracle.calls) == 1
        assert (
            fake_oracle.calls[0]["report_date"]
            == TEST_REPORT_DATE
        )

        ready_after_manual = application[
            "ready_repository"
        ].get_by_id(TEST_JOB_ID)

        assert ready_after_manual is None, (
            "READY must be consumed after successful manual execution."
        )

        # ---------------------------------------------------------
        # STEP 5: Manual control should be consumed/cleared.
        # ---------------------------------------------------------
        control_after_execution = controls.get(TEST_JOB_ID)

        print()
        print("CONTROL AFTER MANUAL EXECUTION:")
        print(control_after_execution)

        assert control_after_execution is not None

        assert (
            control_after_execution["manual_run"]
            in (0, "0", False)
        ), (
            "Manual-run control should be cleared after "
            "the manual execution is consumed."
        )

        # The datetime override is a one-shot manual execution control.
        # Clear it before advancing the scheduler to the real scheduled
        # execution date; otherwise the scheduler would continue using
        # the old 10-Sep override instead of the 11-Sep cycle datetime.
        controls.clear_override_datetime(TEST_JOB_ID)

        control_after_manual_cleanup = controls.get(TEST_JOB_ID)

        print()
        print("JOB CONTROL AFTER MANUAL OVERRIDE CLEANUP:")
        print(control_after_manual_cleanup)

        assert control_after_manual_cleanup is not None
        assert control_after_manual_cleanup["override_datetime"] is None

        # ---------------------------------------------------------
        # STEP 6: Advance to the scheduled execution date.
        #
        # The original scheduled occurrence is still in STAGING.
        # Its scheduled execution date is 11-Sep-2026.
        #
        # The scheduler should now promote that scheduled occurrence
        # to READY. ExecutionManager must then detect that the same
        # job/report_date already completed successfully through the
        # manual run and skip Oracle as a duplicate.
        # ---------------------------------------------------------
        print()
        print("=" * 60)
        print("STEP 6 - SCHEDULED EXECUTION DATE / DUPLICATE PROTECTION")
        print("=" * 60)

        summary_4 = application[
            "scheduler"
        ].run_cycle(
            current_datetime=SCHEDULED_EXECUTION_TIME
        )

        print(summary_4)
        print_state(
            application,
            "STATE AFTER SCHEDULED EXECUTION-DATE CYCLE",
        )

        ready_scheduled = application[
            "ready_repository"
        ].get_by_id(TEST_JOB_ID)

        assert ready_scheduled is not None, (
            "The original scheduled occurrence should become READY "
            "on its scheduled execution date."
        )

        assert (
            str(ready_scheduled.occurrence_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            str(ready_scheduled.execution_date)
            == TEST_EXECUTION_DATE.isoformat()
        )

        assert (
            str(ready_scheduled.report_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            str(ready_scheduled.t_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            str(ready_scheduled.target_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            application["priority_queue"].size() == 1
        )

        scheduled_execution_results = application[
            "execution_manager"
        ].execute_available()

        print()
        print("SCHEDULED EXECUTION RESULTS:")
        for result in scheduled_execution_results:
            print(result)

        assert len(scheduled_execution_results) == 1

        duplicate_result = scheduled_execution_results[0]

        assert duplicate_result["executed"] is False, (
            "Scheduled execution must not call Oracle again after "
            "the manual execution already succeeded."
        )

        assert duplicate_result["duplicate"] is True, (
            "The scheduled execution should be classified as a duplicate."
        )

        assert duplicate_result["report_date"] == TEST_REPORT_DATE, (
            "Duplicate detection must use the original business "
            "report date."
        )

        assert len(fake_oracle.calls) == 1, (
            "Oracle must have been called only once by the manual run."
        )

        ready_after_duplicate = application[
            "ready_repository"
        ].get_by_id(TEST_JOB_ID)

        assert ready_after_duplicate is None, (
            "The duplicate scheduled READY record should be consumed "
            "without executing Oracle."
        )

        assert (
            application["priority_queue"].size() == 0
        )

        history = get_history(application)

        assert len(history) == 1, (
            "Only the manual execution should exist in execution history."
        )

        assert history[0].status == "SUCCESS"
        assert history[0].attempt_no == 1
        assert history[0].report_date == TEST_REPORT_DATE

        print()
        print()
        print("=" * 60)
        print("FINAL VALIDATION")
        print("=" * 60)

        print(
            "PASS: Scheduled occurrence was established in STAGING."
        )
        print(
            "PASS: Manual run request was accepted with datetime override."
        )
        print(
            "PASS: Manual run became READY immediately."
        )
        print(
            "PASS: occurrence_date remained 2026-09-10."
        )
        print(
            "PASS: report_date remained 2026-09-10."
        )
        print(
            "PASS: manual execution_date used the override date 2026-09-10."
        )
        print(
            "PASS: Manual run did not manufacture a scheduled T date."
        )
        print(
            "PASS: Manual execution passed 2026-09-10 to fake Oracle."
        )
        print(
            "PASS: Manual-run control was cleared after execution."
        )
        print(
            "PASS: Original scheduled STAGING occurrence remained intact."
        )
        print(
            "PASS: Scheduled occurrence became READY on 2026-09-11."
        )
        print(
            "PASS: Scheduled duplicate protection prevented a second Oracle call."
        )
        print(
            "PASS: Only one successful execution exists for report_date 2026-09-10."
        )

        print()
        print(
            "MANUAL RUN + DATETIME OVERRIDE TEST PASSED."
        )



    finally:
        if application is not None:
            try:
                close_application(application)
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
