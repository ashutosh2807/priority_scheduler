import unittest
from dataclasses import dataclass
from datetime import date, datetime
from types import SimpleNamespace


class InMemoryStagingRepository:
    def __init__(self):
        self.rows = {}

    def save(self, job):
        self.rows[job.job_id] = job

    def get_by_id(self, job_id):
        return self.rows.get(job_id)

    def delete(self, job_id):
        self.rows.pop(job_id, None)

    def get_all(self):
        return list(self.rows.values())


class InMemoryReadyRepository:
    def __init__(self):
        self.rows = {}

    def save(self, job):
        self.rows[job.job_id] = job

    def get_by_id(self, job_id):
        return self.rows.get(job_id)

    def delete(self, job_id):
        self.rows.pop(job_id, None)

    def get_all(self):
        return list(self.rows.values())


class InMemoryControlRepository:
    def __init__(self, control=None):
        self.control = control or {
            "control_status": "ACTIVE",
            "manual_run": 1,
            "confirmation": 0,
        }

    def get(self, job_id):
        return dict(self.control)

    def clear_manual_run(self, job_id):
        self.control["manual_run"] = 0


class InMemoryScheduleMasterRepository:
    def __init__(self, job):
        self.job = job

    def get_by_id(self, job_id):
        if job_id == self.job.id:
            return self.job
        return None


@dataclass
class ExecutionRecord:
    id: int
    job_id: int
    job_name: str
    procedure_name: str
    report_date: date
    status: str
    attempt_no: int = 1


class FakeExecutionRepository:
    """
    Minimal execution repository implementing the methods used by
    ExecutionManager on a successful manual execution path.
    """

    def __init__(self):
        self.records = {}
        self.calls = []
        self.next_id = 1

    def has_success(self, job_id, report_date):
        return any(
            record.job_id == job_id
            and record.report_date == report_date
            and record.status == "SUCCESS"
            for record in self.records.values()
        )

    def has_running(self, job_id, report_date):
        return any(
            record.job_id == job_id
            and record.report_date == report_date
            and record.status == "RUNNING"
            for record in self.records.values()
        )

    def start_execution(
        self,
        job_id,
        job_name,
        procedure_name,
        report_date,
    ):
        execution_id = self.next_id
        self.next_id += 1

        record = ExecutionRecord(
            id=execution_id,
            job_id=job_id,
            job_name=job_name,
            procedure_name=procedure_name,
            report_date=report_date,
            status="RUNNING",
            attempt_no=1,
        )

        self.records[execution_id] = record

        self.calls.append(
            {
                "operation": "start_execution",
                "job_id": job_id,
                "job_name": job_name,
                "procedure_name": procedure_name,
                "report_date": report_date,
            }
        )

        return execution_id

    def get_by_id(self, execution_id):
        return self.records.get(execution_id)

    def mark_success(
        self,
        execution_id,
        count=None,
        duration_seconds=None,
    ):
        record = self.records[execution_id]
        record.status = "SUCCESS"

        self.calls.append(
            {
                "operation": "mark_success",
                "execution_id": execution_id,
                "count": count,
                "duration_seconds": duration_seconds,
            }
        )

    def mark_failed(
        self,
        execution_id,
        error=None,
        error_type=None,
        duration_seconds=None,
    ):
        record = self.records[execution_id]
        record.status = "FAILED"

        self.calls.append(
            {
                "operation": "mark_failed",
                "execution_id": execution_id,
                "error": error,
                "error_type": error_type,
                "duration_seconds": duration_seconds,
            }
        )

    def get_latest(self, job_id, report_date):
        matching = [
            record
            for record in self.records.values()
            if record.job_id == job_id
            and record.report_date == report_date
        ]

        if not matching:
            return None

        return max(
            matching,
            key=lambda record: record.attempt_no,
        )


