"""Bounded Oracle DATEMAST query tests; all data and clocks are injected."""

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from repositories.oracle_schedule_master_repository import OracleCalendarSnapshotRepository, OracleMasterSyncError


class CalendarConnection:
    def __init__(self, *, datemast=(), holidays=(), fail=False):
        self.datemast = list(datemast)
        self.holidays = list(holidays)
        self.fail = fail
        self.calls = []
        self.fetched = []
        self.closed = False
        self.cursors = []

    def cursor(self):
        cursor = CalendarCursor(self)
        self.cursors.append(cursor)
        return cursor

    def close(self):
        self.closed = True


class CalendarCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.closed = False

    def execute(self, sql, parameters=None):
        self.connection.calls.append((sql, parameters))
        if self.connection.fail:
            raise RuntimeError("Oracle read failed password=not-for-output")
        # Model Oracle's WHERE predicate before returning rows: this catches
        # implementations that load the whole table and filter in Python.
        if parameters is not None:
            self.rows = [(value,) for value in self.connection.datemast
                         if parameters["date_from"] <= self.as_datetime(value) < parameters["date_until"]]
        else:
            self.rows = [(value,) for value in self.connection.holidays]

    def fetchall(self):
        self.connection.fetched.append(list(self.rows))
        return list(self.rows)

    def close(self):
        self.closed = True

    @staticmethod
    def as_datetime(value):
        return value if isinstance(value, datetime) else datetime.combine(value, datetime.min.time())


class OracleDateMastRangeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.datemast_path = self.directory / "datemaster.json"
        self.holiday_path = self.directory / "holidays.json"

    def repository(self, connection, today, **kwargs):
        return OracleCalendarSnapshotRepository(
            self.datemast_path, self.holiday_path, connection_factory=lambda: connection,
            today_provider=lambda: today, holiday_table=kwargs.pop("holiday_table", ""), **kwargs,
        )

    def test_query_fetches_only_previous_fiscal_boundary_through_all_of_today(self):
        today = date(2026, 9, 12)
        connection = CalendarConnection(datemast=[
            date(1990, 1, 1), datetime(2025, 3, 30, 23, 59, 59),
            datetime(2025, 3, 31), date(2026, 9, 11),
            datetime(2026, 9, 12, 23, 59, 59), datetime(2026, 9, 13), date(2099, 1, 1),
        ])
        result = self.repository(connection, today).read_calendar()
        self.assertEqual(result["report_dates"], ["2025-03-31", "2026-09-11", "2026-09-12"])
        self.assertEqual((result["coverage_start"], result["coverage_end"]), ("2025-03-31", "2026-09-12"))
        sql, values = connection.calls[0]
        self.assertIn("SELECT DISTINCT REPORT_DATE FROM DATEMAST", sql)
        self.assertIn("REPORT_DATE >= :date_from AND REPORT_DATE < :date_until", sql)
        self.assertNotIn("TRUNC", sql.upper())
        self.assertNotIn("2025", sql)
        self.assertNotIn("2026", sql)
        self.assertEqual(values, {"date_from": datetime(2025, 3, 31), "date_until": datetime(2026, 9, 13)})
        self.assertEqual(len(connection.fetched[0]), 3)
        self.assertTrue(connection.closed)
        self.assertTrue(all(cursor.closed for cursor in connection.cursors))
        self.assertFalse(self.datemast_path.exists())

    def test_rolling_fiscal_boundary_changes_on_april_first(self):
        for today, start in (
            (date(2026, 1, 1), date(2024, 3, 31)),
            (date(2026, 3, 31), date(2024, 3, 31)),
            (date(2026, 4, 1), date(2025, 3, 31)),
            (date(2028, 2, 29), date(2026, 3, 31)),
            (date(2030, 12, 31), date(2029, 3, 31)),
        ):
            with self.subTest(today=today):
                connection = CalendarConnection()
                result = self.repository(connection, today).read_calendar()
                values = connection.calls[0][1]
                self.assertEqual(values["date_from"], datetime.combine(start, datetime.min.time()))
                self.assertEqual(values["date_until"], datetime.combine(today + timedelta(days=1), datetime.min.time()))
                self.assertEqual(result["coverage_start"], start.isoformat())
                self.assertEqual(result["coverage_end"], today.isoformat())

    def test_custom_datemast_column_is_bound_and_future_holiday_source_is_unrestricted(self):
        connection = CalendarConnection(datemast=[date(2026, 9, 11)], holidays=[date(2099, 12, 31)])
        result = self.repository(connection, date(2026, 9, 12), datemast_table="BANK.REPORT_DAYS",
                                 datemast_date_column="AS_OF_DATE", holiday_table="BANK.HOLIDAYS",
                                 holiday_date_column="HOLIDAY_DATE").read_calendar()
        datemast_sql, binds = connection.calls[0]
        self.assertIn("SELECT DISTINCT AS_OF_DATE FROM BANK.REPORT_DAYS", datemast_sql)
        self.assertIn("AS_OF_DATE >= :date_from AND AS_OF_DATE < :date_until", datemast_sql)
        self.assertIsNotNone(binds)
        holiday_sql, holiday_binds = connection.calls[1]
        self.assertIn("FROM BANK.HOLIDAYS", holiday_sql)
        self.assertIsNone(holiday_binds)
        self.assertNotIn("date_from", holiday_sql)
        self.assertEqual(result["holidays"], ["2099-12-31"])

    def test_snapshot_persists_dates_and_coverage_together(self):
        today = date(2026, 9, 12)
        connection = CalendarConnection(datemast=[date(2026, 9, 11)])
        self.holiday_path.write_text('["2099-12-31"]', encoding="utf-8")
        result = self.repository(connection, today).refresh_snapshots()
        self.assertEqual(json.loads(self.datemast_path.read_text()), {
            "report_dates": ["2026-09-11"], "coverage_start": "2025-03-31", "coverage_end": "2026-09-12",
        })
        self.assertEqual(result, {"datemast": 1, "holidays": None,
                                  "coverage_start": "2025-03-31", "coverage_end": "2026-09-12"})
        self.assertEqual(json.loads(self.holiday_path.read_text()), ["2099-12-31"])

    def test_failed_read_or_atomic_replace_preserves_old_dates_and_coverage(self):
        original = {"report_dates": ["2025-03-31"], "coverage_start": "2024-03-31", "coverage_end": "2026-03-31"}
        self.datemast_path.write_text(json.dumps(original), encoding="utf-8")
        self.holiday_path.write_text('["2099-12-31"]', encoding="utf-8")
        old_datemast, old_holidays = self.datemast_path.read_bytes(), self.holiday_path.read_bytes()
        failed = CalendarConnection(fail=True)
        with self.assertRaises(OracleMasterSyncError) as error:
            self.repository(failed, date(2026, 4, 1)).refresh_snapshots()
        self.assertNotIn("not-for-output", str(error.exception))
        self.assertEqual(self.datemast_path.read_bytes(), old_datemast)
        self.assertEqual(self.holiday_path.read_bytes(), old_holidays)
        with patch("repositories.oracle_schedule_master_repository.os.replace", side_effect=OSError("file busy")):
            with self.assertRaises(OracleMasterSyncError):
                self.repository(CalendarConnection(datemast=[date(2026, 4, 1)]), date(2026, 4, 1)).refresh_snapshots()
        self.assertEqual(self.datemast_path.read_bytes(), old_datemast)
        self.assertEqual(self.holiday_path.read_bytes(), old_holidays)

    def test_one_clock_read_keeps_bounds_consistent_across_midnight(self):
        values = iter([date(2026, 3, 31), date(2026, 4, 1)])
        connection = CalendarConnection()
        repository = self.repository(connection, date(2026, 3, 31))
        repository.today_provider = lambda: next(values)
        first = repository.read_calendar()
        second = repository.read_calendar()
        self.assertEqual((first["coverage_start"], first["coverage_end"]), ("2024-03-31", "2026-03-31"))
        self.assertEqual((second["coverage_start"], second["coverage_end"]), ("2025-03-31", "2026-04-01"))


if __name__ == "__main__":
    unittest.main()
