"""Isolated banking-calendar regressions; no source writes or Oracle calls."""
import json
import tempfile
import os
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from execution.execution_manager import ExecutionManager

from scheduler.datemast import DateMast
from scheduler.eligibility import EligibilityEvaluator
from scheduler.frequency import FrequencyEvaluator
from scheduler.holiday import HolidayEvaluator
from scheduler.margin import MarginCalculator
from scheduler.occurrence import OracleCompatibleOccurrencePlanner
from scheduler.time_window import TimeWindowEvaluator
from scheduler.upcoming import UpcomingPlanner


class BankCalendarTests(unittest.TestCase):
    def evaluator(self, dates=None, today=date(2026, 9, 10), holidays=None):
        clock = lambda: today
        source = DateMast(report_dates=dates, today_provider=clock)
        return HolidayEvaluator(holidays=holidays, datemast=source, today_provider=clock), source

    def test_t_minus_one_observation_and_future_seed_cannot_publish(self):
        calendar, source = self.evaluator(["2026-09-09", "2026-09-10", "2026-10-01"])
        self.assertEqual(source.get_latest_report_date(), date(2026, 9, 9))
        self.assertEqual(source.get_previous_report_date("2026-10-02"), date(2026, 9, 9))
        self.assertEqual(source.get_report_date_for("2026-10-02"), date(2026, 9, 9))
        self.assertFalse(source.has_report_date("2026-09-10"))
        self.assertFalse(source.latest_is_at_least("2026-09-10"))
        self.assertEqual(len(source.get_report_dates()), 3)
        past = calendar.classify_date("2026-09-08")
        self.assertEqual(past["kind"], "HOLIDAY")
        self.assertEqual(past["source"], "datemast")
        self.assertFalse(past["is_provisional"])
        for value in ("2026-09-10", "2026-09-11", "2026-10-01"):
            day = calendar.classify_date(value)
            self.assertFalse(day["is_holiday"])
            self.assertTrue(day["is_provisional"])

    def test_all_five_saturdays_and_month_leap_boundaries(self):
        calendar, _ = self.evaluator(today=date(2024, 1, 1))
        for number, value in enumerate((3, 10, 17, 24, 31), 1):
            day = calendar.classify_date(date(2026, 1, value))
            self.assertEqual(day["is_working_day"], number in {1, 3, 5})
            self.assertEqual(day["is_bank_holiday"], number in {2, 4})
        for value, working in (("2024-02-24", False), ("2024-02-29", True), ("2024-03-02", True)):
            self.assertEqual(calendar.is_working_day(value), working)

    def test_past_presence_overrides_defaults_absence_overrides_open_saturday(self):
        calendar, source = self.evaluator(["2026-08-30", "2026-09-09"], holidays=["2026-08-30"])
        self.assertTrue(calendar.is_working_day("2026-08-30"))  # Special working Sunday.
        self.assertEqual(calendar.get_day_type("2026-09-05"), "HOLIDAY")
        source.replace_report_dates(["2026-08-30", "2026-09-05", "2026-09-09"])
        self.assertTrue(calendar.is_working_day("2026-09-05"))  # Shared live provider.
        source.replace_report_dates(["2026-08-22", "2026-09-09"])
        self.assertTrue(calendar.is_working_day("2026-08-22"))  # Special fourth Saturday.

    def test_empty_or_failed_source_does_not_infer_missing_holidays(self):
        calendar, source = self.evaluator([])
        self.assertTrue(calendar.is_working_day("2026-09-08"))
        self.assertTrue(calendar.classify_date("2026-09-08")["is_provisional"])
        source.replace_report_dates(["2026-09-07"])
        source.available = False
        self.assertTrue(calendar.is_working_day("2026-09-08"))
        self.assertFalse(calendar.classify_date("2026-09-07")["is_provisional"])
        source.replace_report_dates(["2026-09-07"])
        self.assertFalse(calendar.is_working_day("2026-09-08"))

    def test_margin_before_first_and_second_saturdays_and_explicit_holiday(self):
        calendar, _ = self.evaluator(today=date(2026, 1, 1))
        margin = MarginCalculator(calendar.is_working_day)
        self.assertEqual(margin.next_working_day("2026-09-04"), date(2026, 9, 5))
        self.assertEqual(margin.next_working_day("2026-09-11"), date(2026, 9, 14))
        calendar.add_holiday("2026-09-05")
        self.assertEqual(margin.next_working_day("2026-09-04"), date(2026, 9, 7))
        self.assertEqual(calendar.get_day_type("2026-09-05"), "HOLIDAY")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "holidays.json"
            path.write_text(json.dumps(["2026-09-05"]))
            self.assertEqual(HolidayEvaluator(file_path=path).get_day_type("2026-09-05"), "HOLIDAY")
        self.assertEqual(MarginCalculator().next_working_day("2026-09-04"), date(2026, 9, 5))

    def test_previous_working_date_before_known_history_is_explicitly_unavailable(self):
        calendar, _ = self.evaluator(["2026-09-09"])
        with self.assertRaisesRegex(ValueError, "No previous working date"):
            MarginCalculator(calendar.is_working_day).previous_working_day("2026-09-09")

    def test_file_read_recovery_does_not_erase_separate_upstream_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "datemast.json"
            path.write_text('["2026-09-09"]')
            source = DateMast(file_path=path, today_provider=lambda: date(2026, 9, 10))
            original = path.stat()
            saved = path.with_suffix(".saved")
            path.rename(saved)
            source.reload()
            self.assertFalse(source.available)
            saved.rename(path)
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            source.reload()
            self.assertTrue(source.available)
            source.available = False  # Oracle is down; a readable old file is not a fresh feed.
            source.reload(force=True)
            self.assertFalse(source.available)

    def test_holiday_run_preserves_closed_saturday_and_sunday_overrides(self):
        calendar, _ = self.evaluator(today=date(2026, 1, 1))
        job = SimpleNamespace(run_config={"HOLIDAY_RUN": []})
        self.assertTrue(calendar.can_run_on_day(job, "2026-09-05"))
        self.assertFalse(calendar.can_run_on_day(job, "2026-09-12"))
        job.run_config["HOLIDAY_RUN"] = ["SAT", "SUN"]
        self.assertTrue(calendar.can_run_on_day(job, "2026-09-12"))
        self.assertTrue(calendar.can_run_on_day(job, "2026-09-13"))
        calendar.add_holiday("2026-09-05")
        self.assertFalse(calendar.can_run_on_day(job, "2026-09-05"))
        job.run_config["HOLIDAY_RUN"].append("HOLIDAY")
        self.assertTrue(calendar.can_run_on_day(job, "2026-09-05"))

    def test_weekly_has_one_final_working_day_in_open_and_closed_saturday_weeks(self):
        calendar, _ = self.evaluator(today=date(2026, 1, 1))
        frequency = FrequencyEvaluator(calendar.is_working_day)
        job = SimpleNamespace(run_config={"RUNS_ON": ["WEEKLY"]})
        for value, matches in (("2026-09-04", False), ("2026-09-05", True),
                               ("2026-09-11", True), ("2026-09-12", False)):
            self.assertEqual(frequency.is_scheduled(job, date.fromisoformat(value)), matches)

    def test_daily_planning_and_projection_obey_execution_day_policy(self):
        calendar, source = self.evaluator(["2026-09-04"], today=date(2026, 9, 5))
        frequency = FrequencyEvaluator(calendar.is_working_day)
        margin = MarginCalculator(calendar.is_working_day)
        planner = OracleCompatibleOccurrencePlanner(frequency, margin)
        job = SimpleNamespace(id=1, name="Daily", same_day=0, margin="T", is_active=1,
                              time_flag=0, confirmation_needed=0,
                              run_config={"RUNS_ON": ["DAILY"], "HOLIDAY_RUN": []})
        self.assertEqual(len(planner.due_occurrences(job, "2026-09-05", source)), 1)
        self.assertEqual(planner.due_occurrences(job, "2026-09-12", source), [])
        upcoming = UpcomingPlanner(frequency, calendar, margin, TimeWindowEvaluator(), planner, source)
        self.assertEqual(upcoming.build([job], start_date="2026-09-12", days=2), [])
        context = planner.due_occurrences(job, "2026-09-05", source)[0]
        eligibility = EligibilityEvaluator(holiday_evaluator=calendar)
        result = eligibility.evaluate_occurrence(job, datetime(2026, 9, 12, 10), context)
        self.assertFalse(result.eligible)
        self.assertEqual(result.waiting_for, "MANUAL_RUN")
        job.same_day = 1
        same_day_context = planner.due_occurrences(job, "2026-09-05", source)[0]
        result = eligibility.evaluate_occurrence(job, datetime(2026, 9, 12, 10), same_day_context)
        self.assertFalse(result.eligible)
        self.assertEqual(result.waiting_for, "MANUAL_RUN")
        self.assertEqual(result.report_date, date(2026, 9, 5))
        job.same_day = 0
        job.run_config["HOLIDAY_RUN"] = ["SAT"]
        self.assertEqual(len(planner.due_occurrences(job, "2026-09-12", source)), 1)

    def test_execution_and_historical_retries_recheck_actual_calendar_day(self):
        job = SimpleNamespace(id=1, name="Daily", is_active=1, confirmation_needed=0,
                              package_name="PKG.RUN", run_config={"HOLIDAY_RUN": []})
        candidate = SimpleNamespace(id=4, job_id=1, job_name="Daily", report_date="2026-09-11",
                                    status="FAILED", occurrence_key="1:2026-09-11")
        executions = Mock()
        executions.get_latest.return_value = candidate
        executions.get_planned_execution_date.return_value = date(2026, 9, 12)
        executions.has_success.return_value = False
        executions.has_running.return_value = False
        executions.get_by_id.return_value = SimpleNamespace(attempt_no=2)
        master = Mock()
        master.get_by_id.return_value = job
        queue = Mock()
        queue.is_empty.return_value = False
        queue.peek.return_value = candidate
        ready = Mock()
        ready.get_by_key.return_value = candidate
        oracle = Mock()
        oracle.execute.return_value = SimpleNamespace(success=True, count=1, duration_seconds=0)
        manager = ExecutionManager(ready, queue, oracle, executions, schedule_master_repository=master)
        day = datetime(2026, 9, 12, 8)
        self.assertEqual(manager.execute_next(day)["status"], "CALENDAR_POLICY_BLOCKED")
        self.assertEqual(manager.execute_retry_candidates([candidate], day)[0]["status"], "CALENDAR_POLICY_BLOCKED")
        oracle.execute.assert_not_called()
        executions.start_execution.assert_not_called()
        job.run_config["HOLIDAY_RUN"] = ["SAT"]
        result = manager.execute_retry_candidates([candidate], day)[0]
        self.assertTrue(result["executed"])
        self.assertEqual(result["report_date"], date(2026, 9, 11))
        oracle.execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
