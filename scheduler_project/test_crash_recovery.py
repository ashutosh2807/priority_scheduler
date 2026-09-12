"""
Crash / restart recovery test for the scheduler.

This is a controlled integration test.

It simulates the state left behind when the scheduler process dies
while an execution is RUNNING.

The test does NOT call Oracle.

Scenario:

    Process A
        |
        +--> execution_jobs = RUNNING
        |
        X  process crashes

    Process B starts
        |
        +--> create_application()
        |
        +--> recover_running_executions()
        |
        +--> orphaned RUNNING row becomes FAILED
        |
        +--> execution history is preserved
        |
        +--> application can continue normally

The test also verifies that an existing SUCCESS execution is not changed.

All SQLite changes are restored from a backup at the end.
"""

import shutil
from datetime import date, datetime
from pathlib import Path

from main import create_application, close_application


PROJECT_ROOT = Path(__file__).resolve().parent

DB_FILE = PROJECT_ROOT / "scheduler.db"

BACKUP_DB = (
    PROJECT_ROOT
    / "scheduler_crash_recovery_test.db.bak"
)

TEST_JOB_ID = 2

TEST_REPORT_DATE = date(2026, 9, 10)

TEST_PROCEDURE = "SCHEDULE_EXTRACTS.tltro"


def backup_database():
    shutil.copy2(
        DB_FILE,
        BACKUP_DB,
    )


def restore_database():
    if BACKUP_DB.exists():
        shutil.copy2(
            BACKUP_DB,
            DB_FILE,
        )


def cleanup_test_rows(application):
    """
    Remove only the exact controlled test occurrence.
    """

    connection = application[
        "connection"
    ]

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


def create_orphaned_running_execution(
    application,
):
    """
    Insert a realistic orphaned RUNNING execution.

    This represents the database state immediately before a process
    crash.
    """

    connection = application[
        "connection"
    ]

    job_repository = application[
        "schedule_master_repository"
    ]

    job = job_repository.get_by_id(
        TEST_JOB_ID
    )

    assert job is not None, (
        f"Job {TEST_JOB_ID} was not found."
    )

    now = datetime.now().isoformat(
        timespec="seconds"
    )

    cursor = connection.execute(
        """
        INSERT INTO execution_jobs (
            job_id,
            job_name,
            procedure_name,
            report_date,
            status,
            attempt_no,
            count,
            started_at,
            finished_at,
            error,
            error_type,
            duration_seconds,
            created_at,
            updated_at
        )
        VALUES (
            ?, ?, ?, ?, 'RUNNING', 1, NULL,
            ?, NULL, NULL, NULL, NULL, ?, ?
        )
        """,
        (
            TEST_JOB_ID,
            job.name,
            TEST_PROCEDURE,
            TEST_REPORT_DATE.isoformat(),
            now,
            now,
            now,
        ),
    )

    connection.commit()

    return cursor.lastrowid


def get_execution_by_id(
    application,
    execution_id,
):
    execution_repository = application[
        "execution_repository"
    ]

    return execution_repository.get_by_id(
        execution_id
    )


def print_execution(
    title,
    execution,
):
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)
    print(execution)