class FakeOracleExecutor:
    def __init__(self):
        self.calls = []

    def execute(
        self,
        procedure_name,
        report_date,
        **kwargs,
    ):
        self.calls.append(
            {
                "procedure_name": procedure_name,
                "report_date": report_date,
            }
        )

        return SimpleNamespace(
            success=True,
            procedure_name=procedure_name,
            report_date=report_date,
            count=123,
            error=None,
            error_type=None,
            duration_seconds=0.25,
        )


class FakePriorityCalculator:
    @dataclass
    class Priority:
        date_priority: int
        time_priority: int
        job_priority: int
        priority_key: tuple
        time_state: str = None
        from_time: object = None
        to_time: object = None

    def calculate(self, job, report_date, current_datetime):
        return self.Priority(
            date_priority=1,
            time_priority=2,
            job_priority=3,
            priority_key=(1, 2, 3, int(job.id)),
            time_state="READY",
            from_time=None,
            to_time=None,
        )


class TestStagingReadyExecutionPipeline(unittest.TestCase):

    def setUp(self):
        from scheduler.staging import StagingManager
        from scheduler_queue.priority_queue import PriorityQueue
        from execution.execution_manager import ExecutionManager

        self.job = SimpleNamespace(
            id=101,
            name="PIPELINE_TEST_JOB",
            package_name="TEST_PACKAGE.TEST_PROCEDURE",
            same_day=0,
            time_flag=0,
            confirmation_needed=0,
            run_config=None,
        )

        self.staging_repository = InMemoryStagingRepository()
        self.ready_repository = InMemoryReadyRepository()
        self.priority_calculator = FakePriorityCalculator()

        self.staging_manager = StagingManager(
            staging_repository=self.staging_repository,
            ready_repository=self.ready_repository,
            priority_calculator=self.priority_calculator,
        )

        self.priority_queue = PriorityQueue()
        self.execution_repository = FakeExecutionRepository()
        self.control_repository = InMemoryControlRepository()
        self.schedule_master_repository = (
            InMemoryScheduleMasterRepository(self.job)
        )
        self.oracle_executor = FakeOracleExecutor()

        self.execution_manager = ExecutionManager(
            ready_repository=self.ready_repository,
            priority_queue=self.priority_queue,
            oracle_executor=self.oracle_executor,
            execution_repository=self.execution_repository,
            job_control_repository=self.control_repository,
            schedule_master_repository=self.schedule_master_repository,
            schedule_extg_repository=None,
        )

    @staticmethod
    def _staging_result():
        return SimpleNamespace(
            state="STAGING",
            eligible=False,

            occurrence_date=date(2026, 9, 15),
            execution_date=date(2026, 9, 17),
            t_date=date(2026, 9, 15),
            report_date=date(2026, 9, 15),
            target_date=date(2026, 9, 18),

            waiting_for="DATEMAST",
            reason="Waiting for T+3 threshold.",

            confirmation_required=False,
            confirmation_status=None,

            from_time=None,
            to_time=None,
            time_window_state=None,
        )

    @staticmethod
    def _ready_result():
        return SimpleNamespace(
            state="READY",
            eligible=True,

            occurrence_date=date(2026, 9, 15),
            execution_date=date(2026, 9, 17),
            t_date=date(2026, 9, 15),
            report_date=date(2026, 9, 15),
            target_date=date(2026, 9, 18),

            waiting_for=None,
            reason="All eligibility conditions satisfied.",

            confirmation_required=False,
            confirmation_status=None,

            from_time=None,
            to_time=None,
            time_window_state="READY",
        )

    def test_staging_to_ready_preserves_all_five_dates(self):
        cycle_time_1 = datetime(2026, 9, 17, 10, 0)

        staging_job = self.staging_manager.synchronize(
            job=self.job,
            result=self._staging_result(),
            current_datetime=cycle_time_1,
        )

        self.assertIsNotNone(staging_job)
        self.assertEqual(
            staging_job.job_id,
            self.job.id,
        )

        self.assertEqual(
            staging_job.occurrence_date,
            "2026-09-15",
        )
        self.assertEqual(
            staging_job.execution_date,
            "2026-09-17",
        )
        self.assertEqual(
            staging_job.t_date,
            "2026-09-15",
        )
        self.assertEqual(
            staging_job.report_date,
            "2026-09-15",
        )
        self.assertEqual(
            staging_job.target_date,
            "2026-09-18",
        )

        cycle_time_2 = datetime(2026, 9, 18, 10, 0)

        ready_job = self.staging_manager.synchronize(
            job=self.job,
            result=self._ready_result(),
            current_datetime=cycle_time_2,
        )

        self.assertIsNotNone(ready_job)

        self.assertIsNone(
            self.staging_repository.get_by_id(
                self.job.id
            )
        )

        stored_ready = self.ready_repository.get_by_id(
            self.job.id
        )

        self.assertIsNotNone(stored_ready)

        self.assertEqual(
            stored_ready.occurrence_date,
            "2026-09-15",
        )
        self.assertEqual(
            stored_ready.execution_date,
            "2026-09-17",
        )
        self.assertEqual(
            stored_ready.t_date,
            "2026-09-15",
        )
        self.assertEqual(
            stored_ready.report_date,
            "2026-09-15",
        )
        self.assertEqual(
            stored_ready.target_date,
            "2026-09-18",
        )

    def test_ready_record_enters_priority_queue_with_report_date(self):
        self.staging_manager.synchronize(
            job=self.job,
            result=self._ready_result(),
            current_datetime=datetime(
                2026, 9, 18, 10, 0
            ),
        )

        stored_ready = self.ready_repository.get_by_id(
            self.job.id
        )

        self.assertIsNotNone(stored_ready)
        self.assertEqual(
            stored_ready.report_date,
            "2026-09-15",
        )

        self.priority_queue.rebuild(
            self.ready_repository.get_all()
        )

        self.assertEqual(
            self.priority_queue.size(),
            1,
        )

        queue_item = self.priority_queue.peek()

        self.assertIsNotNone(queue_item)
        self.assertEqual(
            queue_item.job_id,
            self.job.id,
        )
        self.assertEqual(
            queue_item.report_date,
            "2026-09-15",
        )

    def test_full_ready_to_execution_flow_passes_report_date_to_oracle(self):
        self.staging_manager.synchronize(
            job=self.job,
            result=self._ready_result(),
            current_datetime=datetime(
                2026, 9, 18, 10, 0
            ),
        )

        self.priority_queue.rebuild(
            self.ready_repository.get_all()
        )

        self.assertEqual(
            self.priority_queue.size(),
            1,
        )

        results = self.execution_manager.execute_available()

        self.assertEqual(
            len(results),
            1,
        )

        execution_result = results[0]

        self.assertEqual(
            execution_result["status"],
            self.execution_manager.STATUS_SUCCESS,
        )

        self.assertTrue(
            execution_result["executed"]
        )

        self.assertEqual(
            execution_result["report_date"],
            date(2026, 9, 15),
        )

        self.assertEqual(
            execution_result["procedure_name"],
            "TEST_PACKAGE.TEST_PROCEDURE",
        )

        self.assertEqual(
            len(self.oracle_executor.calls),
            1,
        )

        oracle_call = self.oracle_executor.calls[0]

        self.assertEqual(
            oracle_call["report_date"],
            date(2026, 9, 15),
        )

        self.assertNotEqual(
            oracle_call["report_date"],
            date(2026, 9, 17),
        )

        # Successful execution removes the persistent READY record.
        self.assertIsNone(
            self.ready_repository.get_by_id(
                self.job.id
            )
        )

        # Queue is also consumed.
        self.assertTrue(
            self.priority_queue.is_empty()
        )

        # SQLite/execution tracking contract is represented by the fake
        # execution repository in this test.
        self.assertEqual(
            len(self.execution_repository.records),
            1,
        )

        execution_record = next(
            iter(self.execution_repository.records.values())
        )

        self.assertEqual(
            execution_record.status,
            "SUCCESS",
        )
        self.assertEqual(
            execution_record.report_date,
            date(2026, 9, 15),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
