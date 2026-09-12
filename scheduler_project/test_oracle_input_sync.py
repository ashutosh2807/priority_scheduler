"""Tests for the Oracle-to-JSON scheduler input synchroniser.

These run without an Oracle instance.  Small DB-API doubles prove that the
sync validates all source data before a snapshot is replaced and that callers
retain a last-known-good Schedule Master when a later refresh fails.
"""

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from repositories.oracle_schedule_master_repository import (
    OracleCalendarSnapshotRepository,
    OracleMasterSyncError,
    OracleScheduleMasterRepository,
)
from repositories.schedule_master_repository import ScheduleMasterRepository


class FakeCursor:
    def __init__(self, master_rows=None, datemast_rows=None, holiday_rows=None):
        self.master_rows = master_rows or []
        self.datemast_rows = datemast_rows or []
        self.holiday_rows = holiday_rows or []
        self.description = []
        self.rows = []
        self.closed = False

    def execute(self, sql, parameters=None):
        if "SCHEDULE_EXTRACT_MASTER" in sql:
            self.description = [(name,) for name in (
                "ID", "NAME", "PACKAGE_NAME", "RUN_CONFIG", "MARGIN",
                "SAME_DAY", "IS_ACTIVE", "CREATED_DATE",
            )]
            self.rows = self.master_rows
        elif "DATEMAST" in sql:
            self.rows = self.datemast_rows
        elif "HOLIDAY_MASTER" in sql:
            self.rows = self.holiday_rows
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, **kwargs):
        self.cursor_instance = FakeCursor(**kwargs)
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class OracleInputSyncTests(unittest.TestCase):
    def test_master_snapshot_is_normalised_and_atomically_written(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "Schedule_Master.json"
            connection = FakeConnection(master_rows=[(
                7,
                "MONTH_END",
                "RPT.EXTRACTS.RUN_MONTH_END",
                json.dumps({
                    "runs_on": ["daily", "bi_annually", "on_specific_date"],
                    "specific_date": "2026-09-30",
                    "BY_TIME": {"FROM": "16:00:00", "TO": "22:00"},
                    "holiday_run": ["saturday", "holiday"],
                }),
                "+3",
                0,
                1,
                datetime(2026, 9, 11, 10, 30),
            )])
            repository = OracleScheduleMasterRepository(
                snapshot,
                connection_factory=lambda: connection,
            )

            records = repository.refresh_snapshot()
            self.assertTrue(connection.closed)
            self.assertEqual(records[0]["margin"], "T+3")
            self.assertEqual(records[0]["time_flag"], 1)
            self.assertEqual(records[0]["run_config"]["RUNS_ON"], ["DAILY", "HALF-YEARLY", "SPECIFIC_DATE"])
            self.assertEqual(records[0]["run_config"]["SPECIFIC_DATES"], ["2026-09-30"])
            self.assertEqual(records[0]["run_config"]["RUN_BY"], {"FROM_TIME": "16:00", "TO_TIME": "22:00"})
            self.assertEqual(records[0]["run_config"]["HOLIDAY_RUN"], ["SAT", "HOLIDAY"])
            self.assertEqual(json.loads(snapshot.read_text(encoding="utf-8")), records)

    def test_invalid_oracle_record_never_replaces_last_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "Schedule_Master.json"
            original = '[{"id": 1, "name": "LAST_GOOD"}]\n'
            snapshot.write_text(original, encoding="utf-8")
            connection = FakeConnection(master_rows=[(
                8, "BAD_CONFIG", "RPT.EXTRACTS.RUN", "{not-json", "T", 0, 1, date(2026, 9, 11),
            )])
            repository = OracleScheduleMasterRepository(snapshot, connection_factory=lambda: connection)

            with self.assertRaises(OracleMasterSyncError):
                repository.refresh_snapshot()
            self.assertEqual(snapshot.read_text(encoding="utf-8"), original)

    def test_calendar_snapshots_preserve_coverage_and_iso_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            datemast = Path(directory) / "datemaster.json"
            holidays = Path(directory) / "holiday_master.json"
            connection = FakeConnection(
                datemast_rows=[(datetime(2026, 9, 11, 0, 0),), (date(2026, 9, 10),), (date(2026, 9, 10),)],
                holiday_rows=[(date(2026, 9, 17),), (datetime(2026, 9, 15, 0, 0),)],
            )
            repository = OracleCalendarSnapshotRepository(
                datemast,
                holidays,
                connection_factory=lambda: connection,
                holiday_table="HOLIDAY_MASTER",
                today_provider=lambda: date(2026, 9, 12),
            )

            coverage = {"coverage_start": "2025-03-31", "coverage_end": "2026-09-12"}
            self.assertEqual(repository.refresh_snapshots(), {"datemast": 2, "holidays": 2, **coverage})
            self.assertEqual(json.loads(datemast.read_text(encoding="utf-8")), {"report_dates": ["2026-09-10", "2026-09-11"], **coverage})
            self.assertEqual(json.loads(holidays.read_text(encoding="utf-8")), ["2026-09-15", "2026-09-17"])

    def test_invalid_calendar_source_leaves_both_last_snapshots_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            datemast = Path(directory) / "datemaster.json"
            holidays = Path(directory) / "holiday_master.json"
            datemast.write_text('["2026-09-01"]\n', encoding="utf-8")
            holidays.write_text('["2026-09-02"]\n', encoding="utf-8")
            original_datemast = datemast.read_text(encoding="utf-8")
            original_holidays = holidays.read_text(encoding="utf-8")
            connection = FakeConnection(
                datemast_rows=[(date(2026, 9, 11),)],
                holiday_rows=[("not-a-date",)],
            )
            repository = OracleCalendarSnapshotRepository(
                datemast,
                holidays,
                connection_factory=lambda: connection,
                holiday_table="HOLIDAY_MASTER",
                today_provider=lambda: date(2026, 9, 12),
            )

            with self.assertRaises(OracleMasterSyncError):
                repository.refresh_snapshots()
            self.assertEqual(datemast.read_text(encoding="utf-8"), original_datemast)
            self.assertEqual(holidays.read_text(encoding="utf-8"), original_holidays)

    def test_datemast_only_refresh_preserves_existing_extra_holiday_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            datemast = Path(directory) / "datemaster.json"
            holidays = Path(directory) / "holiday_master.json"
            original = b'["2026-09-19"]\r\n'
            holidays.write_bytes(original)
            connection = FakeConnection(datemast_rows=[(date(2026, 9, 10),)])
            repository = OracleCalendarSnapshotRepository(
                datemast, holidays, connection_factory=lambda: connection, holiday_table="",
                today_provider=lambda: date(2026, 9, 12),
            )
            coverage = {"coverage_start": "2025-03-31", "coverage_end": "2026-09-12"}
            self.assertEqual(repository.refresh_snapshots(), {"datemast": 1, "holidays": None, **coverage})
            self.assertEqual(json.loads(datemast.read_text()), {"report_dates": ["2026-09-10"], **coverage})
            self.assertEqual(holidays.read_bytes(), original)
            self.assertTrue(connection.closed)

    def test_driver_error_is_sanitised_before_it_reaches_a_snapshot_caller(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "Schedule_Master.json"

            def failing_connection():
                raise RuntimeError("ORA-01017 password=never-display-this")

            repository = OracleScheduleMasterRepository(snapshot, connection_factory=failing_connection)
            with self.assertRaises(OracleMasterSyncError) as context:
                repository.refresh_snapshot()
            self.assertIn("authentication was rejected", str(context.exception))
            self.assertNotIn("never-display-this", str(context.exception))

    def test_schedule_repository_uses_last_known_snapshot_after_a_refresh_failure(self):
        class OracleSource:
            def __init__(self):
                self.calls = 0

            def refresh_snapshot(self):
                self.calls += 1
                if self.calls == 1:
                    return [{
                        "id": 1, "name": "DAILY", "package_name": "RPT.PKG.RUN",
                        "run_config": {"RUNS_ON": ["DAILY"]}, "margin": "T",
                        "same_day": 0, "time_flag": 0, "is_active": 1,
                    }]
                raise OracleMasterSyncError("Oracle listener is not reachable for scheduler input refresh.")

        with tempfile.TemporaryDirectory() as directory:
            source = OracleSource()
            repository = ScheduleMasterRepository(
                Path(directory) / "Schedule_Master.json",
                source="oracle",
                oracle_repository=source,
                refresh_seconds=0,
                allow_stale_snapshot=False,
            )
            self.assertEqual(repository.get_all()[0].name, "DAILY")
            self.assertEqual(repository.get_all()[0].name, "DAILY")
            self.assertEqual(source.calls, 2)
            self.assertIn("listener is not reachable", repository.last_refresh_error)


if __name__ == "__main__":
    unittest.main()
