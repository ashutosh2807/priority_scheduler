import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from control_api import SchedulerControlApi, start_control_api
from scheduler.holiday import HolidayEvaluator


class ControlRepository:
    def __init__(self):
        self.record = {
            "job_id": 1,
            "control_status": "ACTIVE",
            "manual_run": 0,
            "confirmation": 0,
            "override_datetime": None,
            "updated_at": "2026-09-11T09:00:00",
        }

    def get_all(self):
        return [dict(self.record)]

    def get(self, job_id):
        if int(job_id) != int(self.record["job_id"]):
            return None
        return dict(self.record)

    def pause(self, job_id):
        self.record["control_status"] = "PAUSED"
        return dict(self.record)

    def resume(self, job_id):
        self.record["control_status"] = "ACTIVE"
        return dict(self.record)

    def cancel(self, job_id):
        self.record["control_status"] = "CANCELLED"
        return dict(self.record)

    def activate(self, job_id):
        self.record["control_status"] = "ACTIVE"
        return dict(self.record)

    def request_manual_run(self, job_id):
        self.record["manual_run"] = 1
        return dict(self.record)

    def clear_manual_run(self, job_id):
        self.record["manual_run"] = 0
        return dict(self.record)

    def confirm(self, job_id):
        self.record["confirmation"] = 1
        return dict(self.record)

    def clear_confirmation(self, job_id):
        self.record["confirmation"] = 0
        return dict(self.record)

    def reset(self, job_id):
        self.record.update({"control_status": "ACTIVE", "manual_run": 0, "confirmation": 0, "override_datetime": None})
        return dict(self.record)

    def set_override_datetime(self, job_id, value):
        self.record["override_datetime"] = str(value)
        return dict(self.record)

    def clear_override_datetime(self, job_id):
        self.record["override_datetime"] = None
        return dict(self.record)


class Repository:
    def __init__(self, records):
        self.records = records

    def get_all(self):
        return list(self.records)

    def get_by_id(self, record_id):
        return next((record for record in self.records if getattr(record, "id", None) == record_id), None)


