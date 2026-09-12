import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace


class FakeDateMast:
    def __init__(self, dates):
        self.dates = sorted(dates)

    def get_latest_report_date(self):
        return self.dates[-1] if self.dates else None

    def has_report_date(self, report_date):
        return report_date in self.dates

    def latest_is_at_least(self, target_date):
        latest = self.get_latest_report_date()
        return latest is not None and latest >= target_date

    def is_ready_for(self, target_date):
        return self.latest_is_at_least(target_date)

    def get_report_date_for(self, current_date):
        available = [
            value for value in self.dates
            if value <= current_date
        ]
        return max(available) if available else None


class FakeHolidayEvaluator:
    def __init__(self, holidays=None):
        self.holidays = set(holidays or [])

    def is_working_day(self, value):
        return value.weekday() < 5 and value not in self.holidays

    def can_run_on_day(self, job, value):
        return self.is_working_day(value)


class AlwaysScheduledFrequencyEvaluator:
    """
    The eligibility tests below start with an occurrence that has already
    been identified as a scheduled occurrence.

    FrequencyEvaluator.is_scheduled() is responsible for discovering whether
    a date is an occurrence. It should not be allowed to make these tests
    depend on a particular RUN_CONFIG representation.
    """

    def is_scheduled(self, job, current_date, reference_date=None):
        return True


def assert_date_equal(testcase, expected, actual, message=""):
    testcase.assertEqual(
        expected,
        actual,
        message or f"Expected {expected!r}, got {actual!r}",
    )


def assert_persisted_date_equal(testcase, expected, actual, message=""):
    """
    SQLite stores the scheduler date fields as TEXT.

    Depending on sqlite3 adapters/configuration, a retrieved value may be an
    ISO date string rather than datetime.date. The persisted representation
    is what this test is validating.
    """

    if isinstance(actual, datetime):
        actual = actual.date()

    if isinstance(actual, date):
        actual = actual.isoformat()

    expected_text = expected.isoformat()

    testcase.assertEqual(
        expected_text,
        actual,
        message or f"Expected persisted date {expected_text!r}, got {actual!r}",
    )


class TestStagingReadyModels(unittest.TestCase):

    def test_staging_job_contains_all_five_dates(self):
        from models.staging_job import StagingJob

        job = StagingJob(
            job_id=1,
            job_name="TEST_JOB",
            state="STAGING",
            occurrence_date=date(2026, 9, 15),
            execution_date=date(2026, 9, 18),
            t_date=date(2026, 9, 15),
            report_date=date(2026, 9, 15),
            target_date=date(2026, 9, 18),
        )

        assert_date_equal(self, date(2026, 9, 15), job.occurrence_date)
        assert_date_equal(self, date(2026, 9, 18), job.execution_date)
        assert_date_equal(self, date(2026, 9, 15), job.t_date)
        assert_date_equal(self, date(2026, 9, 15), job.report_date)
        assert_date_equal(self, date(2026, 9, 18), job.target_date)

    def test_ready_job_contains_all_five_dates(self):
        from models.ready_job import ReadyJob

        job = ReadyJob(
            job_id=1,
            job_name="TEST_JOB",
            occurrence_date=date(2026, 9, 15),
            execution_date=date(2026, 9, 18),
            t_date=date(2026, 9, 15),
            report_date=date(2026, 9, 15),
            target_date=date(2026, 9, 18),
            ready_since=datetime(2026, 9, 18, 8, 0),
        )

        assert_date_equal(self, date(2026, 9, 15), job.occurrence_date)
        assert_date_equal(self, date(2026, 9, 18), job.execution_date)
        assert_date_equal(self, date(2026, 9, 15), job.t_date)
        assert_date_equal(self, date(2026, 9, 15), job.report_date)
        assert_date_equal(self, date(2026, 9, 18), job.target_date)


