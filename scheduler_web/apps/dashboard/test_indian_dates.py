from datetime import date, datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.template import Context, Template
from django.test import SimpleTestCase
from django.utils import timezone
from django.utils.safestring import mark_safe

from .templatetags.indian_dates import indian_date, indian_datetime


class IndianDateFormattingTests(SimpleTestCase):
    def test_business_dates_do_not_shift_or_acquire_a_time(self):
        with timezone.override("America/Los_Angeles"):
            for value in (date(2026, 3, 31), "2026-03-31", " 2026-03-31 "):
                with self.subTest(value=value):
                    self.assertEqual(indian_date(value), "31/03/2026")
                    self.assertEqual(indian_datetime(value), "31/03/2026")
        self.assertEqual(indian_date("2024-02-29"), "29/02/2024")

    def test_aware_timestamps_convert_across_midnight_in_both_directions(self):
        cases = (
            ("2026-09-11T22:45:29Z", "12/09/2026", "12/09/2026 04:15 IST"),
            ("2026-09-12T01:10:00+09:00", "11/09/2026", "11/09/2026 21:40 IST"),
            ("2026-09-11T23:15:00-04:00", "12/09/2026", "12/09/2026 08:45 IST"),
            (datetime(2026, 9, 11, 22, 45, tzinfo=datetime_timezone.utc),
             "12/09/2026", "12/09/2026 04:15 IST"),
            (datetime(2026, 9, 12, 1, 10, tzinfo=datetime_timezone(timedelta(hours=9))),
             "11/09/2026", "11/09/2026 21:40 IST"),
        )
        # Display must remain IST even when another request timezone is active.
        with timezone.override("America/Los_Angeles"):
            for value, expected_date, expected_datetime in cases:
                with self.subTest(value=value):
                    self.assertEqual(indian_date(value), expected_date)
                    self.assertEqual(indian_datetime(value), expected_datetime)

    def test_naive_worker_timestamps_are_local_ist_and_use_24_hour_clock(self):
        for value in (datetime(2026, 9, 12, 23, 5, 41), "2026-09-12T23:05:41",
                      "2026-09-12 23:05:41.123456", "2026-09-12T23:05:41+05:30"):
            with self.subTest(value=value):
                self.assertEqual(indian_date(value), "12/09/2026")
                self.assertEqual(indian_datetime(value), "12/09/2026 23:05 IST")

    def test_missing_and_non_date_values_are_graceful(self):
        for formatter in (indian_date, indian_datetime):
            for value in (None, "", "  "):
                self.assertEqual(formatter(value), "—")
            for value in ("Pending", "Awaiting DATEMAST", "2026-02-30", "unavailable"):
                self.assertEqual(formatter(value), value)

    def test_unrepresentable_timezone_conversion_does_not_break_rendering(self):
        value = "9999-12-31T23:59:00Z"
        self.assertEqual(indian_date(value), value)
        self.assertEqual(indian_datetime(value), value)

    def test_builtins_need_no_load_directive_and_do_not_reformat_iso_form_value(self):
        value = "2026-09-11T22:45:00Z"
        rendered = Template('{{ value|indian_date }} · {{ value|indian_datetime }} <input value="{{ value }}">').render(Context({"value": value}))
        self.assertEqual(rendered, '12/09/2026 · 12/09/2026 04:15 IST <input value="2026-09-11T22:45:00Z">')
        self.assertEqual(settings.TIME_ZONE, "Asia/Kolkata")
        self.assertTrue(settings.USE_TZ)

    def test_placeholders_remain_escaped_and_custom_defaults_are_supported(self):
        template = Template('{{ value|indian_date }} · {{ value|indian_datetime }}')
        for value in ('<script>alert("date")</script>', mark_safe('<b>Pending</b>')):
            rendered = template.render(Context({"value": value}))
            self.assertNotIn("<script>", rendered)
            self.assertNotIn("<b>", rendered)
            self.assertIn("&lt;", rendered)
        self.assertEqual(Template('{{ value|default:"Pending"|indian_date }}').render(Context({"value": None})), "Pending")
