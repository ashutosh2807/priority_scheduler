from datetime import date, datetime

from main import create_application, close_application
from models.ready_job import ReadyJob
from execution.oracle_executor import ExecutionResult


class ControlledOracleExecutor:

    def __init__(self):
        self.calls = 0

    def execute(
        self,
        procedure_name,
        report_date,
    ):
        self.calls += 1

        print()
        print(
            f"FAKE ORACLE CALL #{self.calls}"
        )

        print(
            "procedure:",
            procedure_name,
        )

        print(
            "report_date:",
            report_date,
        )

        # -------------------------------------------------
        # FIRST CALL -> FAILURE
        # -------------------------------------------------

        if self.calls == 1:

            return ExecutionResult(
                success=False,
                procedure_name=procedure_name,
                report_date=report_date,
                count=None,
                error="Controlled test failure",
                error_type="ControlledTestError",
                started_at=datetime.now().isoformat(
                    timespec="seconds"
                ),
                finished_at=datetime.now().isoformat(
                    timespec="seconds"
                ),
                duration_seconds=0.1,
            )

        # -------------------------------------------------
        # SECOND CALL -> SUCCESS
        # -------------------------------------------------

        return ExecutionResult(
            success=True,
            procedure_name=procedure_name,
            report_date=report_date,
            count=999,
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


def main():

    application = create_application()

    try:

        manager = application[
            "execution_manager"
        ]

        ready_repository = application[
            "ready_repository"
        ]

        execution_repository = application[
            "execution_repository"
        ]

        # -------------------------------------------------
        # Use a NEW test occurrence.
        #
        # 2026-09-11 was not used in the previous test.
        # -------------------------------------------------

        test_job_id = 2
        test_report_date = date(
            2026,
            9,
            11,
        )

        # -------------------------------------------------
        # Remove only the READY record for this job.
        # -------------------------------------------------

        ready_repository.delete(
            test_job_id
        )

        # -------------------------------------------------
        # Remove only this exact test occurrence from
        # execution history, if it already exists.
        # -------------------------------------------------

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
                test_job_id,
                test_report_date.isoformat(),
            ),
        )

        connection.commit()

        # -------------------------------------------------
        # Create controlled READY record.
        # -------------------------------------------------

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        ready_job = ReadyJob(
            job_id=test_job_id,
            job_name="FTD_EXTRACT",
            report_date=test_report_date,
            ready_since=now,
            time_priority=0,
            date_priority=0,
            job_priority=0,
            priority_key=(
                0,
                0,
                0,
                test_job_id,
            ),
            from_time="10:00",
            to_time="11:00",
            time_state="ACTIVE",
            updated_at=now,
        )

        ready_repository.save(
            ready_job
        )

        # -------------------------------------------------
        # Install controlled executor.
        #
        # IMPORTANT:
        # No real Oracle execution will happen.
        # -------------------------------------------------

        fake_oracle = (
            ControlledOracleExecutor()
        )

        manager.oracle_executor = fake_oracle

        # -------------------------------------------------
        # Build queue from READY.
        # -------------------------------------------------

        manager.rebuild_queue()

        print()
        print("=" * 50)
        print("BEFORE FIRST EXECUTION")
        print("=" * 50)

        print(
            "READY:",
            ready_repository.get_by_id(
                test_job_id
            ),
        )

        print(
            "QUEUE SIZE:",
            manager.priority_queue.size(),
        )

        # -------------------------------------------------
        # FIRST EXECUTION
        # Expected:
        #
        # attempt 1
        # FAILED
        # READY remains
        # -------------------------------------------------

        print()
        print("=" * 50)
        print("FIRST EXECUTION")
        print("=" * 50)

        first_result = (
            manager.execute_next()
        )

        print(
            "RESULT:",
            first_result,
        )

        print()
        print(
            "READY AFTER FAILURE:"
        )

        print(
            ready_repository.get_by_id(
                test_job_id
            )
        )

        print()
        print(
            "EXECUTION HISTORY:"
        )

        for row in (
            execution_repository.get_all()
        ):

            if (
                row.job_id
                == test_job_id
                and row.report_date
                == test_report_date
            ):
                print(row)

        # -------------------------------------------------
        # SECOND EXECUTION
        #
        # Rebuild queue from persistent READY.
        #
        # Expected:
        #
        # attempt 2
        # SUCCESS
        # READY removed
        # -------------------------------------------------

        manager.rebuild_queue()

        print()
        print("=" * 50)
        print("SECOND EXECUTION / RETRY")
        print("=" * 50)

        second_result = (
            manager.execute_next()
        )

        print(
            "RESULT:",
            second_result,
        )

        print()
        print(
            "READY AFTER SUCCESS:"
        )

        print(
            ready_repository.get_by_id(
                test_job_id
            )
        )

        print()
        print(
            "EXECUTION HISTORY:"
        )

        for row in (
            execution_repository.get_all()
        ):

            if (
                row.job_id
                == test_job_id
                and row.report_date
                == test_report_date
            ):
                print(row)

        print()
        print(
            "FAKE ORACLE CALL COUNT:",
            fake_oracle.calls,
        )

    finally:

        close_application(
            application
        )


if __name__ == "__main__":
    main()