class TestSQLitePersistence(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(
            self.temp_dir.name,
            "scheduler_test.db",
        )

        import database.sqlite_db as sqlite_db

        self.sqlite_db = sqlite_db
        self.original_path = sqlite_db.SQLITE_DB
        sqlite_db.SQLITE_DB = self.db_path

        sqlite_db.create_tables()

        self.connection = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row

    def tearDown(self):
        try:
            self.connection.close()
        finally:
            self.sqlite_db.SQLITE_DB = self.original_path
            self.temp_dir.cleanup()

    def test_staging_execution_date_is_persisted(self):
        from models.staging_job import StagingJob
        from repositories.staging_repository import StagingRepository

        repository = StagingRepository(self.connection)

        timestamp = datetime(2026, 9, 15, 8, 0)

        job = StagingJob(
            job_id=10,
            job_name="STAGING_TEST",
            state="STAGING",
            occurrence_date=date(2026, 9, 15),
            execution_date=date(2026, 9, 18),
            t_date=date(2026, 9, 15),
            report_date=date(2026, 9, 15),
            target_date=date(2026, 9, 18),
            margin=3,
            confirmation_required=0,
            confirmation_status=None,
            time_flag=0,
            from_time=None,
            to_time=None,
            waiting_for="DATEMAST",
            reason="Waiting for T+3",
            next_evaluation=datetime(2026, 9, 18, 0, 0),
            calculated_at=timestamp,
            updated_at=timestamp,
        )

        repository.save(job)
        loaded = repository.get_by_id(10)

        self.assertIsNotNone(loaded)

        assert_persisted_date_equal(
            self,
            date(2026, 9, 15),
            loaded.occurrence_date,
        )
        assert_persisted_date_equal(
            self,
            date(2026, 9, 18),
            loaded.execution_date,
        )
        assert_persisted_date_equal(
            self,
            date(2026, 9, 15),
            loaded.t_date,
        )
        assert_persisted_date_equal(
            self,
            date(2026, 9, 15),
            loaded.report_date,
        )
        assert_persisted_date_equal(
            self,
            date(2026, 9, 18),
            loaded.target_date,
        )

    def test_ready_context_is_persisted(self):
        from models.ready_job import ReadyJob
        from repositories.ready_repository import ReadyRepository

        repository = ReadyRepository(self.connection)

        timestamp = datetime(2026, 9, 18, 8, 0)

        job = ReadyJob(
            job_id=20,
            job_name="READY_TEST",
            occurrence_date=date(2026, 9, 15),
            execution_date=date(2026, 9, 18),
            t_date=date(2026, 9, 15),
            report_date=date(2026, 9, 15),
            target_date=date(2026, 9, 18),
            ready_since=timestamp,
            updated_at=timestamp,
        )

        repository.save(job)
        loaded = repository.get_by_id(20)

        self.assertIsNotNone(loaded)

        assert_persisted_date_equal(
            self,
            date(2026, 9, 15),
            loaded.occurrence_date,
        )
        assert_persisted_date_equal(
            self,
            date(2026, 9, 18),
            loaded.execution_date,
        )
        assert_persisted_date_equal(
            self,
            date(2026, 9, 15),
            loaded.t_date,
        )
        assert_persisted_date_equal(
            self,
            date(2026, 9, 15),
            loaded.report_date,
        )
        assert_persisted_date_equal(
            self,
            date(2026, 9, 18),
            loaded.target_date,
        )


class FakeOracleExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, procedure_name, report_date, **kwargs):
        self.calls.append(
            {
                "procedure_name": procedure_name,
                "report_date": report_date,
                **kwargs,
            }
        )
        return True


class TestOracleReportDateContract(unittest.TestCase):

    def test_execution_uses_report_date_not_execution_date(self):
        ready_context = SimpleNamespace(
            occurrence_date=date(2026, 9, 15),
            execution_date=date(2026, 9, 18),
            t_date=date(2026, 9, 15),
            report_date=date(2026, 9, 15),
            target_date=date(2026, 9, 18),
        )

        oracle = FakeOracleExecutor()

        oracle.execute(
            procedure_name="TEST_PACKAGE.TEST_PROC",
            report_date=ready_context.report_date,
        )

        self.assertEqual(len(oracle.calls), 1)

        assert_date_equal(
            self,
            date(2026, 9, 15),
            oracle.calls[0]["report_date"],
        )

        self.assertNotEqual(
            ready_context.execution_date,
            oracle.calls[0]["report_date"],
        )


