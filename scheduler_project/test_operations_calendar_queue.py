"""Isolated operational UI contracts: no worker startup or Oracle connection."""

import json
import os
import threading
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from control_api import SchedulerControlApi, StaleOperationError, start_control_api
from execution.execution_manager import ExecutionManager
from models.staging_job import StagingJob
from repositories.operations_repository import OperationsRepository
from scheduler.upcoming import UpcomingPlanner
from scheduler_queue.priority_queue import PriorityQueue
from test_control_api import Repository
from test_oracle_occurrence_runtime import OracleOccurrenceRuntimeTests as RuntimeCase


class OperationsCalendarQueueTests(RuntimeCase):
    def api_for(self, job, needs_confirmation=False):
        job.confirmation_needed = int(needs_confirmation)
        runtime = self._runtime(job, [date(2026, 8, 14), date(2026, 8, 17)])
        scheduler, staging, ready, controls, executions, queue = runtime
        operations = OperationsRepository(self.connection)
        queue.order_provider = operations.get_queue_order
        planner = UpcomingPlanner(self.frequency, self.holidays, self.margin, self.time_window,
                                  self.planner, scheduler.datemast)
        application = {
            "schedule_master_repository": Repository([job]),
            "staging_repository": staging, "ready_repository": ready,
            "job_control_repository": controls, "execution_repository": executions,
            "priority_queue": queue, "operations_repository": operations,
            "datemast": scheduler.datemast, "upcoming_planner": planner,
        }
        scheduler.run_cycle(datetime(2026, 8, 17, 9, 0))
        return SchedulerControlApi(application), runtime, operations

    def test_reorder_survives_rebuild_and_rejects_stale_and_partial_views(self):
        api, runtime, operations = self.api_for(self._job())
        scheduler, _, ready, _, _, queue = runtime
        before = api.snapshot()
        desired = [row["occurrence_key"] for row in reversed(before["priority_queue"])]
        with self.assertRaises(StaleOperationError):
            api.reorder_queue(desired[:1], before["meta"]["queue_revision"])
        result = api.reorder_queue(desired, before["meta"]["queue_revision"], "employee-17", "Business priority")
        self.assertEqual([row["occurrence_key"] for row in result["priority_queue"]], desired)
        with self.assertRaises(StaleOperationError):
            api.reorder_queue(desired, before["meta"]["queue_revision"])
        scheduler.run_cycle(datetime(2026, 8, 17, 9, 3))
        self.assertEqual([row.occurrence_key for row in queue.get_all()], desired)
        restarted = PriorityQueue(order_provider=operations.get_queue_order)
        restarted.rebuild(ready.get_all())
        self.assertEqual([row.occurrence_key for row in restarted.get_all()], desired)
        self.assertEqual(operations.get_recent_audit()[0]["actor"], "employee-17")

    def test_confirmation_authorizes_only_selected_report_date(self):
        api, runtime, _ = self.api_for(self._job(), needs_confirmation=True)
        scheduler, staging, ready, controls, _, queue = runtime
        self.assertEqual(staging.count(), 2)
        with self.assertRaises(StaleOperationError):
            api.control(701, "confirm")
        api.control(701, "confirm", occurrence_key="701:2026-08-14", actor="checker")
        visible = {row["occurrence_key"]: row for row in api.snapshot()["staging"]}
        self.assertTrue(visible["701:2026-08-14"]["confirmation"])
        self.assertFalse(visible["701:2026-08-15"]["confirmation"])
        day = {row["occurrence_key"]: row for row in api.calendar("2026-08-17", 1)["occurrences"]}
        self.assertTrue(day["701:2026-08-14"]["confirmation"])
        self.assertFalse(day["701:2026-08-15"]["confirmation"])
        scheduler.run_cycle(datetime(2026, 8, 17, 9, 3))
        self.assertEqual([row.occurrence_key for row in ready.get_all()], ["701:2026-08-14"])
        self.assertEqual(staging.get_all()[0].state, "WAITING_CONFIRMATION")
        self.assertEqual(controls.get(701)["confirmation"], 0)
        self.assertEqual(controls.get_for_occurrence(701, "701:2026-08-15")["confirmation"], 0)
        api.control(701, "clear_confirmation", occurrence_key="701:2026-08-14")
        scheduler.run_cycle(datetime(2026, 8, 17, 9, 6))
        self.assertEqual(queue.size(), 0)
        self.assertEqual(staging.count(), 2)
        with self.assertRaises(StaleOperationError):
            api.control(701, "confirm", occurrence_key="701:2026-08-13")
        api.control(701, "confirm", occurrence_key="701:2026-08-14")
        api.control(701, "reset")
        self.assertFalse(controls.get_for_occurrence(701, "701:2026-08-14")["confirmation"])

    def test_calendar_contains_all_month_rows_and_labels_unpublished_datemast(self):
        api, _, _ = self.api_for(self._job())
        jobs = [self._job(job_id=i, runs_on=["DAILY"]) for i in range(1, 11)]
        api.application["schedule_master_repository"] = Repository(jobs)
        next_month = (date.today().replace(day=28) + timedelta(days=4)).replace(day=1)
        calendar = api.calendar(next_month.isoformat(), 31)
        expected = 10 * sum(day["is_working_day"] for day in calendar["calendar_days"])
        self.assertGreater(expected, 120)
        self.assertEqual(len(calendar["occurrences"]), expected)
        self.assertEqual(sum(day["total"] for day in calendar["days"]), expected)
        self.assertTrue(all(row["report_date"] is None for row in calendar["occurrences"]))
        self.assertTrue(all(row["availability"] == "awaiting_datemast" for row in calendar["occurrences"]))
        self.assertEqual(calendar["calendar"]["latest_report_date"], "2026-08-17")
        self.assertEqual(calendar["calendar"]["financial_year_end"], "03-31")

    def test_calendar_overlay_keeps_report_date_and_displays_day_state(self):
        api, _, _ = self.api_for(self._job(), needs_confirmation=True)
        result = api.calendar("2026-08-17", 1)
        self.assertEqual(len(result["occurrences"]), 2)
        self.assertEqual({row["report_date"] for row in result["occurrences"]}, {"2026-08-14", "2026-08-15"})
        self.assertTrue(all(row["status"] == "waiting_confirmation" for row in result["occurrences"]))
        api.control(701, "cancel")
        result = api.calendar("2026-08-17", 1)
        self.assertTrue(all(row["status"] == "cancelled" for row in result["occurrences"]))

    def test_execution_history_preserves_scheduled_and_actual_dates(self):
        api, _, _ = self.api_for(self._job())
        api.application["execution_repository"] = Repository([SimpleNamespace(
            id=1, job_id=701, job_name="CASH_POSITION_701", report_date="2026-08-14", status="SUCCESS",
            started_at="2026-08-18T09:00:00", finished_at="2026-08-18T09:02:00", duration_seconds=120,
        )])
        row = next(row for row in api.calendar("2026-08-17", 1)["occurrences"] if row["report_date"] == "2026-08-14")
        self.assertEqual(row["execution_date"], "2026-08-17")
        self.assertEqual(row["planned_execution_date"], "2026-08-17")
        self.assertEqual(row["actual_execution_date"], "2026-08-18")
        self.assertEqual(row["status"], "success")
        self.assertFalse(row["is_projection"])
        late_day = [row for row in api.calendar("2026-08-18", 1)["occurrences"] if row["report_date"] == "2026-08-14"]
        self.assertEqual(len(late_day), 1)
        self.assertEqual(late_day[0]["calendar_date"], "2026-08-18")
        self.assertEqual(late_day[0]["day_context"], "activity")
        self.assertEqual(late_day[0]["planned_execution_date"], "2026-08-17")
        spanning = [row for row in api.calendar("2026-08-17", 2)["occurrences"] if row["report_date"] == "2026-08-14"]
        self.assertEqual({row["calendar_date"] for row in spanning}, {"2026-08-17", "2026-08-18"})
        self.assertEqual(len(spanning), 2)
        with self.assertRaises(StaleOperationError):
            api.control(701, "confirm", occurrence_key="701:2026-08-14")

    def test_active_retry_status_wins_over_failed_history(self):
        api, runtime, _ = self.api_for(self._job())
        api.application["execution_repository"] = Repository([SimpleNamespace(
            id=1, job_id=701, job_name="CASH_POSITION_701", report_date="2026-08-14", status="FAILED",
            started_at="2026-08-17T09:00:00", finished_at="2026-08-17T09:02:00", error="Test failure",
        )])
        result = api.calendar("2026-08-17", 1)
        row = next(row for row in result["occurrences"] if row["report_date"] == "2026-08-14")
        self.assertEqual(row["state"], "READY")
        self.assertEqual(row["latest_attempt_status"], "FAILED")
        self.assertTrue(row["retry_pending"])
        self.assertEqual(row["day_context"], "scheduled_activity")
        today = api.calendar(date.today().isoformat(), 1)
        self.assertEqual(len([row for row in today["occurrences"] if row["occurrence_key"] == "701:2026-08-14"]), 1)
        runtime[-1].remove(occurrence_key="701:2026-08-14")
        row = next(row for row in api.calendar("2026-08-17", 1)["occurrences"] if row["report_date"] == "2026-08-14")
        self.assertEqual(row["state"], "FAILED")
        self.assertFalse(row["retry_pending"])

    def test_running_overnight_occurrence_is_visible_on_each_active_day(self):
        api, runtime, _ = self.api_for(self._job())
        runtime[-1].remove(occurrence_key="701:2026-08-14")
        api.application["execution_repository"] = Repository([SimpleNamespace(
            id=2, job_id=701, job_name="CASH_POSITION_701", report_date="2026-08-14", status="RUNNING",
            started_at="2026-08-18T23:00:00", finished_at=None,
        )])
        rows = [row for row in api.calendar("2026-08-18", 2)["occurrences"] if row["report_date"] == "2026-08-14"]
        self.assertEqual([row["calendar_date"] for row in rows], ["2026-08-18", "2026-08-19"])
        self.assertTrue(all(row["state"] == "RUNNING" for row in rows))
        self.assertTrue(all(row["planned_execution_date"] == "2026-08-17" for row in rows))

    def test_http_calendar_and_queue_reorder_contract(self):
        api, _, _ = self.api_for(self._job())
        with patch.dict(os.environ, {"SCHEDULER_API_TOKEN": "calendar-test-token"}):
            server = start_control_api({}, api, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        headers = {"X-Scheduler-Token": "calendar-test-token", "Content-Type": "application/json"}
        with urlopen(Request(base + "/v1/operations/calendar?start_date=2026-08-17&days=1", headers=headers), timeout=2) as response:
            self.assertEqual(len(json.load(response)["occurrences"]), 2)
        snapshot = api.snapshot(include_upcoming=False)
        keys = [row["occurrence_key"] for row in reversed(snapshot["priority_queue"])]
        body = json.dumps({"occurrence_keys": keys, "queue_revision": snapshot["meta"]["queue_revision"]}).encode()
        request = Request(base + "/v1/queue/reorder", data=body, headers=headers, method="POST")
        with urlopen(request, timeout=2) as response:
            self.assertEqual([row["occurrence_key"] for row in json.load(response)["priority_queue"]], keys)
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

    def test_monitor_confirmation_immediately_re_evaluates_real_queue(self):
        from main import configure_monitor
        api, runtime, _ = self.api_for(self._job(), needs_confirmation=True)
        api.application["scheduler"] = runtime[0]
        api.application["execution_manager"] = SimpleNamespace()
        configure_monitor(api.application)
        self.assertEqual(runtime[-1].size(), 0)
        with patch("main.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 17, 9, 3)
            api.control(701, "confirm", occurrence_key="701:2026-08-14")
        self.assertTrue(runtime[-1].contains(occurrence_key="701:2026-08-14"))
        self.assertEqual(api.snapshot()["meta"]["mode"], "monitor")
        self.assertFalse(api.snapshot()["meta"]["execution_enabled"])
        self.assertEqual(runtime[4].get_all(), [])

    def test_migrated_null_date_rows_do_not_abort_or_invent_report_dates(self):
        api, runtime, _ = self.api_for(self._job())
        scheduler, staging, ready, _, _, queue = runtime
        staging.save(StagingJob(occurrence_key="701:legacy", job_id=701, job_name="LEGACY_PLACEHOLDER", state="STAGING",
                               calculated_at="2026-08-17T08:00:00", updated_at="2026-08-17T08:00:00"))
        ready.delete_by_key("701:2026-08-14")
        staging.save(StagingJob(occurrence_key="701:2026-08-14", job_id=701,
                               job_name="PARTIAL_LEGACY", state="STAGING", occurrence_date="2026-08-14",
                               calculated_at="2026-08-17T08:00:00", updated_at="2026-08-17T08:00:00"))
        scheduler.run_cycle(datetime(2026, 8, 17, 9, 0))
        placeholder = staging.get_by_key("701:legacy")
        self.assertIsNotNone(placeholder)
        self.assertIsNone(placeholder.report_date)
        self.assertIsNone(placeholder.execution_date)
        self.assertEqual(placeholder.waiting_for, "OCCURRENCE_CONTEXT")
        self.assertFalse(queue.contains(occurrence_key="701:legacy"))
        repaired = ready.get_by_key("701:2026-08-14")
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.report_date, "2026-08-14")
        self.assertEqual(repaired.execution_date, "2026-08-17")
        self.assertEqual(queue.size(), 2)

    def test_monitoring_reads_running_state_during_external_oracle_call(self):
        api, runtime, _ = self.api_for(self._job())
        _, _, ready, controls, executions, queue = runtime
        started, finish = threading.Event(), threading.Event()

        class BlockingOracle:
            def execute(self, **kwargs):
                started.set()
                if not finish.wait(4):
                    raise RuntimeError("Test did not release fake Oracle")
                return SimpleNamespace(success=True, count=1, duration_seconds=0.1, error=None, error_type=None)

        manager = ExecutionManager(ready_repository=ready, priority_queue=queue,
                                   oracle_executor=BlockingOracle(), execution_repository=executions,
                                   job_control_repository=controls,
                                   schedule_master_repository=api.application["schedule_master_repository"])
        errors = []

        def run():
            try:
                with api.lock:
                    manager.oracle_io_lock = api.lock
                    manager.execute_next(datetime(2026, 8, 17, 9, 1))
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertTrue(api.lock.acquire(timeout=1), "Control lock must be available during Oracle IO")
            try:
                snapshot = api.snapshot(include_upcoming=False)
                self.assertEqual(snapshot["executions"][0]["status"], "RUNNING")
                self.assertEqual(len(snapshot["priority_queue"]), 1)
                api.control(701, "pause")
                self.assertEqual(api.snapshot(include_upcoming=False)["priority_queue"], [])
            finally:
                api.lock.release()
        finally:
            finish.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
