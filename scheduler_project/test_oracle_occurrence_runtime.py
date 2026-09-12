"""Regression coverage for Oracle-package-compatible occurrence runtime."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import database.sqlite_db as sqlite_db
from execution.execution_manager import ExecutionManager
from models.schedule_master import ScheduleMaster
from repositories.execution_repository import ExecutionRepository
from repositories.job_control_repository import JobControlRepository
from repositories.occurrence_repository import (
    OccurrenceReadyRepository,
    OccurrenceStagingRepository,
)
from scheduler.confirmation import ConfirmationEvaluator
from scheduler.datemast import DateMast
from scheduler.eligibility import EligibilityEvaluator
from scheduler.frequency import FrequencyEvaluator
from scheduler.holiday import HolidayEvaluator
from scheduler.margin import MarginCalculator
from scheduler.occurrence import OracleCompatibleOccurrencePlanner
from scheduler.occurrence_scheduler import OccurrenceScheduler
from scheduler.priority import PriorityCalculator
from scheduler.time_window import TimeWindowEvaluator
from scheduler.upcoming import UpcomingPlanner
from scheduler_queue.priority_queue import PriorityQueue


class _MasterRepository:
    def __init__(self, jobs):
        self.jobs = list(jobs)

    def get_active(self):
        return [job for job in self.jobs if bool(job.is_active)]

    def get_by_id(self, job_id):
        return next((job for job in self.jobs if job.id == job_id), None)


class _PendingMirror:
    def __init__(self):
        self.records = []

    def get_by_name_report_date(self, name, report_date):
        report_date = str(report_date)[:10]
        for record in self.records:
            if record["name"] == name and record["report_date"] == report_date:
                return record
        return None

    def get_pending(self):
        return [
            record
            for record in self.records
            if record.get("status") == "PENDING"
        ]

    def create_pending(self, name, report_date, **kwargs):
        record = {
            "id": len(self.records) + 1,
            "name": name,
            "report_date": str(report_date)[:10],
            "status": "PENDING",
            "run_date": "2026-08-17T09:00:00",
        }
        self.records.append(record)
        return record


class _SuccessOracle:
    def execute(self, procedure_name, report_date):
        return SimpleNamespace(
            success=True,
            count=7,
            duration_seconds=0.01,
            error=None,
            error_type=None,
        )


class OracleOccurrenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.original_db = sqlite_db.SQLITE_DB
        sqlite_db.SQLITE_DB = Path(self.temp_directory.name) / "scheduler.db"
        sqlite_db.create_tables()
        self.connection = sqlite_db.get_connection()

        self.holidays = HolidayEvaluator()
        self.frequency = FrequencyEvaluator(
            working_day_checker=self.holidays.is_working_day,
        )
        self.margin = MarginCalculator(
            working_day_checker=self.holidays.is_working_day,
        )
        self.planner = OracleCompatibleOccurrencePlanner(
            frequency_evaluator=self.frequency,
            margin_calculator=self.margin,
        )
        self.time_window = TimeWindowEvaluator()
        self.eligibility = EligibilityEvaluator(
            frequency_evaluator=self.frequency,
            holiday_evaluator=self.holidays,
            margin_calculator=self.margin,
            confirmation_evaluator=ConfirmationEvaluator(),
            time_window_evaluator=self.time_window,
        )

    def tearDown(self):
        self.connection.close()
        sqlite_db.SQLITE_DB = self.original_db
        self.temp_directory.cleanup()

    @staticmethod
    def _job(*, job_id=701, runs_on=None, time_flag=0, run_by=None):
        run_config = {"RUNS_ON": list(runs_on or ["DAILY", "FORTNIGHTLY"])}
        if run_by is not None:
            run_config["RUN_BY"] = run_by
        return ScheduleMaster(
            id=job_id,
            name="CASH_POSITION_{0}".format(job_id),
            package_name="REPORTING.PKG_CASH.RUN_REPORT",
            run_config=run_config,
            margin="T",
            same_day=0,
            time_flag=time_flag,
            is_active=1,
            confirmation_needed=0,
        )

    def _runtime(self, job, report_dates, mirror=None):
        staging = OccurrenceStagingRepository(self.connection)
        ready = OccurrenceReadyRepository(self.connection)
        controls = JobControlRepository(self.connection)
        execution = ExecutionRepository(self.connection)
        queue = PriorityQueue()
        scheduler = OccurrenceScheduler(
            schedule_master_repository=_MasterRepository([job]),
            job_control_repository=controls,
            occurrence_planner=self.planner,
            eligibility_evaluator=self.eligibility,
            staging_repository=staging,
            ready_repository=ready,
            priority_calculator=PriorityCalculator(),
            priority_queue=queue,
            datemast=DateMast(report_dates=report_dates),
            schedule_extg_repository=mirror,
            execution_repository=execution,
        )
        return scheduler, staging, ready, controls, execution, queue

    def test_legacy_daily_and_fortnightly_contexts_are_distinct(self):
        job = self._job()
        datemast = DateMast(report_dates=[date(2026, 8, 13), date(2026, 8, 14)])

        # 14-Aug daily uses strict prior DATEMAST T date, not 14-Aug itself.
        on_fourteenth = self.planner.due_occurrences(job, date(2026, 8, 14), datemast)
        self.assertEqual(len(on_fourteenth), 1)
        self.assertEqual(on_fourteenth[0].report_date, date(2026, 8, 13))
        self.assertEqual(on_fourteenth[0].execution_date, date(2026, 8, 14))

        # Saturday daily stays on the invocation date; the 15th periodic
        # occurrence is deferred separately to Monday 17-Aug.
        on_fifteenth = self.planner.due_occurrences(job, date(2026, 8, 15), datemast)
        self.assertEqual(len(on_fifteenth), 1)
        self.assertEqual(on_fifteenth[0].report_date, date(2026, 8, 14))
        self.assertEqual(on_fifteenth[0].execution_date, date(2026, 8, 15))

        on_monday = self.planner.due_occurrences(job, date(2026, 8, 17), datemast)
        self.assertEqual(
            {(item.frequency, item.report_date, item.execution_date) for item in on_monday},
            {
                ("DAILY", date(2026, 8, 14), date(2026, 8, 17)),
                ("FORTNIGHTLY", date(2026, 8, 15), date(2026, 8, 17)),
            },
        )
        self.assertEqual(
            {item.occurrence_key for item in on_monday},
            {"701:2026-08-14", "701:2026-08-15"},
        )

    def test_runtime_keeps_two_ready_rows_and_execution_removes_only_one(self):
        job = self._job()
        mirror = _PendingMirror()
        scheduler, staging, ready, controls, execution, queue = self._runtime(
            job,
            [date(2026, 8, 14)],
            mirror=mirror,
        )

        summary = scheduler.run_cycle(datetime(2026, 8, 17, 9, 0))
        self.assertEqual(summary["ready_count"], 2)
        self.assertEqual(staging.count(), 0)
        self.assertEqual(queue.size(), 2)
        self.assertEqual(
            {row.occurrence_key for row in ready.get_by_job_id(job.id)},
            {"701:2026-08-14", "701:2026-08-15"},
        )
        self.assertEqual(len(mirror.records), 2)

        manager = ExecutionManager(
            ready_repository=ready,
            priority_queue=queue,
            oracle_executor=_SuccessOracle(),
            execution_repository=execution,
            job_control_repository=controls,
            schedule_master_repository=_MasterRepository([job]),
        )
        result = manager.execute_next(datetime(2026, 8, 17, 9, 1))
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(ready.count(), 1)
        self.assertEqual(
            ready.get_all()[0].occurrence_key,
            "701:2026-08-15",
        )

        # A later tick sees the successful 14-Aug occurrence in durable
        # execution history and does not recreate it before DATEMAST moves.
        scheduler.run_cycle(datetime(2026, 8, 17, 9, 2))
        self.assertEqual(ready.count(), 1)
        self.assertEqual(queue.size(), 1)

    def test_expired_window_can_reopen_same_day_but_requires_manual_after_midnight(self):
        job = self._job(
            job_id=702,
            runs_on=["DAILY"],
            time_flag=1,
            run_by={"FROM_TIME": "09:00", "TO_TIME": "10:00"},
        )
        scheduler, staging, ready, _, _, _ = self._runtime(
            job,
            [date(2026, 8, 14)],
        )

        scheduler.run_cycle(datetime(2026, 8, 17, 17, 0))
        pending = staging.get_by_job_id(job.id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].occurrence_key, "702:2026-08-14")
        self.assertEqual(pending[0].waiting_for, "TIME_WINDOW")
        self.assertEqual(ready.count(), 0)

        scheduler.run_cycle(datetime(2026, 8, 17, 9, 30))
        self.assertEqual(staging.count(), 0)
        self.assertEqual(ready.count(), 1)

        # If a higher-priority job left this row queued, the next tick after
        # 10:00 must not run it outside RUN_BY; it returns to pending state.
        scheduler.run_cycle(datetime(2026, 8, 17, 17, 0))
        self.assertEqual(ready.count(), 0)
        self.assertEqual(staging.count(), 1)
        self.assertEqual(staging.get_all()[0].waiting_for, "TIME_WINDOW")

        scheduler.run_cycle(datetime(2026, 8, 18, 9, 30))
        self.assertEqual(staging.count(), 1)
        self.assertEqual(ready.count(), 0)
        self.assertEqual(staging.get_all()[0].occurrence_key, "702:2026-08-14")
        self.assertEqual(staging.get_all()[0].state, "MANUAL_REQUIRED")

    def test_upcoming_daily_placeholder_never_invents_future_datemast_date(self):
        job = self._job(job_id=703, runs_on=["DAILY"])
        planner = UpcomingPlanner(
            frequency_evaluator=self.frequency,
            holiday_evaluator=self.holidays,
            margin_calculator=self.margin,
            time_window_evaluator=self.time_window,
            occurrence_planner=self.planner,
            datemast=DateMast(report_dates=[date.today()]),
        )
        records = planner.build(
            [job],
            start_date=date.today() + timedelta(days=7 - date.today().weekday()),
            days=2,
        )
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item["state"] == "FORECAST_AWAITING_DATEMAST" for item in records))
        self.assertTrue(all(item["report_date"] is None for item in records))
        self.assertTrue(all(item["occurrence_key"] is None for item in records))

    def test_legacy_state_migration_is_one_time_and_does_not_resurrect_removed_row(self):
        # Simulate an upgrade from a job-keyed scheduler.db.  The marker is
        # intentionally reset here because setUp already initialized a fresh
        # occurrence-aware database.
        self.connection.execute("DELETE FROM scheduler_schema_migrations")
        self.connection.execute("DELETE FROM staging_occurrences")
        self.connection.execute(
            """
            INSERT INTO staging_jobs (
                job_id, job_name, state, occurrence_date, execution_date,
                t_date, report_date, target_date, margin,
                confirmation_required, confirmation_status, time_flag,
                from_time, to_time, waiting_for, reason, next_evaluation,
                calculated_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                799,
                "LEGACY_ROW",
                "STAGING",
                "2026-08-15",
                "2026-08-17",
                "2026-08-15",
                "2026-08-15",
                "2026-08-15",
                "T",
                0,
                None,
                0,
                None,
                None,
                "TIME_WINDOW",
                "Legacy pending occurrence",
                "2026-08-17T09:00:00",
                "2026-08-17T08:00:00",
                "2026-08-17T08:00:00",
            ),
        )
        self.connection.commit()

        sqlite_db.create_tables()
        repository = OccurrenceStagingRepository(self.connection)
        migrated = repository.get_by_key("799:2026-08-15")
        self.assertIsNotNone(migrated)
        self.assertEqual(migrated.job_name, "LEGACY_ROW")

        repository.delete_by_key("799:2026-08-15")
        sqlite_db.create_tables()
        self.assertIsNone(repository.get_by_key("799:2026-08-15"))

    def test_prior_pending_schedule_extg_is_rehydrated_without_losing_report_date(self):
        job = self._job(job_id=704, runs_on=["SPECIFIC_DATE"])
        job.run_config["SPECIFIC_DATES"] = ["2026-01-01"]
        mirror = _PendingMirror()
        mirror.records.append(
            {
                "id": 1,
                "name": job.name,
                "report_date": "2026-08-10",
                "run_date": "2026-08-11T09:00:00",
                "status": "PENDING",
            }
        )
        scheduler, staging, ready, _, _, queue = self._runtime(
            job,
            [date(2026, 8, 14)],
            mirror=mirror,
        )

        scheduler.run_cycle(datetime(2026, 8, 17, 9, 0))
        self.assertEqual(staging.count(), 1)
        self.assertEqual(ready.count(), 0)
        self.assertEqual(queue.size(), 0)
        self.assertEqual(staging.get_all()[0].occurrence_key, "704:2026-08-10")
        self.assertEqual(staging.get_all()[0].report_date, "2026-08-10")
        self.assertEqual(staging.get_all()[0].state, "MANUAL_REQUIRED")


if __name__ == "__main__":
    unittest.main()