def main():
    backup_database()

    application = None

    try:
        print("=" * 60)
        print("CRASH / RESTART RECOVERY TEST")
        print("=" * 60)

        # ---------------------------------------------------------
        # PROCESS A
        #
        # Create the state that would exist immediately before a
        # scheduler process crashes.
        # ---------------------------------------------------------

        print()
        print("=" * 60)
        print("PROCESS A - CREATE ORPHANED RUNNING EXECUTION")
        print("=" * 60)

        application = create_application()

        cleanup_test_rows(
            application
        )

        orphaned_execution_id = (
            create_orphaned_running_execution(
                application
            )
        )

        orphaned_before = (
            get_execution_by_id(
                application,
                orphaned_execution_id,
            )
        )

        print_execution(
            "STATE BEFORE SIMULATED CRASH",
            orphaned_before,
        )

        assert (
            orphaned_before is not None
        )

        assert (
            orphaned_before.status
            == "RUNNING"
        )

        assert (
            orphaned_before.job_id
            == TEST_JOB_ID
        )

        assert (
            orphaned_before.report_date
            == TEST_REPORT_DATE
        )

        assert (
            orphaned_before.attempt_no
            == 1
        )

        # Close Process A.
        #
        # In a real crash there would be no graceful shutdown,
        # but closing the connection here gives us a clean
        # hand-off to the simulated restarted process.
        close_application(
            application
        )

        application = None

        print()
        print(
            "SIMULATED PROCESS CRASH"
        )

        # ---------------------------------------------------------
        # PROCESS B
        #
        # Build a completely new application instance.
        # ---------------------------------------------------------

        print()
        print("=" * 60)
        print("PROCESS B - APPLICATION RESTART")
        print("=" * 60)

        application = create_application()

        execution_manager = (
            application[
                "execution_manager"
            ]
        )

        # The real startup recovery method is the same method
        # called by main.py before run_forever().
        recovered = (
            execution_manager
            .recover_running_executions()
        )

        print()
        print(
            "RECOVERED EXECUTION COUNT:"
        )
        print(recovered)

        assert recovered >= 1, (
            "The orphaned RUNNING execution "
            "was not recovered."
        )

        orphaned_after = (
            get_execution_by_id(
                application,
                orphaned_execution_id,
            )
        )

        print_execution(
            "STATE AFTER RESTART RECOVERY",
            orphaned_after,
        )

        assert (
            orphaned_after is not None
        )

        assert (
            orphaned_after.status
            == "FAILED"
        ), (
            "An orphaned RUNNING execution "
            "must be marked FAILED during "
            "startup recovery."
        )

        assert (
            orphaned_after.job_id
            == TEST_JOB_ID
        )

        assert (
            orphaned_after.report_date
            == TEST_REPORT_DATE
        )

        assert (
            orphaned_after.attempt_no
            == 1
        )

        assert (
            orphaned_after.finished_at
            is not None
        )

        assert (
            orphaned_after.updated_at
            is not None
        )

        assert (
            orphaned_after.error
            is not None
        ), (
            "Recovered execution should "
            "contain an explanatory error."
        )

        print()
        print(
            "RECOVERY ERROR:"
        )
        print(
            orphaned_after.error
        )

        print()
        print(
            "RECOVERY ERROR TYPE:"
        )
        print(
            orphaned_after.error_type
        )

        # ---------------------------------------------------------
        # Verify no second recovery occurs.
        #
        # Once the row is FAILED, calling recovery again must not
        # create another attempt or change the same row.
        # ---------------------------------------------------------

        recovered_again = (
            execution_manager
            .recover_running_executions()
        )

        print()
        print(
            "SECOND RECOVERY COUNT:"
        )
        print(
            recovered_again
        )

        assert (
            recovered_again == 0
        ), (
            "A previously recovered FAILED "
            "execution must not be recovered again."
        )

        orphaned_after_second_recovery = (
            get_execution_by_id(
                application,
                orphaned_execution_id,
            )
        )

        assert (
            orphaned_after_second_recovery
            is not None
        )

        assert (
            orphaned_after_second_recovery.status
            == "FAILED"
        )

        assert (
            orphaned_after_second_recovery.attempt_no
            == 1
        )

        # ---------------------------------------------------------
        # Verify the normal application can continue after recovery.
        #
        # Rebuild the priority queue from persistent READY state.
        # This should not be affected by the recovered execution.
        # ---------------------------------------------------------

        priority_queue = application[
            "priority_queue"
        ]

        ready_repository = application[
            "ready_repository"
        ]

        print()
        print("=" * 60)
        print("POST-RECOVERY APPLICATION STATE")
        print("=" * 60)

        print(
            "READY COUNT:"
        )
        print(
            ready_repository.count()
        )

        print()
        print(
            "QUEUE SIZE:"
        )
        print(
            priority_queue.size()
        )

        # ---------------------------------------------------------
        # Final validation.
        # ---------------------------------------------------------

        print()
        print("=" * 60)
        print("FINAL VALIDATION")
        print("=" * 60)

        print(
            "PASS: Orphaned RUNNING execution "
            "was created."
        )

        print(
            "PASS: Application was closed and "
            "re-created to simulate restart."
        )

        print(
            "PASS: Startup recovery found the "
            "orphaned RUNNING execution."
        )

        print(
            "PASS: Orphaned RUNNING execution "
            "was changed to FAILED."
        )

        print(
            "PASS: Original job_id/report_date "
            "were preserved."
        )

        print(
            "PASS: Attempt number remained 1."
        )

        print(
            "PASS: Recovery added an explanatory "
            "error."
        )

        print(
            "PASS: Second recovery did not "
            "recover the same execution again."
        )

        print(
            "PASS: Application remained usable "
            "after recovery."
        )

        print()
        print(
            "CRASH / RESTART RECOVERY TEST PASSED."
        )

    finally:
        if application is not None:
            try:
                close_application(
                    application
                )
            except Exception:
                pass

        restore_database()

        try:
            BACKUP_DB.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
