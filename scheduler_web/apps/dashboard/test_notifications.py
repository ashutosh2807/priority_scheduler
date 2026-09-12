from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase


class RuntimeNotificationTests(SimpleTestCase):
    def test_healthy_monitor_mode_does_not_show_a_calendar_warning(self):
        html = render_to_string("scheduler/_runtime_notice.html", {"scheduler": {
            "available": True, "execution_enabled": False, "cycle_status": "OK",
            "calendar": {"available": True, "refresh_error": None},
        }})
        self.assertIn("Monitoring mode", html)
        self.assertIn("Oracle procedure execution is disabled", html)
        self.assertNotIn("alert-warning", html)

    def test_actual_calendar_and_cycle_failures_remain_visible(self):
        html = render_to_string("scheduler/_runtime_notice.html", {"scheduler": {
            "cycle_status": "ERROR", "calendar": {
                "available": False, "refresh_error": "Oracle calendar refresh failed.",
            },
        }})
        self.assertIn("The last scheduling cycle failed", html)
        self.assertIn("Calendar refresh needs attention", html)
        self.assertIn('calendar-source-notice" open', html)
        self.assertIn("Oracle calendar refresh failed.", html)

    def test_manual_dialog_preserves_required_reason_and_optional_override(self):
        html = render_to_string("scheduler/job_detail.html", {
            "schedule": {"id": 1, "name": "Preview", "can_operate": True,
                         "occurrence": {"occurrence_key": "report-2026-09-10"}},
            "user": SimpleNamespace(is_authenticated=False),
            "controls_available": True, "execution_history": [], "csrf_token": "test-token",
        })
        dialog = html[html.index('<div class="modal fade manual-run-dialog"'):]
        self.assertIn('name="reason" rows="2" required maxlength="500"', dialog)
        self.assertIn('value="report-2026-09-10"', dialog)
        self.assertIn('name="override_datetime"', dialog)
        self.assertIn("<details><summary>Advanced timing (optional)</summary>", dialog)
        self.assertIn('data-bs-dismiss="modal"', dialog)
