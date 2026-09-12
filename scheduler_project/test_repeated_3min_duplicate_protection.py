"""
Repeated 3-minute-cycle duplicate-protection test.

This test verifies the most important production property after a
scheduled occurrence succeeds:

    SUCCESS must prevent the same scheduled occurrence from executing
    again on later scheduler cycles.

It also verifies the retry path:

    FAILED -> READY remains -> RETRY -> SUCCESS
                                  |
                                  +-> later cycles are duplicate-skipped

The REAL scheduler, eligibility, repositories, staging and priority
queue are used.

Only Oracle execution is replaced with a controlled fake executor.

Two independent scenarios are tested:

SCENARIO 1
----------
Cycle 1: SUCCESS
Cycle 2: duplicate protection
Cycle 3: duplicate protection
Cycle 4: duplicate protection

Expected Oracle calls: 1


SCENARIO 2
----------
Cycle 1: FAILED
Cycle 2: RETRY -> SUCCESS
Cycle 3: duplicate protection
Cycle 4: duplicate protection

Expected Oracle calls: 2

The test temporarily adds 10-09-2026 to DATEMAST and restores the
original DATEMAST, Schedule_extg.json and scheduler.db afterward.
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
    / "scheduler_duplicate_test_scheduler.db.bak"
)

BACKUP_EXTG = (
    PROJECT_ROOT
    / "scheduler_duplicate_test_extg.json.bak"
)

BACKUP_DATEMAST = (
    PROJECT_ROOT
    / "scheduler_duplicate_test_datemaster.json.bak"
)


TEST_JOB_ID = 2

TEST_REPORT_DATE = date(2026, 9, 10)

TEST_PROCEDURE = "SCHEDULE_EXTRACTS.tltro"

CYCLE_TIMES = [
    datetime(2026, 9, 10, 10, 30, 0),
    datetime(2026, 9, 10, 10, 33, 0),
    datetime(2026, 9, 10, 10, 36, 0),
    datetime(2026, 9, 10, 10, 39, 0),
]


class ControlledOracleExecutor:
    """
    Controlled fake Oracle executor.

    mode="success":
        every call succeeds.

    mode="fail_once":
        first call fails, every later call succeeds.
    """

    def __init__(
        self,
        mode,
    ):
        self.mode = mode
        self.calls = []

    def execute(
        self,
        procedure_name,
        report_date=None,
        **kwargs,
    ):
        call_number = len(
            self.calls
        ) + 1

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

        should_fail = (
            self.mode == "fail_once"
            and call_number == 1
        )

        if should_fail:
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
                error=(
                    "Controlled duplicate-protection "
                    "test failure"
                ),
                error_type="ControlledTestError",
                started_at=now,
                finished_at=now,
                duration_seconds=0.1,
            )

        print(
            "FAKE RESULT: SUCCESS"
        )

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        return ExecutionResult(
            success=True,
            procedure_name=procedure_name,
            report_date=report_date,
            count=4000 + call_number,
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

    connection = (
        execution_repository.connection
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


def get_test_history(
    application,
):
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


def print_cycle_state(
    application,
    cycle_number,
    summary,
):
    ready_repository = application[
        "ready_repository"
    ]

    priority_queue = application[
        "priority_queue"
    ]

    print()
    print("=" * 60)
    print(
        f"CYCLE {cycle_number} STATE"
    )
    print("=" * 60)

    print(
        "SCHEDULER SUMMARY:"
    )
    print(summary)

    print()
    print("READY:")

    print(
        ready_repository.get_by_id(
            TEST_JOB_ID
        )
    )

    print()
    print(
        "QUEUE SIZE:"
    )

    print(
        priority_queue.size()
    )

    print()
    print("QUEUE:")

    for item in priority_queue.get_all():
        print(item)


def execute_cycle(
    application,
    cycle_number,
    current_datetime,
):
    scheduler = application[
        "scheduler"
    ]

    execution_manager = application[
        "execution_manager"
    ]

    print()
    print("=" * 60)
    print(
        f"CYCLE {cycle_number} - "
        f"{current_datetime.strftime('%H:%M')}"
    )
    print("=" * 60)

    summary = scheduler.run_cycle(
        current_datetime=current_datetime
    )

    print_cycle_state(
        application,
        cycle_number,
        summary,
    )

    results = (
        execution_manager
        .execute_available()
    )

    print()
    print(
        f"CYCLE {cycle_number} EXECUTION RESULTS:"
    )

    for result in results:
        print(result)

    return summary, results


def run_success_scenario():
    """
    Scenario 1:

        SUCCESS on first cycle.

    Later cycles must not execute the occurrence again.
    """

    application = None

    try:
        print()
        print("=" * 60)
        print(
            "SCENARIO 1 - SUCCESS THEN "
            "REPEATED 3-MINUTE CYCLES"
        )
        print("=" * 60)

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
            ControlledOracleExecutor(
                mode="success"
            )
        )

        application[
            "execution_manager"
        ].oracle_executor = (
            fake_oracle
        )

        # ---------------------------------------------------------
        # Cycle 1: SUCCESS
        # ---------------------------------------------------------

        _, results_1 = execute_cycle(
            application,
            1,
            CYCLE_TIMES[0],
        )

        assert len(results_1) == 1

        assert (
            results_1[0]["status"]
            == "SUCCESS"
        )

        assert (
            results_1[0]["executed"]
            is True
        )

        assert (
            results_1[0]["duplicate"]
            is False
        )

        ready_repository = application[
            "ready_repository"
        ]

        assert (
            ready_repository.get_by_id(
                TEST_JOB_ID
            )
            is None
        )

        # ---------------------------------------------------------
        # Cycles 2-4:
        #
        # The same scheduled occurrence remains inside its
        # RUN_BY window.
        #
        # The execution manager must not execute it again.
        # ---------------------------------------------------------

        for index, cycle_time in enumerate(
            CYCLE_TIMES[1:],
            start=2,
        ):
            _, results = execute_cycle(
                application,
                index,
                cycle_time,
            )

            assert len(results) == 1, (
                "The execution manager should return "
                "a duplicate-skip result for the READY "
                "occurrence rather than silently returning "
                "an empty result."
            )

            assert results[0]["executed"] is False, (
                "A successful scheduled occurrence "
                "must not execute again on a "
                "later scheduler cycle."
            )

            assert results[0]["duplicate"] is True, (
                "The later scheduler cycle should be "
                "identified as a duplicate."
            )

            assert results[0]["reason"] == (
                "Scheduled occurrence already completed successfully."
            ), (
                "Unexpected duplicate-skip reason."
            )

            assert (
                len(fake_oracle.calls)
                == 1
            ), (
                "Oracle should have been called "
                "exactly once after SUCCESS."
            )

        history = get_test_history(
            application
        )

        assert len(history) == 1

        assert (
            history[0].status
            == "SUCCESS"
        )

        assert (
            history[0].attempt_no
            == 1
        )

        assert (
            history[0].report_date
            == TEST_REPORT_DATE
        )

        print()
        print(
            "SCENARIO 1 PASSED:"
        )
        print(
            "One SUCCESS, followed by "
            "three duplicate-protected cycles."
        )
        print(
            "FAKE ORACLE CALL COUNT:",
            len(fake_oracle.calls),
        )

    finally:
        if application is not None:
            try:
                close_application(
                    application
                )
            except Exception:
                pass


def run_failure_retry_scenario():
    """
    Scenario 2:

        Cycle 1 -> FAILED
        Cycle 2 -> RETRY SUCCESS
        Cycle 3 -> duplicate protection
        Cycle 4 -> duplicate protection
    """

    application = None

    try:
        print()
        print("=" * 60)
        print(
            "SCENARIO 2 - FAILURE, RETRY, "
            "THEN DUPLICATE PROTECTION"
        )
        print("=" * 60)

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
            ControlledOracleExecutor(
                mode="fail_once"
            )
        )

        application[
            "execution_manager"
        ].oracle_executor = (
            fake_oracle
        )

        ready_repository = application[
            "ready_repository"
        ]

        # ---------------------------------------------------------
        # Cycle 1: FAILED
        # ---------------------------------------------------------

        _, results_1 = execute_cycle(
            application,
            1,
            CYCLE_TIMES[0],
        )

        assert len(results_1) == 1

        assert (
            results_1[0]["status"]
            == "FAILED"
        )

        assert (
            results_1[0]["executed"]
            is True
        )

        assert (
            results_1[0]["duplicate"]
            is False
        )

        ready_after_failure = (
            ready_repository.get_by_id(
                TEST_JOB_ID
            )
        )

        assert (
            ready_after_failure
            is not None
        ), (
            "FAILED scheduled execution "
            "must remain READY."
        )

        # ---------------------------------------------------------
        # Cycle 2: RETRY SUCCESS
        # ---------------------------------------------------------

        _, results_2 = execute_cycle(
            application,
            2,
            CYCLE_TIMES[1],
        )

        assert len(results_2) == 1

        assert (
            results_2[0]["status"]
            == "SUCCESS"
        )

        assert (
            results_2[0]["executed"]
            is True
        )

        assert (
            results_2[0]["duplicate"]
            is False
        )

        assert (
            ready_repository.get_by_id(
                TEST_JOB_ID
            )
            is None
        )

        assert (
            len(fake_oracle.calls)
            == 2
        )

        # ---------------------------------------------------------
        # Cycles 3-4: duplicate protection
        # ---------------------------------------------------------

        for index, cycle_time in enumerate(
            CYCLE_TIMES[2:],
            start=3,
        ):
            _, results = execute_cycle(
                application,
                index,
                cycle_time,
            )

            assert len(results) == 1, (
                "The execution manager should return "
                "a duplicate-skip result after the "
                "retry succeeds."
            )

            assert results[0]["executed"] is False, (
                "After retry SUCCESS, later cycles "
                "must not execute the same occurrence again."
            )

            assert results[0]["duplicate"] is True, (
                "After retry SUCCESS, the later cycle "
                "should be identified as a duplicate."
            )

            assert results[0]["reason"] == (
                "Scheduled occurrence already completed successfully."
            ), (
                "Unexpected duplicate-skip reason after retry."
            )

            assert (
                len(fake_oracle.calls)
                == 2
            )

        history = get_test_history(
            application
        )

        assert len(history) == 2

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

        print()
        print(
            "SCENARIO 2 PASSED:"
        )
        print(
            "FAILED -> RETRY SUCCESS -> "
            "duplicate-protected cycles."
        )
        print(
            "FAKE ORACLE CALL COUNT:",
            len(fake_oracle.calls),
        )

    finally:
        if application is not None:
            try:
                close_application(
                    application
                )
            except Exception:
                pass


def main():
    backup_files()

    try:
        print("=" * 60)
        print(
            "REPEATED 3-MINUTE DUPLICATE "
            "PROTECTION TEST"
        )
        print("=" * 60)

        prepare_unique_datemast()

        run_success_scenario()

        run_failure_retry_scenario()

        print()
        print("=" * 60)
        print("FINAL VALIDATION")
        print("=" * 60)

        print(
            "PASS: Successful occurrence executes "
            "only once."
        )

        print(
            "PASS: Later 3-minute cycles do not "
            "re-execute SUCCESS."
        )

        print(
            "PASS: FAILED occurrence remains "
            "eligible for retry."
        )

        print(
            "PASS: Retry creates attempt number 2."
        )

        print(
            "PASS: Retry SUCCESS closes the "
            "scheduled occurrence."
        )

        print(
            "PASS: Later cycles do not re-execute "
            "a successful retry."
        )

        print(
            "PASS: Report date remains the same "
            "throughout the occurrence lifecycle."
        )

        print()
        print(
            "REPEATED 3-MINUTE DUPLICATE "
            "PROTECTION TEST PASSED."
        )

    finally:
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
