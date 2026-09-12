"""
Retry exhaustion test for the scheduler.

Verifies:

    Attempt 1 -> FAILED -> retry allowed
    Attempt 2 -> FAILED -> retry allowed
    Attempt 3 -> FAILED -> retry exhausted
    Attempt 4 -> Oracle must NOT be called

The real scheduler and execution manager are used.
Only Oracle execution is replaced with a controlled fake executor.

The test uses job 2 (FTD_EXTRACT) and a temporary DATEMAST
report date of 2026-09-10.

All modified files are restored when the test finishes.
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
    / "retry_exhaustion_test_scheduler.db.bak"
)

BACKUP_EXTG = (
    PROJECT_ROOT
    / "retry_exhaustion_test_extg.json.bak"
)

BACKUP_DATEMAST = (
    PROJECT_ROOT
    / "retry_exhaustion_test_datemaster.json.bak"
)


TEST_JOB_ID = 2

TEST_REPORT_DATE = date(2026, 9, 10)

CYCLE_TIMES = [
    datetime(2026, 9, 10, 10, 30, 0),
    datetime(2026, 9, 11, 10, 30, 0),
    datetime(2026, 9, 11, 10, 33, 0),
    datetime(2026, 9, 11, 10, 36, 0),
    datetime(2026, 9, 11, 10, 39, 0),
]


class AlwaysFailOracleExecutor:
    """
    Fake Oracle executor.

    Every Oracle call fails.

    The call counter is used to prove that the fourth cycle
    never reaches Oracle after MAX_SCHEDULED_ATTEMPTS=3.
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
        print(
            "FAKE RESULT: FAILED"
        )

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        return ExecutionResult(
            success=False,
            procedure_name=procedure_name,
            report_date=report_date,
            count=None,
            error="Controlled retry exhaustion test failure",
            error_type="ControlledTestError",
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


def clean_test_occurrence(
    application,
):
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


def get_test_history(
    application,
):
    execution_repository = application[
        "execution_repository"
    ]

    history = [
        row
        for row in execution_repository.get_all()
        if (
            row.job_id == TEST_JOB_ID
            and row.report_date == TEST_REPORT_DATE
        )
    ]

    return sorted(
        history,
        key=lambda row: row.attempt_no,
    )


def run_cycle(
    application,
    fake_oracle,
    cycle_number,
    cycle_time,
):
    scheduler = application["scheduler"]

    execution_manager = application[
        "execution_manager"
    ]

    print()
    print("=" * 60)
    print(
        f"CYCLE {cycle_number} - "
        f"{cycle_time.strftime('%H:%M')}"
    )
    print("=" * 60)

    summary = scheduler.run_cycle(
        current_datetime=cycle_time
    )

    print()
    print("SCHEDULER SUMMARY:")
    print(summary)

    ready = application[
        "ready_repository"
    ].get_by_id(
        TEST_JOB_ID
    )

    print()
    print("READY:")
    print(ready)

    print()
    print("QUEUE SIZE:")
    print(
        application["priority_queue"].size()
    )

    results = (
        execution_manager
        .execute_available()
    )

    print()
    print("EXECUTION RESULTS:")

    for result in results:
        print(result)

    print()
    print(
        "FAKE ORACLE CALL COUNT:",
        len(fake_oracle.calls),
    )

    return results


def main():
    application = None

    backup_files()

    try:
        print("=" * 60)
        print("RETRY EXHAUSTION TEST")
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

        fake_oracle = AlwaysFailOracleExecutor()

        application[
            "execution_manager"
        ].oracle_executor = fake_oracle

        # ---------------------------------------------------------
        # OCCURRENCE ESTABLISHMENT
        #
        # Job 2 is SAME_DAY=0.  The 10-Sep occurrence is established
        # first and is scheduled for execution on the next working day,
        # 11-Sep.
        # ---------------------------------------------------------

        results_0 = run_cycle(
            application,
            fake_oracle,
            0,
            CYCLE_TIMES[0],
        )

        print()
        print("OCCURRENCE ESTABLISHMENT EXECUTION RESULTS:")
        for result in results_0:
            print(result)

        ready_0 = application[
            "ready_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert ready_0 is None, (
            "The SAME_DAY=0 occurrence must not be READY "
            "before its execution date."
        )

        staging_0 = application[
            "staging_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert staging_0 is not None, (
            "The occurrence must be persisted in STAGING."
        )

        assert (
            str(staging_0.occurrence_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert (
            str(staging_0.execution_date)
            == "2026-09-11"
        )

        assert (
            str(staging_0.report_date)
            == TEST_REPORT_DATE.isoformat()
        )

        assert len(results_0) == 0, (
            "No Oracle execution should occur before execution_date."
        )

        assert len(fake_oracle.calls) == 0

        # ---------------------------------------------------------
        # CYCLE 1 - first execution / FAILED
        # ---------------------------------------------------------

        results_1 = run_cycle(
            application,
            fake_oracle,
            1,
            CYCLE_TIMES[1],
        )

        print()
        print("CYCLE 1 SUMMARY:")
        

        assert len(results_1) == 1
        assert results_1[0]["status"] == "FAILED"
        assert results_1[0]["executed"] is True
        assert results_1[0]["duplicate"] is False
        assert (
            results_1[0]["retry_exhausted"]
            is False
        ), (
            "Attempt 1 must not be retry-exhausted."
        )
        assert results_1[0]["attempt_no"] == 1
        assert results_1[0]["max_attempts"] == 3
        assert len(fake_oracle.calls) == 1

        ready_after_1 = application[
            "ready_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert ready_after_1 is not None, (
            "READY must remain after attempt 1 failure."
        )

        assert (
            str(ready_after_1.report_date)
            == TEST_REPORT_DATE.isoformat()
        )

        # ---------------------------------------------------------
        # CYCLE 2 - second execution / FAILED
        # ---------------------------------------------------------

        results_2 = run_cycle(
            application,
            fake_oracle,
            2,
            CYCLE_TIMES[2],
        )

        print()
        print("CYCLE 2 SUMMARY:")
        

        assert len(results_2) == 1
        assert results_2[0]["status"] == "FAILED"
        assert results_2[0]["executed"] is True
        assert results_2[0]["duplicate"] is False
        assert (
            results_2[0]["retry_exhausted"]
            is False
        ), (
            "Attempt 2 must still be retryable."
        )
        assert results_2[0]["attempt_no"] == 2
        assert results_2[0]["max_attempts"] == 3
        assert len(fake_oracle.calls) == 2

        # ---------------------------------------------------------
        # CYCLE 3 - third execution / FAILED / EXHAUSTED
        # ---------------------------------------------------------

        results_3 = run_cycle(
            application,
            fake_oracle,
            3,
            CYCLE_TIMES[3],
        )

        print()
        print("CYCLE 3 SUMMARY:")
        

        assert len(results_3) == 1
        assert results_3[0]["status"] == "FAILED"
        assert results_3[0]["executed"] is True
        assert results_3[0]["duplicate"] is False
        assert (
            results_3[0]["retry_exhausted"]
            is True
        ), (
            "Attempt 3 must mark the occurrence as retry-exhausted."
        )
        assert results_3[0]["attempt_no"] == 3
        assert results_3[0]["max_attempts"] == 3
        assert len(fake_oracle.calls) == 3

        # ---------------------------------------------------------
        # CYCLE 4 - exhausted occurrence / NO Oracle call
        # ---------------------------------------------------------

        results_4 = run_cycle(
            application,
            fake_oracle,
            4,
            CYCLE_TIMES[4],
        )

        print()
        print("CYCLE 4 SUMMARY:")
        

        assert len(results_4) == 1
        assert (
            results_4[0]["executed"] is False
        ), (
            "Fourth cycle must not execute Oracle "
            "after retry exhaustion."
        )
        assert (
            results_4[0]["retry_exhausted"] is True
        ), (
            "Fourth cycle must identify the occurrence "
            "as retry-exhausted."
        )
        assert results_4[0]["attempt_no"] == 3
        assert results_4[0]["max_attempts"] == 3
        assert len(fake_oracle.calls) == 3

        # ---------------------------------------------------------
        # Execution history
        # ---------------------------------------------------------

        history = get_test_history(
            application
        )

        assert len(history) == 3

        assert [
            row.attempt_no
            for row in history
        ] == [1, 2, 3]

        assert all(
            row.status == "FAILED"
            for row in history
        )

        assert all(
            row.report_date
            == TEST_REPORT_DATE
            for row in history
        )

        assert all(
            row.job_id == TEST_JOB_ID
            for row in history
        )

        ready = application[
            "ready_repository"
        ].get_by_id(
            TEST_JOB_ID
        )

        assert ready is not None, (
            "The exhausted occurrence should "
            "remain visible in READY for monitoring."
        )

        # ---------------------------------------------------------
        # Final validation
        # ---------------------------------------------------------

        print()
        print("=" * 60)
        print("FINAL VALIDATION")
        print("=" * 60)

        print(
            "PASS: Occurrence established before execution_date."
        )

        print(
            "PASS: Attempt 1 FAILED and remained retryable."
        )

        print(
            "PASS: Attempt 2 FAILED and remained retryable."
        )

        print(
            "PASS: Attempt 3 FAILED and exhausted retries."
        )

        print(
            "PASS: Attempt 4 did not call Oracle."
        )

        print(
            "PASS: Exactly 3 Oracle calls were made."
        )

        print(
            "PASS: Execution history contains "
            "exactly attempts 1, 2 and 3."
        )

        print(
            "PASS: All attempts have the same "
            "job_id and report_date."
        )

        print(
            "PASS: Exhausted occurrence remains "
            "visible in READY for monitoring."
        )

        print()
        print(
            "RETRY EXHAUSTION TEST PASSED."
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
