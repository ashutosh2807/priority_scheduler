"""Monitor startup and live calendar tests with no real Oracle calls."""

import tempfile
import os
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from scheduler.datemast import DateMast
from scheduler.holiday import HolidayEvaluator
from repositories.oracle_schedule_master_repository import OracleCalendarSnapshotRepository, OracleMasterSyncError
from control_api import SchedulerControlApi


class MonitorModeTests(unittest.TestCase):
    def application(self):
        scheduler = Mock()
        scheduler.lifecycle = SimpleNamespace(schedule_extg_repository=Mock())
        scheduler.run_cycle.return_value = {"staging_count": 3, "ready_count": 2, "queue_size": 2}
        operations = Mock()
        operations.is_scheduler_enabled.return_value = True
        return {"scheduler": scheduler, "execution_manager": Mock(), "oracle_executor": Mock(),
                "operations_repository": operations, "calendar_snapshot_repository": None,
                "datemast": DateMast(report_dates=["2026-09-10"]), "holiday_evaluator": HolidayEvaluator()}

    def test_monitor_fences_execution_and_retains_operator_control(self):
        app = self.application()
        main.configure_monitor(app)
        summary = main.run_once(app)
        self.assertEqual(summary["mode"], "monitor")
        self.assertFalse(summary["execution_enabled"])
        app["scheduler"].run_cycle.assert_called_once()
        self.assertEqual(app["execution_manager"].mock_calls, [])
        self.assertEqual(app["oracle_executor"].mock_calls, [])
        app["operations_repository"].set_scheduler_enabled.assert_not_called()
        self.assertIsNone(app["scheduler"].lifecycle.schedule_extg_repository)
        app["operations_repository"].is_scheduler_enabled.return_value = False
        app["scheduler"].run_cycle.reset_mock()
        self.assertEqual(main.run_once(app)["scheduler"]["status"], "STOPPED")
        app["scheduler"].run_cycle.assert_not_called()

    def test_oracle_monitor_refresh_updates_memory_without_writing_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "datemaster.json"
            original = b'["2026-09-10"]'
            source.write_bytes(original)
            app = self.application()
            app["datemast"] = DateMast(file_path=source, today_provider=lambda: date(2026, 9, 11))
            repository = Mock()
            repository.read_calendar.return_value = {
                "report_dates": ["2026-09-10", "2026-09-11", "2026-10-01"],
                "holidays": None, "warning": "Holiday table is unavailable; configured snapshot retained.",
            }
            app["calendar_snapshot_repository"] = repository
            main.configure_monitor(app)
            main.run_once(app)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(app["datemast"].get_latest_report_date().isoformat(), "2026-09-10")
            self.assertEqual(max(app["datemast"].get_report_dates()).isoformat(), "2026-10-01")
            self.assertEqual(app["datemast"].get_previous_report_date("2026-09-11").isoformat(), "2026-09-10")
            self.assertIsNone(app["datemast"].file_path)
            repository.refresh_snapshots.assert_not_called()
            self.assertTrue(app["calendar_refresh_error"])
            self.assertTrue(app["datemast"].available)  # Optional holiday lookup alone does not invalidate DATEMAST.

    def test_failed_oracle_read_retains_positive_dates_but_disables_absence_inference(self):
        app = self.application()
        app["datemast"] = DateMast(report_dates=["2026-09-08"], today_provider=lambda: date(2026, 9, 11))
        app["holiday_evaluator"] = HolidayEvaluator(datemast=app["datemast"], today_provider=lambda: date(2026, 9, 11))
        repository = Mock()
        repository.read_calendar.side_effect = OracleMasterSyncError("Connection unavailable")
        app["calendar_snapshot_repository"] = repository
        main.configure_monitor(app)
        main.run_once(app)
        self.assertFalse(app["datemast"].available)
        self.assertEqual(app["datemast"].get_latest_report_date(), date(2026, 9, 8))
        day = app["holiday_evaluator"].classify_date("2026-09-10")
        self.assertTrue(day["is_working_day"])
        self.assertTrue(day["is_provisional"])
        self.assertIn("unavailable", day["reason"])
        self.assertFalse(app["holiday_evaluator"].classify_date("2026-09-08")["is_provisional"])

    def test_read_only_calendar_keeps_datemast_when_holiday_lookup_fails(self):
        connection = Mock()
        repository = OracleCalendarSnapshotRepository("unused-datemast.json", "unused-holidays.json",
                                                       connection_factory=lambda: connection, holiday_table="HOLIDAY_MASTER")
        with patch.object(repository, "_fetch_dates", side_effect=[
            ["2026-09-10", "2026-09-11"], OracleMasterSyncError("missing holiday table"),
        ]), patch("repositories.oracle_schedule_master_repository._atomic_write_json") as write:
            result = repository.read_calendar()
        self.assertEqual(len(result["report_dates"]), 2)
        self.assertIsNone(result["holidays"])
        self.assertTrue(result["warning"])
        self.assertIn("configured", result["warning"])
        write.assert_not_called()
        connection.close.assert_called_once()

    def test_default_oracle_calendar_does_not_query_an_optional_holiday_table(self):
        for configured in (None, "", "   "):
            with self.subTest(configured=configured), patch.dict(os.environ):
                os.environ.pop("SCHEDULER_HOLIDAY_TABLE", None)
                if configured is not None:
                    os.environ["SCHEDULER_HOLIDAY_TABLE"] = configured
                connection = Mock()
                repository = OracleCalendarSnapshotRepository("unused-datemast.json", "unused-holidays.json",
                                                               connection_factory=lambda: connection,
                                                               today_provider=lambda: date(2026, 9, 12))
                with patch.object(repository, "_fetch_dates", return_value=["2026-09-10"]) as fetch, \
                     patch("repositories.oracle_schedule_master_repository._atomic_write_json") as write:
                    result = repository.read_calendar()
                fetch.assert_called_once_with(connection, table="DATEMAST", date_column="REPORT_DATE", label="DATEMAST",
                                               date_from=date(2025, 3, 31), date_until=date(2026, 9, 13))
                self.assertIsNone(result["holidays"])
                self.assertIsNone(result["warning"])
                self.assertIsNone(repository.holiday_table)
                write.assert_not_called()
                connection.close.assert_called_once()

    def test_default_monitor_clears_old_optional_warning_and_keeps_local_extra_holidays(self):
        with tempfile.TemporaryDirectory() as directory:
            holiday_path = Path(directory) / "holidays.json"
            original = b'["2099-12-31"]\n'
            holiday_path.write_bytes(original)
            app = self.application()
            app["holiday_evaluator"] = HolidayEvaluator(file_path=holiday_path)
            app["calendar_refresh_error"] = "Old optional holiday warning"
            app["holiday_source"] = "retained_snapshot"
            repository = OracleCalendarSnapshotRepository("unused-datemast.json", holiday_path,
                                                           connection_factory=Mock(), holiday_table="")
            app["calendar_snapshot_repository"] = repository
            main.configure_monitor(app)
            with patch.object(repository, "_fetch_dates", return_value=["2026-09-10"]):
                main.run_once(app)
            metadata = SchedulerControlApi(app).calendar_metadata()
            self.assertIsNone(metadata["refresh_error"])
            self.assertFalse(metadata["oracle_holiday_source_configured"])
            self.assertEqual(metadata["holiday_source"], "local_snapshot")
            self.assertEqual(metadata["holiday_count"], 1)
            self.assertFalse(app["holiday_evaluator"].is_working_day("2099-12-31"))
            self.assertEqual(holiday_path.read_bytes(), original)
            self.assertTrue(app["datemast"].available)
            self.assertFalse(app["execution_enabled"])

    def test_environment_can_explicitly_enable_extra_oracle_holidays(self):
        with patch.dict(os.environ, {"SCHEDULER_HOLIDAY_TABLE": "BANK_HOLIDAYS", "SCHEDULER_HOLIDAY_DATE_COLUMN": "BANK_DATE"}):
            connection = Mock()
            repository = OracleCalendarSnapshotRepository("unused-datemast.json", "unused-holidays.json",
                                                           connection_factory=lambda: connection)
            with patch.object(repository, "_fetch_dates", side_effect=[["2026-09-10"], ["2099-12-31"]]) as fetch:
                values = repository.read_calendar()
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(fetch.call_args.kwargs["table"], "BANK_HOLIDAYS")
            self.assertEqual(fetch.call_args.kwargs["date_column"], "BANK_DATE")
            self.assertEqual(values["holidays"], ["2099-12-31"])
            self.assertIsNone(values["warning"])
            app = self.application()
            app["calendar_snapshot_repository"] = repository
            self.assertTrue(SchedulerControlApi(app).calendar_metadata()["oracle_holiday_source_configured"])

    def test_monitor_startup_skips_recovery_and_starts_control_api(self):
        app = self.application()
        lock = Mock()
        lock.acquire.return_value = True
        with patch.object(main, "create_application", return_value=app), \
             patch.object(main, "SchedulerInstanceLock", return_value=lock), \
             patch.object(main, "_recover_orphaned_executions") as recover, \
             patch.object(main, "start_control_api") as start_api, \
             patch.object(main, "run_forever") as run, \
             patch.object(main, "close_application"):
            self.assertEqual(main.main(["--monitor"]), 0)
        recover.assert_not_called()
        start_api.assert_called_once()
        run.assert_called_once()
        self.assertFalse(app["execution_enabled"])
        lock.release.assert_called_once()

    def test_monitor_rejects_execution_or_no_api_flags(self):
        for args in (["--monitor", "--once"], ["--monitor", "--no-control-api"]):
            with self.subTest(args=args), patch("sys.stderr"), self.assertRaises(SystemExit):
                main._parse_arguments(args)

    def test_failed_monitor_cycle_publishes_failure_before_waiting(self):
        app = self.application()
        main.configure_monitor(app)
        app["scheduler"].run_cycle.side_effect = TypeError("test missing date")
        api = SchedulerControlApi(app)
        with patch.object(main.time, "sleep", side_effect=KeyboardInterrupt), \
             patch.object(main.logger, "exception"), self.assertRaises(KeyboardInterrupt):
            main.run_forever(app, api)
        self.assertEqual(api.last_cycle["scheduler"]["status"], "ERROR")
        self.assertEqual(api.last_cycle["error"]["type"], "TypeError")
        self.assertEqual(app["execution_manager"].mock_calls, [])
