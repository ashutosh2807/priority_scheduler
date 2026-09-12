"""Focused tests for the non-blocking Oracle Schedule_extg mirror."""

import tempfile
import unittest
from pathlib import Path

from repositories.oracle_schedule_extg_mirror import OracleScheduleExtgMirror
from repositories.schedule_extg_repository import ScheduleExtgRepository


class FakeCursor:
    def __init__(self):
        self.calls = []
        self.closed = False

    def execute(self, sql, values):
        self.calls.append((sql, dict(values)))

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class OracleExtgMirrorTests(unittest.TestCase):
    def test_mirror_upserts_by_stable_local_source_id_and_redacts_error(self):
        connection = FakeConnection()
        mirror = OracleScheduleExtgMirror(
            enabled=True,
            connection_factory=lambda: connection,
        )
        result = mirror.mirror({
            "id": 42,
            "name": "MONTH_END",
            "report_date": "2026-09-30",
            "status": "FAILED",
            "same_day": 0,
            "run_date": "2026-10-01T06:00:00",
            "error_info": "ORA-01017 password=not-for-oracle-mirror scheduler/not-for-oracle-mirror@db.internal",
            "count": None,
        })

        self.assertTrue(result)
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        sql, values = connection.cursor_instance.calls[0]
        self.assertIn("MERGE INTO SCHEDULE_EXTG", sql)
        self.assertEqual(values["source_record_id"], 42)
        self.assertEqual(values["status"], "FAILED")
        self.assertNotIn("not-for-oracle-mirror", values["error_info"])
        self.assertIn("password=***", values["error_info"])

    def test_mirror_failure_is_best_effort_and_does_not_raise(self):
        def unavailable_connection():
            raise RuntimeError("ORA-12541 password=not-for-output")

        mirror = OracleScheduleExtgMirror(
            enabled=True,
            connection_factory=unavailable_connection,
        )
        self.assertFalse(mirror.mirror({
            "id": 1, "name": "DAILY", "status": "PENDING", "same_day": 0,
        }))

    def test_local_status_lifecycle_completes_when_oracle_mirror_is_unavailable(self):
        class MirrorSpy:
            def __init__(self):
                self.statuses = []

            def mirror(self, record):
                self.statuses.append(record["status"])
                raise RuntimeError("simulated mirror outage")

        with tempfile.TemporaryDirectory() as directory:
            spy = MirrorSpy()
            repository = ScheduleExtgRepository(
                Path(directory) / "Schedule_extg.json",
                oracle_mirror=spy,
            )
            record = repository.create_pending("DAILY", report_date="2026-09-11")
            record = repository.mark_running(record_id=record["id"])
            record = repository.mark_success(record_id=record["id"], count=17)

            self.assertEqual(record["status"], "SUCCESS")
            self.assertEqual(spy.statuses, ["PENDING", "RUNNING", "SUCCESS"])
            self.assertEqual(repository.get_by_id(record["id"])["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
