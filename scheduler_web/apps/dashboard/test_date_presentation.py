from pathlib import Path
import re

from django.conf import settings
from django.template.loader import get_template, render_to_string
from django.test import RequestFactory, SimpleTestCase


class DatePresentationTests(SimpleTestCase):
    def test_date_pair_has_distinct_labels_icons_and_indian_values(self):
        html = render_to_string("components/date_pair.html", {
            "report_date": "2026-08-31", "planned_date": "2026-09-01",
        })
        for text in ("Report date", "Planned date", "31/08/2026", "01/09/2026",
                     "schedule-date--report", "schedule-date--planned", "bi-file-earmark-text", "bi-calendar2-check"):
            self.assertIn(text, html)
        self.assertNotIn("2026-08-31", html)
        self.assertNotIn("2026-09-01", html)

    def test_empty_dates_have_explicit_distinct_placeholders(self):
        html = render_to_string("components/date_pair.html", {"report_date": None, "planned_date": None})
        self.assertIn("Awaiting DATEMAST", html)
        self.assertIn("Pending", html)
        self.assertNotIn("None", html)

    def test_task_row_formats_display_and_keeps_transport_dates_iso(self):
        request = RequestFactory().get("/scheduler/day/", {"date": "2026-09-01"})
        html = render_to_string("scheduler/_day_table.html", {"rows": [{
            "id": 1, "name": "Daily reconciliation", "report_date": "2026-08-31",
            "execution_date": "2026-09-01", "occurrence_key": "1:2026-08-31",
            "actual_execution_date": "2026-09-01", "started_at": "2026-08-31T23:45:00Z",
            "finished_at": "2026-09-01T00:10:00Z", "tone": "success", "display_status": "Done",
            "timing": {"window_label": "All day", "label": "Completed"},
        }]}, request=request)
        self.assertIn("31/08/2026", html)
        self.assertIn("01/09/2026", html)
        self.assertIn("01/09/2026 05:15 IST", html)
        self.assertIn("01/09/2026 05:40 IST", html)
        self.assertIn("report_date=2026-08-31", html)
        self.assertNotIn("report_date=31/08/2026", html)

    def test_all_templates_compile_and_transport_attributes_stay_machine_readable(self):
        for path in (Path(settings.BASE_DIR) / "templates").rglob("*.html"):
            relative = path.relative_to(Path(settings.BASE_DIR) / "templates").as_posix()
            with self.subTest(template=relative):
                get_template(relative)
                text = path.read_text(encoding="utf-8")
                for tag in re.findall(r"<(?:input|a|strong)\b[^>]*>", text):
                    for attribute in re.findall(r'(?:value|href|data-countdown-target)="([^"]*)"', tag):
                        self.assertNotIn("|indian_", attribute)