class ControlApiTests(unittest.TestCase):
    def setUp(self):
        self.controls = ControlRepository()
        schedule = SimpleNamespace(
            id=1, name="DAILY_REPORT", package_name="PKG.DAILY_REPORT",
            run_config={"RUNS_ON": ["DAILY"]}, margin="T", same_day=1,
            time_flag=0, is_active=1, created_date="2026-09-11", confirmation_needed=0,
        )
        self.api = SchedulerControlApi({
            "schedule_master_repository": Repository([schedule]),
            "job_control_repository": self.controls,
            "staging_repository": Repository([]),
            "ready_repository": Repository([]),
            "priority_queue": Repository([]),
            "execution_repository": Repository([]),
        })

    def test_snapshot_serializes_the_scheduler_owned_read_model(self):
        snapshot = self.api.snapshot()
        self.assertEqual(snapshot["schedule_master"][0]["name"], "DAILY_REPORT")
        self.assertEqual(snapshot["controls"][0]["control_status"], "ACTIVE")

    def test_cycle_failure_is_visible_and_clears_after_success(self):
        self.api.record_cycle_failure(TypeError("Private diagnostic text"))
        meta = self.api.snapshot()["meta"]
        self.assertEqual(meta["cycle_status"], "ERROR")
        self.assertEqual(meta["last_cycle_error"]["type"], "TypeError")
        self.assertNotIn("Private diagnostic", json.dumps(meta))
        self.assertIsNotNone(meta["last_cycle"])
        self.api.record_cycle({"scheduler": {"ready_count": 2}})
        self.assertEqual(self.api.snapshot()["meta"]["cycle_status"], "OK")
        self.assertIsNone(self.api.snapshot()["meta"]["last_cycle_error"])

    def test_calendar_classifies_full_grid_across_year_boundary(self):
        self.api.application["holiday_evaluator"] = HolidayEvaluator(holidays=["2027-01-01"])
        payload = self.api.calendar("2026-12-28", 42)
        calendar = {day["date"]: day for day in payload["calendar_days"]}
        self.assertEqual(len(calendar), 42)
        self.assertEqual(payload["calendar_days"][-1]["date"], "2027-02-07")
        self.assertEqual(calendar["2026-12-31"]["kind"], "WORKING_DAY")
        self.assertEqual(calendar["2027-01-01"]["kind"], "HOLIDAY")
        self.assertTrue(calendar["2027-01-01"]["is_holiday"])
        self.assertEqual(calendar["2027-01-01"]["holiday_name"], "Holiday")
        self.assertEqual(calendar["2027-01-02"]["kind"], "WORKING_DAY")
        self.assertEqual(calendar["2027-01-03"]["kind"], "SUN")
        self.assertTrue(calendar["2027-01-02"]["is_working_day"])
        self.assertEqual(calendar["2027-01-02"]["day_label"], "Working Saturday")
        self.assertEqual(calendar["2027-01-09"]["kind"], "SAT")
        self.assertTrue(calendar["2027-01-09"]["is_holiday"])
        self.assertTrue(calendar["2027-01-04"]["is_working_day"])
        self.assertEqual(payload["calendar"]["holiday_count"], 1)

    def test_empty_holiday_snapshot_does_not_invent_holidays(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "holidays.json"
            source.write_text("[]", encoding="utf-8")
            self.api.application["holiday_evaluator"] = HolidayEvaluator(file_path=source)
            result = self.api.calendar("2027-01-01", 7)
            self.assertEqual(result["calendar"]["holiday_source"], "local_snapshot")
            self.assertEqual(result["calendar"]["holiday_count"], 0)
            self.assertFalse(any(day["kind"] == "HOLIDAY" for day in result["calendar_days"]))
            self.assertEqual(result["calendar_days"][0]["kind"], "WORKING_DAY")
            self.api.application["holiday_source"] = "retained_snapshot"
            result = self.api.calendar("2027-01-01", 7)
            self.assertEqual(result["calendar"]["holiday_source"], "retained_snapshot")
            self.assertEqual(source.read_text(encoding="utf-8"), "[]")

    def test_unconfigured_holiday_source_still_classifies_weekends(self):
        result = self.api.calendar("2027-01-02", 2)
        self.assertEqual(result["calendar"]["holiday_source"], "unavailable")
        self.assertEqual(result["calendar"]["holiday_count"], 0)
        self.assertEqual([day["kind"] for day in result["calendar_days"]], ["WORKING_DAY", "SUN"])
        self.assertIsNone(result["calendar"]["holiday_observed_through"])

    def test_calendar_query_cannot_advance_t_minus_one_observation_cutoff(self):
        from datetime import date, timedelta
        from scheduler.datemast import DateMast
        today = date.today()
        yesterday = today - timedelta(days=1)
        future = today + timedelta(days=100)
        self.api.application["datemast"] = DateMast(report_dates=[yesterday, future])
        result = self.api.calendar(future.isoformat(), 42)
        self.assertEqual(result["calendar"]["latest_report_date"], yesterday.isoformat())
        self.assertEqual(result["calendar"]["source_latest_report_date"], future.isoformat())
        self.assertEqual(result["calendar"]["holiday_observed_through"], yesterday.isoformat())
        self.assertEqual(result["calendar"]["holiday_rule_source"], "datemast_and_bank_calendar")
        self.assertTrue(all(day["is_provisional"] for day in result["calendar_days"]))
        self.assertTrue(all(day["source"] == "bank_calendar" for day in result["calendar_days"]))
        self.api.application["datemast"].available = False
        result = self.api.calendar(today.isoformat(), 1)
        self.assertFalse(result["calendar"]["holiday_inference_available"])
        self.assertIsNone(result["calendar"]["holiday_observed_through"])

    def test_http_control_requires_token_and_records_only_control_intent(self):
        previous_token = os.environ.get("SCHEDULER_API_TOKEN")
        os.environ["SCHEDULER_API_TOKEN"] = "test-token"
        server = start_control_api({}, self.api, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        if previous_token is None:
            self.addCleanup(os.environ.pop, "SCHEDULER_API_TOKEN", None)
        else:
            self.addCleanup(os.environ.__setitem__, "SCHEDULER_API_TOKEN", previous_token)

        url = f"http://127.0.0.1:{server.server_port}/v1/jobs/1/controls/pause"
        request = Request(
            url,
            data=json.dumps({}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "X-Scheduler-Token": "test-token"},
        )
        with urlopen(request, timeout=2) as response:  # nosec B310 -- loopback test server
            body = json.loads(response.read())
        self.assertEqual(body["control"]["control_status"], "PAUSED")
        self.assertEqual(self.controls.record["control_status"], "PAUSED")


if __name__ == "__main__":
    unittest.main()
