"""Verified-range banking-calendar regressions; no Oracle or runtime writes."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scheduler.datemast import DateMast
from scheduler.holiday import HolidayEvaluator


class DateMastCoverageTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 9, 12)

    def source(self, dates=None, **kwargs):
        return DateMast(
            report_dates=dates if dates is not None else ["2025-03-31", "2026-09-11"],
            today_provider=lambda: self.today,
            coverage_start="2025-03-31", coverage_end="2026-09-12", **kwargs,
        )

    def calendar(self, source):
        return HolidayEvaluator(datemast=source, today_provider=lambda: self.today)

    def test_absence_only_confirms_holiday_inside_loaded_range(self):
        source = self.source()
        calendar = self.calendar(source)
        self.assertTrue(source.covers_date("2025-03-31"))
        self.assertTrue(source.covers_date("2026-09-12"))
        self.assertFalse(source.covers_date("2025-03-28"))
        before = calendar.classify_date("2025-03-28")
        self.assertTrue(before["is_working_day"])
        self.assertTrue(before["is_provisional"])
        self.assertEqual(before["source"], "bank_calendar")
        self.assertIn("outside the loaded DATEMAST coverage", before["reason"])
        saturday = calendar.classify_date("2025-03-22")
        self.assertTrue(saturday["is_bank_holiday"])
        self.assertTrue(saturday["is_provisional"])
        self.assertEqual(saturday["kind"], "SAT")
        missing = calendar.classify_date("2025-04-01")
        self.assertTrue(missing["is_holiday"])
        self.assertFalse(missing["is_provisional"])
        self.assertEqual(missing["source"], "datemast")
        self.assertTrue(calendar.classify_date("2025-03-31")["is_working_day"])

    def test_range_does_not_expand_with_clock_and_t_minus_one_still_applies(self):
        source = self.source(["2026-09-11", "2026-09-12", "2026-10-01"])
        calendar = self.calendar(source)
        self.assertEqual(source.get_latest_report_date(), date(2026, 9, 11))
        self.assertFalse(source.has_report_date("2026-09-12"))
        self.assertFalse(source.latest_is_at_least("2026-09-12"))
        self.assertTrue(calendar.classify_date("2026-09-12")["is_provisional"])
        self.assertTrue(calendar.classify_date("2026-10-01")["is_provisional"])
        self.today = date(2026, 9, 16)
        self.assertFalse(source.covers_date("2026-09-14"))
        after = calendar.classify_date("2026-09-14")
        self.assertTrue(after["is_working_day"])
        self.assertTrue(after["is_provisional"])
        self.assertIn("outside the loaded DATEMAST coverage", after["reason"])

    def test_future_coverage_upper_bound_is_capped_to_read_day(self):
        source = DateMast(["2026-09-11"], today_provider=lambda: self.today,
                          coverage_start="2025-03-31", coverage_end="2027-03-31")
        self.assertEqual(source.coverage_end, date(2026, 9, 12))
        self.today = date(2026, 9, 16)
        self.assertEqual(source.coverage_end, date(2026, 9, 12))
        self.assertFalse(source.covers_date("2026-09-14"))

    def test_outage_retains_present_working_days_but_not_absence_inference(self):
        source = self.source(["2026-09-06", "2026-09-11"])
        source.available = False
        calendar = self.calendar(source)
        known_sunday = calendar.classify_date("2026-09-06")
        self.assertTrue(known_sunday["is_working_day"])
        self.assertFalse(known_sunday["is_provisional"])
        self.assertTrue(source.has_report_date("2026-09-11"))
        missing = calendar.classify_date("2026-09-10")
        self.assertTrue(missing["is_working_day"])
        self.assertTrue(missing["is_provisional"])
        self.assertIn("unavailable", missing["reason"])

    def test_invalid_replacement_preserves_dates_range_and_availability(self):
        source = self.source()
        expected = source.get_report_dates(), source.coverage_start, source.coverage_end
        source.available = False
        for start, end in (("2026-09-13", "2026-09-12"), (None, "2026-09-12"),
                           ("2025-03-31", None), ("invalid", "2026-09-12"),
                           ("2026-09-13", "2026-10-01")):
            with self.subTest(start=start, end=end), self.assertRaises((TypeError, ValueError)):
                source.replace_report_dates(["2026-09-10"], coverage_start=start, coverage_end=end)
            self.assertEqual((source.get_report_dates(), source.coverage_start, source.coverage_end), expected)
            self.assertFalse(source.available)

    def test_snapshot_reload_is_atomic_and_recovers_after_invalid_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "datemast.json"
            valid = {"report_dates": ["2026-09-11"], "coverage_start": "2025-03-31", "coverage_end": "2026-09-12"}
            path.write_text(json.dumps(valid), encoding="utf-8")
            source = DateMast(file_path=path, today_provider=lambda: self.today)
            expected = source.get_report_dates(), source.coverage_start, source.coverage_end
            for invalid in ({**valid, "coverage_start": "2027-01-01"},
                            {**valid, "coverage_end": None},
                            {**valid, "report_dates": ["invalid-date"]}):
                path.write_text(json.dumps(invalid), encoding="utf-8")
                self.assertFalse(source.reload(force=True))
                self.assertFalse(source.available)
                self.assertEqual((source.get_report_dates(), source.coverage_start, source.coverage_end), expected)
            self.today = date(2026, 9, 14)
            valid.update(report_dates=["2026-09-11", "2026-09-12"], coverage_end="2026-09-14")
            path.write_text(json.dumps(valid), encoding="utf-8")
            self.assertTrue(source.reload(force=True))
            self.assertTrue(source.available)
            self.assertEqual(source.coverage_end, date(2026, 9, 14))
            self.assertEqual(source.get_latest_report_date(), date(2026, 9, 12))

    def test_legacy_snapshot_and_explicit_replacement_clear_prior_coverage(self):
        source = self.source()
        source.replace_report_dates(["2026-09-11"])
        self.assertIsNone(source.coverage_start)
        self.assertIsNone(source.coverage_end)
        self.assertTrue(source.covers_date("2025-03-28"))
        self.assertFalse(self.calendar(source).classify_date("2025-03-28")["is_provisional"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "datemast.json"
            path.write_text(json.dumps({"report_dates": ["2026-09-11"], "coverage_start": "2025-03-31", "coverage_end": "2026-09-12"}))
            source = DateMast(file_path=path, today_provider=lambda: self.today)
            path.write_text('["2026-09-11"]')
            self.assertTrue(source.reload(force=True))
            self.assertIsNone(source.coverage_start)
            self.assertIsNone(source.coverage_end)


if __name__ == "__main__":
    unittest.main()
