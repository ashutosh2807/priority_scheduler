import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import database.sqlite_db as sqlite_db
from models.schedule_master import ScheduleMaster
from repositories.execution_repository import ExecutionRepository
from repositories.job_control_repository import JobControlRepository
from repositories.occurrence_repository import OccurrenceReadyRepository, OccurrenceStagingRepository
from repositories.operations_repository import OperationsRepository
from repositories.oracle_logging_repository import OracleLoggingRepository
from repositories.worker_logging import WorkerLoggingObserver, consume_manual_request, execution_context
from scheduler.eligibility import EligibilityResult
from scheduler.occurrence_scheduler import OccurrenceLifecycle
from scheduler.priority import PriorityCalculator


class WorkerLoggingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(sqlite_db, "SQLITE_DB", Path(self.temp.name) / "scheduler.db")
        self.db_patch.start()
        sqlite_db.create_tables()
        self.connection = sqlite_db.get_connection()
        self.logger = OracleLoggingRepository(self.connection, enabled=False)
        self.executions = ExecutionRepository(self.connection, event_logger=self.logger)
        self.operations = OperationsRepository(self.connection, event_logger=self.logger)
        self.controls = JobControlRepository(self.connection)
        self.staging = OccurrenceStagingRepository(self.connection)
        self.ready = OccurrenceReadyRepository(self.connection)
        self.lifecycle = OccurrenceLifecycle(self.staging, self.ready, PriorityCalculator(), event_logger=self.logger)
        self.job = ScheduleMaster(id=1, name="Bank report", package_name="PKG.RUN", run_config={"RUNS_ON": ["DAILY"], "MAX_ATTEMPTS": 5},
                                  margin="T", same_day=0, time_flag=0, is_active=1, confirmation_needed=0)
        self.master = Mock()
        self.master.get_all.return_value = [self.job]
        self.application = {"schedule_master_repository": self.master, "staging_repository": self.staging,
                            "ready_repository": self.ready, "job_control_repository": self.controls, "execution_enabled": False}
        self.observer = WorkerLoggingObserver(self.logger, self.connection)

    def tearDown(self):
        self.connection.close()
        self.db_patch.stop()
        self.temp.cleanup()

    def events(self):
        return [json.loads(row[0]) for row in self.connection.execute("SELECT event_json FROM scheduler_oracle_log_outbox ORDER BY event_seq")]

    def state(self, status="READY", **kwargs):
        return EligibilityResult(state=status, eligible=status == "READY", occurrence_key="1:2026-09-10",
                                 occurrence_date="2026-09-10", execution_date="2026-09-11", report_date="2026-09-10", **kwargs)

    def make_ready(self):
        self.lifecycle.synchronize(self.job, self.state(), datetime(2026, 9, 11, 9))

    def start(self):
        return self.executions.start_execution(1, self.job.name, self.job.package_name, "2026-09-10")

    def test_monitor_transitions_and_polling_are_durable_without_oracle(self):
        self.lifecycle.logging_source = "MONITOR"
        gate = self.state("WAITING_CONFIRMATION", confirmation_required=True, waiting_for="CONFIRMATION")
        self.lifecycle.synchronize(self.job, gate, datetime(2026, 9, 11, 8))
        self.lifecycle.synchronize(self.job, gate, datetime(2026, 9, 11, 8, 5))
        self.make_ready()
        self.lifecycle.synchronize(self.job, self.state(), datetime(2026, 9, 11, 9, 5))
        self.assertEqual([event["status"] for event in self.events()], ["WAITING_CONFIRMATION", "READY"])
        self.assertTrue(all(event["source"] == "MONITOR" for event in self.events()))
        self.assertEqual(self.staging.count(), 0)
        self.assertEqual(self.ready.count(), 1)
        self.assertEqual(self.logger.flush()["delivered"], 0)

    def test_lifecycle_outbox_failure_rolls_back_move_and_keeps_old_row(self):
        self.lifecycle.synchronize(self.job, self.state("STAGING", waiting_for="TIME_WINDOW"), datetime(2026, 9, 11, 8))
        before = len(self.events())
        with patch.object(self.logger, "capture_state", side_effect=RuntimeError("disk full")), self.assertRaises(RuntimeError):
            self.make_ready()
        self.assertEqual(self.staging.count(), 1)
        self.assertEqual(self.ready.count(), 0)
        self.assertEqual(len(self.events()), before)

    def test_attempt_details_survive_ready_cleanup_and_repeated_observation(self):
        self.make_ready()
        execution_id = self.start()
        self.executions.mark_success(execution_id, count=987, duration_seconds=1.25)
        before = len(self.events())
        self.observer.capture(self.application)
        self.observer.capture(self.application)
        self.ready.delete(1)
        self.observer.capture(self.application)
        self.assertEqual(len(self.events()), before)
        states = [event for event in self.events() if event["record_key"]]
        self.assertEqual(states[-1]["record_key"], "occurrence:1:2026-09-10")
        self.assertEqual(states[-1]["status"], "SUCCESS")
        self.assertEqual(states[-1]["payload"]["records_loaded"], 987)
        self.assertEqual(states[-1]["payload"]["attempt_no"], 1)
        self.assertTrue(states[-1]["payload"]["executed_at"])
        self.assertEqual(states[-1]["planned_execution_date"], "2026-09-11")
        self.assertEqual([event["event_type"] for event in self.events() if not event["record_key"]], ["EXECUTION_STARTED", "EXECUTION_SUCCESS"])

    def test_each_retry_has_distinct_attempt_and_failure_details(self):
        self.make_ready()
        for number in range(1, 3):
            execution_id = self.start()
            self.executions.mark_failed(execution_id, f"Failure {number}", "TestFailure")
        failures = [event for event in self.events() if event["event_type"] == "EXECUTION_FAILED"]
        self.assertEqual([item["payload"]["execution"]["attempt_no"] for item in failures], [1, 2])
        last = [event for event in self.events() if event["record_key"]][-1]
        self.assertEqual(last["payload"]["attempt_no"], 2)
        self.assertEqual(last["payload"]["error_info"], "Failure 2")

    def test_execution_outbox_failure_rolls_back_start_and_finish(self):
        self.make_ready()
        with patch.object(self.logger, "enqueue", side_effect=RuntimeError("disk full")), self.assertRaises(RuntimeError):
            self.start()
        self.assertEqual(self.executions.get_all(), [])
        execution_id = self.start()
        before = len(self.events())
        with patch.object(self.logger, "enqueue", side_effect=RuntimeError("disk full")), self.assertRaises(RuntimeError):
            self.executions.mark_failed(execution_id, "failure")
        self.assertEqual(self.executions.get_by_id(execution_id).status, "RUNNING")
        self.assertEqual(len(self.events()), before)

    def test_recovery_is_logged_and_repeating_recovery_is_noop(self):
        self.make_ready()
        self.start()
        self.assertEqual(self.executions.recover_running(), 1)
        before = len(self.events())
        self.assertEqual(self.executions.recover_running(), 0)
        self.assertEqual(len(self.events()), before)
        self.assertEqual(sum(event["event_type"] == "EXECUTION_RECOVERED" for event in self.events()), 1)

    def test_service_queue_and_control_transaction_roll_back_with_audit(self):
        self.operations.set_scheduler_enabled(True)
        with patch.object(self.logger, "enqueue", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.operations.set_scheduler_enabled(False)
            with self.assertRaises(RuntimeError):
                self.operations.set_queue_order(["1:2026-09-10"])
            with self.assertRaises(RuntimeError), self.controls.transaction():
                after = self.controls.pause(1)
                self.operations.record_audit(action="JOB_PAUSE", target_type="schedule_master", target_id=1,
                                             after_state=after, connection=self.connection, commit=False)
        self.assertTrue(self.operations.is_scheduler_enabled())
        self.assertEqual(self.operations.get_queue_order(), {})
        self.assertIsNone(self.controls.get(1))
        self.assertEqual(len(self.events()), 1)

    def test_scoped_confirmation_nested_transaction_does_not_commit_early(self):
        self.controls.ensure_job(1)
        with self.assertRaises(RuntimeError), self.controls.transaction():
            self.controls.set_occurrence_confirmation(1, "1:2026-09-10", True)
            with self.controls.transaction():
                self.controls.set_override_datetime(1, "2026-09-11T10:00:00")
            raise RuntimeError("audit failure")
        self.assertFalse(self.controls.get_for_occurrence(1, "1:2026-09-10")["confirmation"])
        self.assertIsNone(self.controls.get(1)["override_datetime"])

    def test_manual_consumption_logs_both_cleared_controls_atomically(self):
        self.controls.request_manual_run(1)
        self.controls.set_override_datetime(1, "2026-09-11T10:00:00")
        with patch.object(self.logger, "enqueue", side_effect=RuntimeError("disk full")), self.assertRaises(RuntimeError):
            consume_manual_request(self.logger, self.connection, 1)
        self.assertTrue(self.controls.get(1)["manual_run"])
        consume_manual_request(self.logger, self.connection, 1)
        self.assertFalse(self.controls.get(1)["manual_run"])
        self.assertIsNone(self.controls.get(1)["override_datetime"])
        self.assertEqual(self.events()[-1]["event_type"], "MANUAL_REQUEST_CONSUMED")
        self.assertEqual(self.events()[-1]["job_id"], 1)

    def test_effective_controls_and_delete_are_observed_once(self):
        self.make_ready()
        self.controls.pause(1)
        self.observer.capture(self.application)
        self.observer.capture(self.application)
        self.controls.cancel(1)
        self.observer.capture(self.application)
        self.controls.activate(1)
        self.job.is_active = 0
        self.observer.capture(self.application)
        self.observer.before_delete(1, actor="operator", reason="Retired")
        self.ready.delete(1)
        self.master.get_all.return_value = []
        before = len(self.events())
        self.observer.capture(self.application)
        self.assertEqual(len(self.events()), before)
        self.assertEqual([event["status"] for event in self.events()], ["READY", "PAUSED", "CANCELLED", "DISABLED", "DELETED"])

    def test_backfill_is_idempotent_and_preserves_existing_history(self):
        legacy = ExecutionRepository(self.connection)
        execution_id = legacy.start_execution(1, "Historic report", "PKG.RUN", "2026-09-10")
        legacy.mark_failed(execution_id, "Earlier failure")
        OperationsRepository(self.connection).record_audit(action="JOB_PAUSE", target_type="schedule_master", target_id=1, actor="operator")
        self.observer.backfill()
        first = self.events()
        self.observer.backfill()
        self.assertEqual(self.events(), first)
        self.assertEqual(sum(event["event_type"] == "EXECUTION_FAILED" for event in first), 1)
        self.assertEqual(sum(event["event_type"] == "JOB_PAUSE" for event in first), 1)

    def test_restart_after_hooked_attempts_does_not_replay_old_states(self):
        self.observer.backfill()
        self.make_ready()
        failed = self.start()
        self.executions.mark_failed(failed, "Try again")
        completed = self.start()
        self.executions.mark_success(completed, count=3)
        self.operations.record_audit(action="JOB_CONFIRM", target_type="occurrence", target_id="1:2026-09-10",
                                    after_state={"job_id": 1}, actor="operator", correlation_id="request-1")
        before = self.events()
        WorkerLoggingObserver(self.logger, self.connection).backfill()
        self.assertEqual(self.events(), before)
        self.assertEqual(before[-1]["job_id"], 1)
        self.assertEqual(before[-1]["report_date"], "2026-09-10")
        self.assertEqual(before[-1]["correlation_id"], "request-1")

    def test_unknown_manual_attempts_have_independent_current_records(self):
        for _ in range(2):
            execution_id = self.executions.start_execution(1, self.job.name, self.job.package_name)
            self.executions.mark_success(execution_id, count=1)
        keys = {event["record_key"] for event in self.events() if event["record_key"]}
        self.assertEqual(keys, {"manual-attempt:1", "manual-attempt:2"})

    def test_confirmation_is_actual_scoped_approval_and_keeps_requirement_separate(self):
        self.job.confirmation_needed = 1
        self.lifecycle.synchronize(self.job, self.state("WAITING_CONFIRMATION", confirmation_required=True,
                                   confirmation_status="PENDING"), datetime(2026, 9, 11, 8))
        self.observer.capture(self.application)
        self.assertFalse(self.events()[-1]["payload"]["confirmation"])
        self.assertTrue(self.events()[-1]["payload"]["confirmation_required"])
        self.controls.set_occurrence_confirmation(1, "1:2026-09-10", True)
        self.observer.capture(self.application)
        self.assertTrue(self.events()[-1]["payload"]["confirmation"])
        self.assertFalse(self.controls.get(1) and self.controls.get(1)["confirmation"])

    def test_context_retains_planned_date_after_pending_rows_are_removed(self):
        self.make_ready()
        self.ready.delete(1)
        context = execution_context(self.connection, 1, "2026-09-10")
        self.assertEqual(context["execution_date"], "2026-09-11")
        self.assertEqual(context["planned_execution_date"], "2026-09-11")

    def test_definition_changes_refresh_current_details_without_poll_duplicates(self):
        self.make_ready()
        self.job.run_config["MAX_ATTEMPTS"] = 12
        self.job.time_flag = 1
        self.observer.capture(self.application)
        current = self.events()[-1]
        self.assertEqual(current["status"], "READY")
        self.assertEqual(current["payload"]["run_config"]["MAX_ATTEMPTS"], 12)
        self.assertEqual(current["payload"]["time_flag"], 1)
        before = len(self.events())
        self.observer.capture(self.application)
        self.assertEqual(len(self.events()), before)

    def test_running_and_success_survive_controls_and_stale_pending_rows(self):
        self.make_ready()
        attempt = self.start()
        for status in ("RUNNING", "SUCCESS"):
            if status == "SUCCESS":
                self.executions.mark_success(attempt, count=42)
            before = len(self.events())
            self.controls.pause(1)
            self.observer.capture(self.application)
            self.controls.cancel(1)
            self.observer.capture(self.application)
            self.job.is_active = 0
            self.observer.capture(self.application)
            self.observer.before_delete(1)
            self.master.get_all.return_value = []
            self.observer.capture(self.application)
            self.observer.capture_decision({"job_id": 1, "report_date": "2026-09-10", "status": "MANUAL_REQUIRED"})
            self.assertEqual(self.events()[-1]["status"], status)
            self.assertEqual(len(self.events()), before)
            self.master.get_all.return_value = [self.job]
            self.job.is_active = 1
            self.controls.activate(1)

    def test_latest_attempt_wins_over_saved_older_success_and_failed_controls_apply(self):
        self.make_ready()
        completed = self.start()
        self.executions.mark_success(completed, count=7)
        # Simulate a preserved later attempt written before hooks were enabled,
        # while the observer still remembers the older successful execution.
        legacy = ExecutionRepository(self.connection)
        latest = legacy.start_execution(1, self.job.name, self.job.package_name, "2026-09-10")
        legacy.mark_failed(latest, "Latest failure")
        self.observer.capture(self.application)
        self.assertEqual(self.events()[-1]["status"], "FAILED")
        self.assertEqual(self.events()[-1]["payload"]["execution_id"], latest)
        self.assertEqual(self.events()[-1]["payload"]["error_info"], "Latest failure")
        self.controls.pause(1)
        self.observer.capture(self.application)
        self.assertEqual(self.events()[-1]["status"], "PAUSED")
        self.observer.before_delete(1)
        self.assertEqual(self.events()[-1]["status"], "DELETED")
        self.assertEqual(self.events()[-1]["payload"]["execution"]["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
