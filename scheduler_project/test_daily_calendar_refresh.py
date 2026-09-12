"""Daily DATEMAST caching uses a local date boundary, not a 24-hour timer."""

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import main
from repositories.oracle_schedule_master_repository import OracleMasterSyncError
from repositories.schedule_master_repository import ScheduleMasterRepository
from scheduler.datemast import DateMast
from scheduler.holiday import HolidayEvaluator
from control_api import SchedulerControlApi


class DailyCalendarRefreshTests(unittest.TestCase):
    def application(self):
        datemast = DateMast(report_dates=["2026-09-10"], today_provider=lambda: date(2026, 9, 12))
        repository = Mock()
        repository.read_calendar.return_value = {
            "report_dates": ["2026-09-10", "2026-09-11", "2026-09-12"],
            "holidays": None, "warning": None,
        }
        return {"execution_enabled": False, "calendar_snapshot_repository": repository,
                "datemast": datemast, "holiday_evaluator": HolidayEvaluator(datemast=datemast)}

    def refresh(self, app, at, monotonic=0, **kwargs):
        with patch.object(main.time, "monotonic", return_value=monotonic):
            return main._refresh_calendar_snapshots_if_due(app, current_datetime=datetime.fromisoformat(at), **kwargs)

    def test_every_cycle_reuses_successful_read_for_the_rest_of_the_day(self):
        app = self.application()
        self.assertTrue(self.refresh(app, "2026-09-12T00:01:00")["refreshed"])
        for hour in (1, 5, 12, 23):
            self.assertFalse(self.refresh(app, f"2026-09-12T{hour:02}:59:59", monotonic=hour * 3600)["refreshed"])
        app["calendar_snapshot_repository"].read_calendar.assert_called_once()
        self.assertEqual(app["calendar_next_refresh_at"], "2026-09-13T00:00:00")
        self.assertEqual(app["datemast"].get_latest_report_date(), date(2026, 9, 11))

    def test_first_cycle_after_midnight_refreshes_even_less_than_24_hours_later(self):
        app = self.application()
        self.refresh(app, "2026-09-12T23:59:59", monotonic=100)
        self.assertTrue(self.refresh(app, "2026-09-13T00:00:01", monotonic=102)["refreshed"])
        self.assertEqual(app["calendar_snapshot_repository"].read_calendar.call_count, 2)

    def test_manual_refresh_picks_up_same_day_corrections(self):
        app = self.application()
        self.refresh(app, "2026-09-12T09:00:00")
        app["calendar_snapshot_repository"].read_calendar.return_value["report_dates"].append("2026-09-09")
        result = self.refresh(app, "2026-09-12T09:01:00", force=True)
        self.assertTrue(result["refreshed"])
        self.assertIn(date(2026, 9, 9), app["datemast"].get_report_dates())
        self.assertEqual(result["last_refresh_at"], "2026-09-12T09:01:00")

    def test_failed_forced_read_recovers_with_backoff_despite_earlier_success(self):
        app = self.application()
        self.refresh(app, "2026-09-12T09:00:00")
        repository = app["calendar_snapshot_repository"]
        values = repository.read_calendar.return_value
        repository.read_calendar.side_effect = [OracleMasterSyncError("unavailable"), values]
        failed = self.refresh(app, "2026-09-12T09:01:00", monotonic=60, force=True)
        self.assertFalse(failed["available"])
        self.assertFalse(failed["refreshed"])
        self.assertTrue(app["datemast"].get_report_dates())
        self.refresh(app, "2026-09-12T09:01:01", monotonic=61)
        self.assertEqual(repository.read_calendar.call_count, 2)
        recovered = self.refresh(app, "2026-09-12T09:06:00", monotonic=60 + main.CALENDAR_RETRY_SECONDS)
        self.assertTrue(recovered["available"])
        self.assertTrue(recovered["refreshed"])
        self.assertIsNone(recovered["error"])
        self.assertEqual(repository.read_calendar.call_count, 3)

    def test_optional_holiday_warning_does_not_repeat_datemast_queries(self):
        app = self.application()
        app["calendar_snapshot_repository"].read_calendar.return_value["warning"] = "Optional holiday source unavailable"
        self.refresh(app, "2026-09-12T09:00:00")
        result = self.refresh(app, "2026-09-12T15:00:00", monotonic=21600)
        app["calendar_snapshot_repository"].read_calendar.assert_called_once()
        self.assertTrue(result["available"])
        self.assertTrue(result["error"])

    def test_worker_reloads_published_snapshots_after_manual_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "datemast.json"
            path.write_text('["2026-09-10"]', encoding="utf-8")
            app = self.application()
            app["execution_enabled"] = True
            app["datemast"] = DateMast(file_path=path)

            def publish():
                path.write_text('["2026-09-10", "2026-09-11"]', encoding="utf-8")
                return {"datemast": 2, "holidays": None}

            app["calendar_snapshot_repository"].refresh_snapshots.side_effect = publish
            result = self.refresh(app, "2026-09-12T09:00:00", force=True)
            self.assertTrue(result["available"])
            self.assertEqual(app["datemast"].get_report_dates(), [date(2026, 9, 10), date(2026, 9, 11)])
            app["calendar_snapshot_repository"].read_calendar.assert_not_called()

    def test_schedule_master_changes_refresh_independently_during_cached_day(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.application()
            source = Mock()
            record = {"id": 1, "name": "BEFORE", "package_name": "RPT.PKG.RUN",
                      "run_config": {"RUNS_ON": ["DAILY"]}, "margin": "T",
                      "same_day": 0, "time_flag": 0, "is_active": 1}
            source.refresh_snapshot.side_effect = [[record], [{**record, "name": "AFTER"}]]
            master = ScheduleMasterRepository(Path(directory) / "master.json", source="oracle",
                                              oracle_repository=source, refresh_seconds=0)
            self.refresh(app, "2026-09-12T09:00:00")
            self.assertEqual(master.get_all()[0].name, "BEFORE")
            self.refresh(app, "2026-09-12T15:00:00", monotonic=21600)
            self.assertEqual(master.get_all()[0].name, "AFTER")
            app["calendar_snapshot_repository"].read_calendar.assert_called_once()
            self.assertEqual(source.refresh_snapshot.call_count, 2)

    def test_monitor_keeps_coverage_in_provider_refresh_response_and_api(self):
        today = date.today()
        start, yesterday = today - timedelta(days=400), today - timedelta(days=1)
        app = self.application()
        app["datemast"] = DateMast(today_provider=lambda: today)
        app["holiday_evaluator"] = HolidayEvaluator(datemast=app["datemast"])
        app["calendar_snapshot_repository"].read_calendar.return_value = {
            "report_dates": [yesterday.isoformat(), today.isoformat()],
            "coverage_start": start.isoformat(), "coverage_end": today.isoformat(),
            "holidays": None, "warning": None,
        }
        result = self.refresh(app, today.isoformat() + "T09:00:00", force=True)
        self.assertEqual(result["coverage_start"], start.isoformat())
        self.assertEqual(app["datemast"].coverage_end, today)
        self.assertEqual(app["datemast"].get_latest_report_date(), yesterday)
        meta = SchedulerControlApi(app).calendar_metadata()
        self.assertEqual(meta["coverage_end"], today.isoformat())
        self.assertEqual(meta["holiday_observed_from"], start.isoformat())
        self.assertEqual(meta["holiday_observed_through"], yesterday.isoformat())
        app["calendar_snapshot_repository"].read_calendar.side_effect = OracleMasterSyncError("unavailable")
        result = self.refresh(app, today.isoformat() + "T10:00:00", force=True)
        self.assertEqual(result["coverage_start"], start.isoformat())
        self.assertIsNone(SchedulerControlApi(app).calendar_metadata()["holiday_observed_through"])

    def test_worker_snapshot_reload_carries_coverage_and_caps_history_metadata(self):
        today = date.today()
        start, end = today - timedelta(days=400), today - timedelta(days=3)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "datemast.json"
            path.write_text("[]", encoding="utf-8")
            app = self.application()
            app["execution_enabled"] = True
            app["datemast"] = DateMast(file_path=path, today_provider=lambda: today)
            app["holiday_evaluator"] = HolidayEvaluator(datemast=app["datemast"])

            def publish():
                path.write_text(json.dumps({"report_dates": [end.isoformat()],
                    "coverage_start": start.isoformat(), "coverage_end": end.isoformat()}), encoding="utf-8")
                return {"datemast": 1, "holidays": None}

            app["calendar_snapshot_repository"].refresh_snapshots.side_effect = publish
            result = self.refresh(app, today.isoformat() + "T09:00:00", force=True)
            self.assertTrue(result["available"])
            self.assertEqual(result["coverage_start"], start.isoformat())
            self.assertEqual(result["coverage_end"], end.isoformat())
            self.assertEqual(SchedulerControlApi(app).calendar_metadata()["holiday_observed_through"], end.isoformat())
            marker = app["holiday_evaluator"].classify_date(end + timedelta(days=1))
            self.assertNotEqual(marker["source"], "datemast")
            self.assertTrue(marker["is_provisional"])


if __name__ == "__main__":
    unittest.main()
