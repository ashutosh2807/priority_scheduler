import unittest
from datetime import date, datetime
from types import SimpleNamespace


class MutableDateMast:
    """
    Controlled DATEMAST source.

    The scheduler sees progressively newer report dates across cycles:
        cycle 1 -> through 16-Sep
        cycle 2 -> through 17-Sep
        cycle 3 -> through 18-Sep

    The occurrence itself remains 15-Sep.
    """

    def __init__(self):
        self.available_dates = {
            datetime(2026, 9, 16).date(): [
                date(2026, 9, 15),
                date(2026, 9, 16),
            ],
            datetime(2026, 9, 17).date(): [
                date(2026, 9, 15),
                date(2026, 9, 16),
                date(2026, 9, 17),
            ],
            datetime(2026, 9, 18).date(): [
                date(2026, 9, 15),
                date(2026, 9, 16),
                date(2026, 9, 17),
                date(2026, 9, 18),
            ],
        }
        self.current_cycle_date = None

    def set_cycle_date(self, cycle_date):
        self.current_cycle_date = cycle_date

    def _dates(self):
        return self.available_dates.get(
            self.current_cycle_date,
            [],
        )

    def get_latest_report_date(self):
        values = self._dates()
        return max(values) if values else None

    def has_report_date(self, report_date):
        return report_date in self._dates()

    def latest_is_at_least(self, target_date):
        latest = self.get_latest_report_date()
        return latest is not None and latest >= target_date

    def is_ready_for(self, target_date):
        return self.latest_is_at_least(target_date)

    def get_report_date_for(self, current_date):
        candidates = [
            value
            for value in self._dates()
            if value <= current_date
        ]
        return max(candidates) if candidates else None


class ControlledFrequencyEvaluator:
    """
    The job has DAILY scheduling semantics, but the test deliberately
    anchors the first occurrence at 15-Sep-2026 so the scheduler's
    persistence behavior can be tested deterministically.
    """

    def __init__(self):
        self.previous_occurrence_calls = []

    def get_previous_occurrence(
        self,
        job,
        current_date,
        include_current=True,
        reference_date=None,
    ):
        self.previous_occurrence_calls.append(
            (
                job.id,
                current_date,
                include_current,
                reference_date,
            )
        )

        # This is the scheduled occurrence used by this test.
        if current_date >= date(2026, 9, 15):
            return date(2026, 9, 15)

        return None

    def get_next_occurrence(
        self,
        job,
        current_date,
        reference_date=None,
    ):
        # Not used by the main scenario because it reaches READY before
        # a next-occurrence transition is required.
        return current_date.replace(
            day=current_date.day + 1
        ) if current_date.day < 28 else None

    def is_scheduled(
        self,
        job,
        current_date,
        reference_date=None,
    ):
        return current_date == date(2026, 9, 15)


class ControlledHolidayEvaluator:
    """
    16-Sep is a holiday.

    Therefore:
        occurrence = 15-Sep
        SAME_DAY=0
        execution_date = 17-Sep
    """

    def __init__(self):
        self.holidays = {
            date(2026, 9, 16),
        }

    def is_working_day(self, value):
        return (
            value.weekday() < 5
            and value not in self.holidays
        )

    def can_run_on_day(self, job, value):
        # SAME_DAY=0 must allow the occurrence to be a holiday;
        # execution is deferred to the next working day.
        return True


class ControlledPriorityCalculator:
    """
    Deterministic priority implementation sufficient to exercise the real
    Scheduler -> StagingManager -> PriorityQueue path.
    """

    def calculate(
        self,
        job,
        report_date,
        current_datetime,
    ):
        return SimpleNamespace(
            date_priority=0,
            time_priority=0,
            job_priority=int(job.id),
            priority_key=(0, 0, int(job.id)),
            time_state="READY",
            from_time=None,
            to_time=None,
        )


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

    def count(self):
        return len(self.rows)


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

    def count(self):
        return len(self.rows)


class InMemoryControlRepository:
    def __init__(self):
        self.ensure_calls = []

    def ensure_job(self, job_id):
        self.ensure_calls.append(job_id)

    def get(self, job_id):
        return {
            "control_status": "ACTIVE",
            "manual_run": 0,
        }


class InMemoryScheduleMasterRepository:
    def __init__(self, job):
        self.job = job

    def get_active(self):
        return [self.job]

    def get_by_id(self, job_id):
        if job_id == self.job.id:
            return self.job
        return None


