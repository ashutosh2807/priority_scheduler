"""Real local repositories and a fake Oracle executor; no live bank operations."""
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from execution.execution_manager import ExecutionManager
from scheduler.retry_policy import RetryPolicy
import test_oracle_occurrence_runtime as fixture


class ExecutionDayRetryTests(unittest.TestCase):
    setUp = fixture.OracleOccurrenceRuntimeTests.setUp
    tearDown = fixture.OracleOccurrenceRuntimeTests.tearDown
    _job = staticmethod(fixture.OracleOccurrenceRuntimeTests._job)
    _runtime = fixture.OracleOccurrenceRuntimeTests._runtime

    def manager(self, attempts=6):
        job = self._job(runs_on=["DAILY"], time_flag=1, run_by={"FROM_TIME": "09:00", "TO_TIME": "10:00"})
        job.run_config["MAX_ATTEMPTS"] = attempts
        scheduler, staging, ready, controls, executions, queue = self._runtime(job, [date(2026, 8, 14)])
        scheduler.run_cycle(datetime(2026, 8, 17, 9, 1))
        oracle = Mock()
        oracle.execute.return_value = SimpleNamespace(success=False, count=0, duration_seconds=0, error="Fixture failure", error_type="TEST")
        manager = ExecutionManager(ready, queue, oracle, executions, job_control_repository=controls,
                                   schedule_master_repository=scheduler.schedule_master_repository,
                                   retry_policy=RetryPolicy())
        return manager, scheduler, ready, controls, executions, queue, oracle

    def test_failure_retries_at_midday_evening_and_before_midnight_after_initial_window(self):
        manager, scheduler, ready, controls, executions, queue, oracle = self.manager()
        self.assertTrue(manager.execute_next(datetime(2026, 8, 17, 9, 2))["executed"])
        for hour, minute in ((12, 0), (18, 0), (23, 59)):
            moment = datetime(2026, 8, 17, hour, minute)
            candidates = manager.prepare_retry_candidates(moment)
            self.assertEqual(len(candidates), 1)
            self.assertTrue(manager.execute_retry_candidates(candidates, moment)[0]["executed"])
        self.assertEqual(oracle.execute.call_count, 4)
        self.assertEqual(executions.get_latest(701, date(2026, 8, 14)).attempt_no, 4)

    def test_midnight_blocks_stale_ready_and_retry_candidate_but_manual_request_can_run(self):
        manager, scheduler, ready, controls, executions, queue, oracle = self.manager()
        manager.execute_next(datetime(2026, 8, 17, 9, 2))
        candidates = manager.prepare_retry_candidates(datetime(2026, 8, 17, 23, 59))
        tomorrow = datetime(2026, 8, 18, 0, 0)
        self.assertEqual(manager.prepare_retry_candidates(tomorrow), [])
        self.assertEqual(manager.execute_retry_candidates(candidates, tomorrow)[0]["status"], "MANUAL_REQUIRED")
        queue.rebuild(ready.get_all())
        self.assertEqual(manager.execute_next(tomorrow)["status"], "MANUAL_REQUIRED")
        self.assertEqual(oracle.execute.call_count, 1)
        controls.request_manual_run(701)
        queue.rebuild(ready.get_all())
        result = manager.execute_next(tomorrow)
        self.assertTrue(result["executed"])
        self.assertEqual(result["report_date"], date(2026, 8, 14))
        self.assertEqual(oracle.execute.call_count, 2)

    def test_task_attempt_limit_is_enforced_even_while_execution_day_is_open(self):
        manager, scheduler, ready, controls, executions, queue, oracle = self.manager(attempts=1)
        manager.execute_next(datetime(2026, 8, 17, 9, 2))
        candidates = manager.prepare_retry_candidates(datetime(2026, 8, 17, 12))
        result = manager.execute_retry_candidates(candidates, datetime(2026, 8, 17, 12))[0]
        self.assertTrue(result["retry_exhausted"])
        self.assertFalse(result["executed"])
        self.assertEqual(oracle.execute.call_count, 1)

    def test_cycle_holds_overdue_rows_and_manual_failure_does_not_extend_retry_day(self):
        manager, scheduler, ready, controls, executions, queue, oracle = self.manager()
        manager.execute_next(datetime(2026, 8, 17, 9, 2))
        moment = datetime(2026, 8, 18, 9, 3)
        scheduler.run_cycle(moment)
        self.assertEqual(queue.size(), 0)
        held = scheduler.staging_repository.get_by_key("701:2026-08-14")
        self.assertEqual(held.state, "MANUAL_REQUIRED")
        controls.request_manual_run(701)
        scheduler.run_cycle(moment)
        manual = ready.get_by_key("701:2026-08-14")
        self.assertEqual(str(manual.execution_date), "2026-08-17")
        self.assertTrue(manager.execute_next(moment)["executed"])
        self.assertEqual(manager.prepare_retry_candidates(moment), [])

    def test_custom_retry_windows_still_work_and_have_accurate_hint(self):
        policy = RetryPolicy(windows="11:00-12:00")
        self.assertTrue(policy.is_retry_window(datetime(2026, 8, 17, 11, 30)))
        self.assertFalse(policy.is_retry_window(datetime(2026, 8, 17, 12)))
        self.assertIn("11:00", policy.next_window_hint(datetime(2026, 8, 17, 12)))


if __name__ == "__main__":
    unittest.main()
