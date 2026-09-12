"""Compatibility of opt-in, read-only monitoring with upgraded worker state."""
import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .control_client import SchedulerApiError
from .file_adapter import SchedulerProjectReadAdapter, get_scheduler_read_adapter


class LocalReadFallbackTests(SimpleTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.master = root / "master.json"
        self.database = root / "scheduler.db"
        self.master.write_text(json.dumps([{
            "id": 1, "name": "Daily report", "is_active": 1,
            "confirmation_needed": 1, "run_config": {"RUNS_ON": ["DAILY"]},
        }]), encoding="utf-8")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                CREATE TABLE job_control (job_id INTEGER, control_status TEXT, confirmation INTEGER);
                INSERT INTO job_control VALUES (1, 'ACTIVE', 1);
                CREATE TABLE staging_jobs (job_id INTEGER, job_name TEXT, report_date TEXT, execution_date TEXT, state TEXT);
                CREATE TABLE ready_jobs (job_id INTEGER, job_name TEXT, report_date TEXT, execution_date TEXT, priority_key TEXT);
                CREATE TABLE execution_jobs (id INTEGER PRIMARY KEY, job_id INTEGER, job_name TEXT, report_date TEXT, started_at TEXT, status TEXT);
                INSERT INTO staging_jobs VALUES (1, 'Archived row', '2026-09-01', '2026-09-02', 'WAITING_CONFIRMATION');
            """)
        self.adapter = SchedulerProjectReadAdapter(self.master, self.database)

    def upgrade(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                CREATE TABLE staging_occurrences (occurrence_key TEXT PRIMARY KEY, job_id INTEGER, job_name TEXT, report_date TEXT, execution_date TEXT, state TEXT);
                CREATE TABLE ready_occurrences (occurrence_key TEXT PRIMARY KEY, job_id INTEGER, job_name TEXT, report_date TEXT, execution_date TEXT, priority_key TEXT);
                CREATE TABLE occurrence_confirmation (occurrence_key TEXT PRIMARY KEY, confirmed INTEGER);
            """)

    def test_legacy_state_is_visible_and_reads_do_not_change_database(self):
        before = self.database.read_bytes()
        payload = self.adapter.get_operations_calendar(date(2026, 9, 2), 1)
        self.assertEqual(len(payload["occurrences"]), 1)
        self.assertEqual(payload["occurrences"][0]["report_date"], "2026-09-01")
        self.assertFalse(payload["projection_available"])
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(self.adapter.get_operations_calendar(date(2026, 10, 1), 31)["occurrences"], [])

    def test_empty_upgraded_tables_do_not_resurrect_archived_legacy_rows(self):
        self.upgrade()
        self.assertEqual(self.adapter.get_staging(), [])
        self.assertEqual(self.adapter.get_operations_calendar(date(2026, 9, 1), 30)["occurrences"], [])

    def test_multiple_occurrences_and_scoped_confirmations_survive_local_reads(self):
        self.upgrade()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                INSERT INTO ready_occurrences VALUES ('1:2026-09-09', 1, 'Daily report', '2026-09-09', '2026-09-11', '[2, 1, 1]');
                INSERT INTO ready_occurrences VALUES ('1:2026-09-10', 1, 'Daily report', '2026-09-10', '2026-09-11', '[1, 1, 1]');
                INSERT INTO occurrence_confirmation VALUES ('1:2026-09-09', 1);
            """)
        before = self.database.read_bytes()
        ready = self.adapter.get_ready()
        self.assertEqual([row["occurrence"]["occurrence_key"] for row in ready], ["1:2026-09-10", "1:2026-09-09"])
        self.assertEqual(len(self.adapter.get_execution_plan(date(2026, 9, 11))), 2)
        self.assertEqual(self.adapter.get_status()["ready"], 2)
        self.assertFalse(ready[0]["control"]["confirmation"])
        self.assertTrue(ready[1]["control"]["confirmation"])
        selected = self.adapter.get_schedule_occurrence(1, "1:2026-09-09")
        self.assertEqual(selected["occurrence"]["report_date"], "2026-09-09")
        payload = self.adapter.get_operations_calendar(date(2026, 9, 11), 1)
        self.assertEqual(len(payload["occurrences"]), 2)
        self.assertEqual(self.database.read_bytes(), before)

    def test_late_latest_attempt_preserves_planned_day_and_actual_day(self):
        self.upgrade()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                INSERT INTO staging_occurrences VALUES ('1:2026-09-09', 1, 'Daily report', '2026-09-09', '2026-09-10', 'WAITING_CONFIRMATION');
                INSERT INTO execution_jobs VALUES (1, 1, 'Daily report', '2026-09-09', '2026-09-11T10:00:00', 'FAILED');
                INSERT INTO execution_jobs VALUES (2, 1, 'Daily report', '2026-09-09', '2026-09-11T10:00:00', 'SUCCESS');
                INSERT INTO occurrence_confirmation VALUES ('1:2026-09-09', 1);
            """)
        rows = self.adapter.get_operations_calendar(date(2026, 9, 10), 1)["occurrences"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "SUCCESS")
        self.assertEqual(rows[0]["execution_date"], "2026-09-10")
        self.assertEqual(rows[0]["actual_execution_date"], "2026-09-11")
        self.assertTrue(rows[0]["confirmation_confirmed"])
        self.assertEqual(self.adapter.get_operations_calendar(date(2026, 9, 11), 1)["occurrences"], [])

    @patch("apps.scheduler.file_adapter.SchedulerApiClient")
    def test_fallback_remains_opt_in_when_worker_is_unavailable(self, client_factory):
        client_factory.return_value.configured = True
        client_factory.return_value.snapshot.side_effect = SchedulerApiError("Worker offline")
        with override_settings(SCHEDULER_MASTER_PATH=self.master, SCHEDULER_STATE_DB_PATH=self.database,
                               SCHEDULER_ALLOW_LOCAL_READ_FALLBACK=False):
            self.assertEqual(get_scheduler_read_adapter().get_schedules(), [])
        with override_settings(SCHEDULER_MASTER_PATH=self.master, SCHEDULER_STATE_DB_PATH=self.database,
                               SCHEDULER_ALLOW_LOCAL_READ_FALLBACK=True):
            self.assertEqual(len(get_scheduler_read_adapter().get_schedules()), 1)