class TestSchedulerOccurrencePersistence(unittest.TestCase):

    def setUp(self):
        from scheduler.eligibility import EligibilityEvaluator
        from scheduler.staging import StagingManager
        from scheduler_queue.priority_queue import PriorityQueue
        from scheduler.scheduler import Scheduler

        self.job = SimpleNamespace(
            id=501,
            name="OCCURRENCE_PERSISTENCE_TEST",
            package_name="TEST_PACKAGE.TEST_PROCEDURE",

            frequency="DAILY",
            same_day=0,
            margin=3,

            confirmation_required=0,
            confirmation_needed=0,
            time_flag=0,

            from_time=None,
            to_time=None,

            run_config={
                "RUNS_ON": [
                    "DAILY",
                ]
            },
        )

        self.staging_repository = (
            InMemoryStagingRepository()
        )

        self.ready_repository = (
            InMemoryReadyRepository()
        )

        self.control_repository = (
            InMemoryControlRepository()
        )

        self.schedule_master_repository = (
            InMemoryScheduleMasterRepository(
                self.job
            )
        )

        self.frequency_evaluator = (
            ControlledFrequencyEvaluator()
        )

        self.holiday_evaluator = (
            ControlledHolidayEvaluator()
        )

        self.eligibility_evaluator = (
            EligibilityEvaluator(
                frequency_evaluator=(
                    self.frequency_evaluator
                ),
                holiday_evaluator=(
                    self.holiday_evaluator
                ),
            )
        )

        self.priority_calculator = (
            ControlledPriorityCalculator()
        )

        self.priority_queue = PriorityQueue()

        self.staging_manager = StagingManager(
            staging_repository=(
                self.staging_repository
            ),
            ready_repository=(
                self.ready_repository
            ),
            priority_calculator=(
                self.priority_calculator
            ),
        )

        self.datemast = MutableDateMast()

        self.scheduler = Scheduler(
            schedule_master_repository=(
                self.schedule_master_repository
            ),
            job_control_repository=(
                self.control_repository
            ),
            eligibility_evaluator=(
                self.eligibility_evaluator
            ),
            staging_manager=(
                self.staging_manager
            ),
            priority_calculator=(
                self.priority_calculator
            ),
            ready_repository=(
                self.ready_repository
            ),
            priority_queue=(
                self.priority_queue
            ),
            datemast=self.datemast,
            frequency_evaluator=(
                self.frequency_evaluator
            ),
        )

    @staticmethod
    def assert_date_context(
        testcase,
        job,
        occurrence_date,
        execution_date,
        t_date,
        report_date,
        target_date,
    ):
        testcase.assertEqual(
            job.occurrence_date,
            occurrence_date.isoformat(),
        )
        testcase.assertEqual(
            job.execution_date,
            execution_date.isoformat(),
        )
        testcase.assertEqual(
            job.t_date,
            t_date.isoformat(),
        )
        testcase.assertEqual(
            job.report_date,
            report_date.isoformat(),
        )
        testcase.assertEqual(
            job.target_date,
            target_date.isoformat(),
        )

    def test_occurrence_is_created_once_and_survives_multiple_cycles(self):
        """
        Cycle 1:
            16-Sep
            occurrence selected = 15-Sep
            execution_date = 17-Sep
            STAGING

        Cycle 2:
            17-Sep
            SAME occurrence must still be 15-Sep
            T/target are established from DATEMAST
            DATEMAST latest = 17-Sep, so target 18-Sep is not ready
            STAGING

        Cycle 3:
            18-Sep
            SAME occurrence must still be 15-Sep
            target 18-Sep becomes available
            READY

        This is the critical repeated 3-minute-cycle persistence test.
        """

        cycle_1 = datetime(
            2026, 9, 16, 10, 0
        )
        self.datemast.set_cycle_date(
            cycle_1.date()
        )

        summary_1 = self.scheduler.run_cycle(
            current_datetime=cycle_1
        )

        self.assertEqual(
            summary_1["staging_count"],
            1,
        )
        self.assertEqual(
            summary_1["ready_count"],
            0,
        )

        staging_1 = (
            self.staging_repository.get_by_id(
                self.job.id
            )
        )

        self.assertIsNotNone(staging_1)

        self.assertEqual(
            staging_1.occurrence_date,
            "2026-09-15",
        )
        self.assertEqual(
            staging_1.execution_date,
            "2026-09-17",
        )
        self.assertEqual(
            staging_1.report_date,
            "2026-09-15",
        )

        # T and target belong to the occurrence itself, not to the
        # scheduler's current cycle date. Therefore even though the
        # execution date is still in the future, the occurrence context
        # is already fixed:
        #
        #     T      = 15-Sep
        #     T+3    = 18-Sep
        #
        # These values must remain unchanged on later cycles.
        self.assertEqual(
            staging_1.t_date,
            "2026-09-15",
        )
        self.assertEqual(
            staging_1.target_date,
            "2026-09-18",
        )

        calls_after_cycle_1 = len(
            self.frequency_evaluator
            .previous_occurrence_calls
        )

        cycle_2 = datetime(
            2026, 9, 17, 10, 0
        )
        self.datemast.set_cycle_date(
            cycle_2.date()
        )

        summary_2 = self.scheduler.run_cycle(
            current_datetime=cycle_2
        )

        self.assertEqual(
            summary_2["staging_count"],
            1,
        )
        self.assertEqual(
            summary_2["ready_count"],
            0,
        )

        staging_2 = (
            self.staging_repository.get_by_id(
                self.job.id
            )
        )

        self.assertIsNotNone(staging_2)

        # Most important assertion:
        # the scheduler did not create a new 17-Sep occurrence.
        self.assertEqual(
            staging_2.occurrence_date,
            "2026-09-15",
        )
        self.assertEqual(
            staging_2.execution_date,
            "2026-09-17",
        )
        self.assertEqual(
            staging_2.report_date,
            "2026-09-15",
        )

        self.assertEqual(
            staging_2.t_date,
            "2026-09-15",
        )
        self.assertEqual(
            staging_2.target_date,
            "2026-09-18",
        )

        # Existing STAGING must prevent a new occurrence lookup.
        self.assertEqual(
            len(
                self.frequency_evaluator
                .previous_occurrence_calls
            ),
            calls_after_cycle_1,
        )

        cycle_3 = datetime(
            2026, 9, 18, 10, 0
        )
        self.datemast.set_cycle_date(
            cycle_3.date()
        )

        summary_3 = self.scheduler.run_cycle(
            current_datetime=cycle_3
        )

        self.assertEqual(
            summary_3["staging_count"],
            0,
        )
        self.assertEqual(
            summary_3["ready_count"],
            1,
        )
        self.assertEqual(
            summary_3["queue_size"],
            1,
        )

        self.assertIsNone(
            self.staging_repository.get_by_id(
                self.job.id
            )
        )

        ready = (
            self.ready_repository.get_by_id(
                self.job.id
            )
        )

        self.assertIsNotNone(ready)

        # All five context fields must survive STAGING -> READY.
        self.assertEqual(
            ready.occurrence_date,
            "2026-09-15",
        )
        self.assertEqual(
            ready.execution_date,
            "2026-09-17",
        )
        self.assertEqual(
            ready.t_date,
            "2026-09-15",
        )
        self.assertEqual(
            ready.report_date,
            "2026-09-15",
        )
        self.assertEqual(
            ready.target_date,
            "2026-09-18",
        )

    def test_scheduler_rebuilds_queue_from_persistent_ready(self):
        """
        READY is persistent state; the queue is derived state.

        After the READY record exists, clear the in-memory heap and rebuild
        it from READY. The report-date context must remain intact.
        """

        cycle = datetime(
            2026, 9, 18, 10, 0
        )
        self.datemast.set_cycle_date(
            cycle.date()
        )

        # First create the persisted READY row by running the three
        # scheduler dates in sequence.
        for cycle_datetime in (
            datetime(2026, 9, 16, 10, 0),
            datetime(2026, 9, 17, 10, 0),
            cycle,
        ):
            self.datemast.set_cycle_date(
                cycle_datetime.date()
            )
            self.scheduler.run_cycle(
                current_datetime=cycle_datetime
            )

        ready = (
            self.ready_repository.get_by_id(
                self.job.id
            )
        )

        self.assertIsNotNone(ready)

        self.priority_queue.clear()

        self.assertTrue(
            self.priority_queue.is_empty()
        )

        self.priority_queue.rebuild(
            self.ready_repository.get_all()
        )

        self.assertEqual(
            self.priority_queue.size(),
            1,
        )

        queue_item = (
            self.priority_queue.peek()
        )

        self.assertIsNotNone(queue_item)

        self.assertEqual(
            queue_item.job_id,
            self.job.id,
        )
        self.assertEqual(
            queue_item.report_date,
            "2026-09-15",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
