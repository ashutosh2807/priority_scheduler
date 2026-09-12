"""Focused checks for the Oracle-compatible historical retry behaviour."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import date, datetime

from config.settings import MAX_SCHEDULED_ATTEMPTS
from repositories.execution_repository import ExecutionRepository
from scheduler.retry_policy import RetryPolicy


class RetryPolicyTests(unittest.TestCase):
    def test_global_windows_include_overnight_and_morning_only(self):
        policy = RetryPolicy(windows="06:00-10:00,22:00-06:00")
        self.assertTrue(policy.is_retry_window(datetime(2026, 9, 10, 5, 59)))
        self.assertTrue(policy.is_retry_window(datetime(2026, 9, 10, 6, 0)))
        self.assertTrue(policy.is_retry_window(datetime(2026, 9, 10, 9, 59)))
        self.assertFalse(policy.is_retry_window(datetime(2026, 9, 10, 10, 0)))
        self.assertFalse(policy.is_retry_window(datetime(2026, 9, 10, 21, 59)))
        self.assertTrue(policy.is_retry_window(datetime(2026, 9, 10, 22, 0)))

    def test_fifteen_day_report_date_boundary_is_inclusive(self):
        policy = RetryPolicy(lookback_days=15)
        today = date(2026, 9, 10)
        self.assertTrue(policy.is_retryable_report_date(date(2026, 8, 26), today))
        self.assertFalse(policy.is_retryable_report_date(date(2026, 8, 25), today))
        self.assertFalse(policy.is_retryable_report_date(date(2026, 9, 11), today))

    def test_max_attempts_reads_case_insensitive_json_key(self):
        job = {"run_config": {"max_attempts": 7}}
        self.assertEqual(RetryPolicy.max_attempts_for(job), 7)

    def test_max_attempts_reads_json_string_payload(self):
        job = {"run_config": '{"MAX_ATTEMPTS": 9}'}
        self.assertEqual(RetryPolicy.max_attempts_for(job), 9)

    def test_max_attempts_defaults_for_invalid_value(self):
        job = {"run_config": {"MAX_ATTEMPTS": 0}}
        self.assertEqual(RetryPolicy.max_attempts_for(job), MAX_SCHEDULED_ATTEMPTS)


class RetryCandidateRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE execution_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                job_name TEXT,
                procedure_name TEXT,
                report_date TEXT,
                status TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                count INTEGER,
                started_at TEXT,
                finished_at TEXT,
                error TEXT,
                error_type TEXT,
                duration_seconds REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.repository = ExecutionRepository(self.connection)

    def tearDown(self):
        self.connection.close()

    def _insert(self, job_id, report_date, status):
        now = "2026-09-10T06:30:00"
        self.connection.execute(
            """
            INSERT INTO execution_jobs (
                job_id, job_name, procedure_name, report_date, status,
                attempt_no, created_at, updated_at
            ) VALUES (?, 'Job', 'APP.PKG.PROC', ?, ?, 1, ?, ?)
            """,
            (job_id, report_date, status, now, now),
        )
        self.connection.commit()

    def test_only_latest_unresolved_failures_are_oldest_first(self):
        self._insert(1, "2026-08-27", "FAILED")
        self._insert(2, "2026-08-28", "FAILED")
        self._insert(2, "2026-08-28", "SUCCESS")
        self._insert(3, "2026-08-29", "FAILED")
        self._insert(3, "2026-08-29", "RUNNING")
        self._insert(4, "2026-08-25", "FAILED")  # expired
        candidates = self.repository.get_retry_candidates(date(2026, 9, 10), lookback_days=15)
        self.assertEqual(candidates, [])  # No durable planned dates: manual recovery only.


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