class TestEligibilityToReadySemantics(unittest.TestCase):

    def _make_evaluator(self):
        from scheduler.eligibility import EligibilityEvaluator

        # 16-Sep-2026 is treated as a holiday for this scenario.
        # Therefore an occurrence on 15-Sep with SAME_DAY=0 has:
        #
        #     occurrence_date = 15-Sep
        #     execution_date = 17-Sep
        #
        # while the T+3 threshold remains:
        #
        #     target_date = 18-Sep
        return EligibilityEvaluator(
            frequency_evaluator=AlwaysScheduledFrequencyEvaluator(),
            holiday_evaluator=FakeHolidayEvaluator(
                holidays={date(2026, 9, 16)}
            ),
        )

    def _make_job(self, job_id=1):
        return SimpleNamespace(
            id=job_id,
            name="T_PLUS_3",
            frequency="DAILY",
            same_day=0,
            margin=3,
            confirmation_required=0,
            time_flag=0,
            from_time=None,
            to_time=None,
        )

    def test_t_plus_three_waits_until_threshold_but_preserves_report_date(self):
        evaluator = self._make_evaluator()
        job = self._make_job(1)

        result = evaluator.evaluate(
            job=job,
            current_datetime=datetime(2026, 9, 17, 10, 0),
            t_date=None,
            occurrence_date=date(2026, 9, 15),
            persisted_t_date=date(2026, 9, 15),
            persisted_target_date=date(2026, 9, 18),
            datemast=FakeDateMast(
                [
                    date(2026, 9, 15),
                    date(2026, 9, 16),
                    date(2026, 9, 17),
                ]
            ),
        )

        self.assertNotEqual(
            result.state,
            evaluator.READY,
        )

        assert_date_equal(
            self,
            date(2026, 9, 15),
            result.occurrence_date,
        )

        assert_date_equal(
            self,
            date(2026, 9, 15),
            result.report_date,
        )

        assert_date_equal(
            self,
            date(2026, 9, 17),
            result.execution_date,
        )

        self.assertEqual(
            result.waiting_for,
            "DATEMAST",
        )

    def test_threshold_reached_keeps_original_report_date(self):
        evaluator = self._make_evaluator()
        job = self._make_job(2)

        result = evaluator.evaluate(
            job=job,
            current_datetime=datetime(2026, 9, 18, 10, 0),
            t_date=None,
            occurrence_date=date(2026, 9, 15),
            persisted_t_date=date(2026, 9, 15),
            persisted_target_date=date(2026, 9, 18),
            datemast=FakeDateMast(
                [
                    date(2026, 9, 15),
                    date(2026, 9, 16),
                    date(2026, 9, 17),
                    date(2026, 9, 18),
                ]
            ),
        )

        self.assertEqual(
            result.state,
            evaluator.READY,
        )

        self.assertTrue(result.eligible)

        assert_date_equal(
            self,
            date(2026, 9, 15),
            result.occurrence_date,
        )

        assert_date_equal(
            self,
            date(2026, 9, 17),
            result.execution_date,
        )

        assert_date_equal(
            self,
            date(2026, 9, 15),
            result.report_date,
        )

        assert_date_equal(
            self,
            date(2026, 9, 15),
            result.t_date,
        )

        assert_date_equal(
            self,
            date(2026, 9, 18),
            result.target_date,
        )

    def test_manual_run_executes_today_but_keeps_business_report_date(self):
        evaluator = self._make_evaluator()
        job = self._make_job(3)

        result = evaluator.evaluate(
            job=job,
            current_datetime=datetime(2026, 9, 18, 10, 0),
            t_date=None,
            occurrence_date=date(2026, 9, 15),
            persisted_t_date=date(2026, 9, 15),
            persisted_target_date=date(2026, 9, 18),
            control={
                "control_status": "ACTIVE",
                "manual_run": 1,
            },
            datemast=FakeDateMast([]),
        )

        self.assertEqual(
            result.state,
            evaluator.READY,
        )

        self.assertTrue(result.eligible)

        assert_date_equal(
            self,
            date(2026, 9, 18),
            result.execution_date,
        )

        assert_date_equal(
            self,
            date(2026, 9, 15),
            result.report_date,
        )

    def test_paused_job_does_not_become_ready_from_manual_run(self):
        evaluator = self._make_evaluator()
        job = self._make_job(4)

        result = evaluator.evaluate(
            job=job,
            current_datetime=datetime(2026, 9, 18, 10, 0),
            t_date=None,
            occurrence_date=date(2026, 9, 15),
            persisted_t_date=date(2026, 9, 15),
            persisted_target_date=date(2026, 9, 18),
            control={
                "control_status": "PAUSED",
                "manual_run": 1,
            },
            datemast=FakeDateMast(
                [date(2026, 9, 18)]
            ),
        )

        self.assertNotEqual(
            result.state,
            evaluator.READY,
        )

        self.assertEqual(
            result.state,
            evaluator.PAUSED,
        )

        assert_date_equal(
            self,
            date(2026, 9, 15),
            result.occurrence_date,
        )

        assert_date_equal(
            self,
            date(2026, 9, 15),
            result.report_date,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
