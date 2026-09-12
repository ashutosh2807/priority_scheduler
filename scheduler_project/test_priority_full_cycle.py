"""
Controlled full-cycle priority test.

This test validates:

    Scheduler
        -> READY
        -> PriorityCalculator
        -> PriorityQueue
        -> ExecutionManager
        -> controlled Oracle executor

It uses MANUAL RUN requests so that the test does not depend on
the current real schedule occurrence.

Three jobs are given different RUN_BY times through job-specific
datetime overrides. All three are made READY in the same scheduler
cycle.

Expected execution order:

    Job 1 -> Job 2 -> Job 4

because all three have the same report date and active time window,
so the deterministic job_id tie-breaker should order them 1, 2, 4.

IMPORTANT:
    The test backs up scheduler.db and Schedule_extg.json and restores
    both files after the test, so the test should not leave its
    temporary scheduler state behind.
"""

import shutil
from datetime import datetime
from pathlib import Path

from main import create_application, close_application
from execution.oracle_executor import ExecutionResult


PROJECT_ROOT = Path(__file__).resolve().parent

DB_FILE = PROJECT_ROOT / "scheduler.db"
EXTG_FILE = PROJECT_ROOT / "file_repository" / "Schedule_extg.json"

BACKUP_DB = PROJECT_ROOT / "scheduler_priority_test_scheduler.db.bak"
BACKUP_EXTG = PROJECT_ROOT / "scheduler_priority_test_extg.json.bak"


TEST_JOB_IDS = [1, 2, 4]

# The exact cycle datetime is not important because every test job
# receives its own override below.
CYCLE_DATETIME = datetime(2026, 9, 10, 12, 0, 0)

OVERRIDES = {
    1: "2026-09-10T06:15:00",
    2: "2026-09-10T10:30:00",
    4: "2026-09-10T18:00:00",
}


class ControlledOracleExecutor:
    """
    Fake Oracle executor.

    It never connects to Oracle.

    Every execution succeeds and records the execution order.
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

        return ExecutionResult(
            success=True,
            procedure_name=procedure_name,
            report_date=report_date,
            count=1000 + call_number,
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


def restore_files():
    if BACKUP_DB.exists():
        shutil.copy2(BACKUP_DB, DB_FILE)

    if BACKUP_EXTG.exists():
        shutil.copy2(BACKUP_EXTG, EXTG_FILE)


def clear_test_state(application):
    ready_repository = application["ready_repository"]
    staging_repository = application["staging_repository"]
    job_control_repository = application["job_control_repository"]

    connection = application["connection"]

    # Remove persistent scheduler state for the selected jobs.
    for job_id in TEST_JOB_IDS:
        ready_repository.delete(job_id)
        staging_repository.delete(job_id)

        # Clear temporary controls from previous runs.
        job_control_repository.reset(job_id)

    # The test is about queue ordering, so remove old execution
    # history for the selected jobs. The database itself is restored
    # after the test.
    placeholders = ",".join(
        "?" for _ in TEST_JOB_IDS
    )

    connection.execute(
        f"""
        DELETE FROM execution_jobs
        WHERE job_id IN ({placeholders})
        """,
        TEST_JOB_IDS,
    )

    connection.commit()


def main():
    backup_files()

    application = None

    try:
        print("=" * 60)
        print("FULL SCHEDULER + PRIORITY + EXECUTION TEST")
        print("=" * 60)

        application = create_application()

        clear_test_state(application)

        controls = application["job_control_repository"]

        # ---------------------------------------------------------
        # Configure controlled manual runs.
        # ---------------------------------------------------------

        for job_id in TEST_JOB_IDS:
            controls.set_override_datetime(
                job_id,
                OVERRIDES[job_id],
            )

            controls.request_manual_run(
                job_id
            )

        # ---------------------------------------------------------
        # Replace the real Oracle executor with the controlled
        # fake executor.
        # ---------------------------------------------------------

        fake_oracle = ControlledOracleExecutor()

        application[
            "execution_manager"
        ].oracle_executor = fake_oracle

        # ---------------------------------------------------------
        # Run the REAL scheduler cycle.
        #
        # The scheduler will:
        #
        #   1. read Schedule Master
        #   2. read Job Control
        #   3. evaluate the three manual requests
        #   4. create READY records
        #   5. calculate priority
        #   6. rebuild the priority heap
        #
        # Then ExecutionManager will execute the heap.
        # ---------------------------------------------------------

        scheduler_summary = (
            application["scheduler"].run_cycle(
                current_datetime=CYCLE_DATETIME
            )
        )

        print()
        print("=" * 60)
        print("SCHEDULER SUMMARY")
        print("=" * 60)
        print(scheduler_summary)

        ready_repository = application[
            "ready_repository"
        ]

        priority_queue = application[
            "priority_queue"
        ]

        print()
        print("=" * 60)
        print("READY STATE BEFORE EXECUTION")
        print("=" * 60)

        ready_jobs = ready_repository.get_all()

        for ready_job in ready_jobs:
            print(ready_job)

        print(f"READY COUNT: {len(ready_jobs)}")
        print(f"QUEUE SIZE: {priority_queue.size()}")

        print()
        print("=" * 60)
        print("QUEUE ORDER BEFORE EXECUTION")
        print("=" * 60)

        queue_items = priority_queue.get_all()

        for item in queue_items:
            print(item)

        # ---------------------------------------------------------
        # Execute everything currently READY.
        # ---------------------------------------------------------

        print()
        print("=" * 60)
        print("EXECUTION")
        print("=" * 60)

        execution_results = (
            application[
                "execution_manager"
            ].execute_available()
        )

        for result in execution_results:
            print(result)

        # ---------------------------------------------------------
        # Show actual execution order.
        # ---------------------------------------------------------

        execution_order = [
            call["procedure_name"]
            for call in fake_oracle.calls
        ]

        job_order = []

        for result in execution_results:
            if result.get("executed"):
                job_order.append(
                    result.get("job_id")
                )

        print()
        print("=" * 60)
        print("RESULT")
        print("=" * 60)

        print(
            "EXECUTION JOB ORDER:",
            job_order,
        )

        print(
            "FAKE ORACLE CALL COUNT:",
            len(fake_oracle.calls),
        )

        remaining_ready = ready_repository.get_all()

        print(
            "REMAINING READY COUNT:",
            len(remaining_ready),
        )

        # ---------------------------------------------------------
        # Assertions.
        # ---------------------------------------------------------

        expected_order = [1, 2, 4]

        assert len(ready_jobs) == 3, (
            "Expected exactly 3 READY jobs."
        )

        assert job_order == expected_order, (
            f"Expected execution order "
            f"{expected_order}, got {job_order}."
        )

        assert len(fake_oracle.calls) == 3, (
            "Expected exactly 3 fake Oracle calls."
        )

        assert len(remaining_ready) == 0, (
            "All successful jobs should be removed from READY."
        )

        print()
        print("PASS: Scheduler created all three READY jobs.")
        print("PASS: Priority queue ordered jobs as 1 -> 2 -> 4.")
        print("PASS: Execution manager followed queue order.")
        print("PASS: All three controlled executions succeeded.")
        print("PASS: READY is empty after successful execution.")
        print()
        print("FULL PRIORITY FLOW TEST PASSED.")

    finally:
        if application is not None:
            try:
                close_application(application)
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


if __name__ == "__main__":
    main()
