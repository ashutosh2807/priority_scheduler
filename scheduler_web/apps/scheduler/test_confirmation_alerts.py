from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth.models import AnonymousUser
from django.template import Context
from django.template.loader import render_to_string
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import AdminUser
from apps.leave.models import LeaveRequest, LeaveStatus
from .file_adapter import SchedulerApiReadAdapter
from .models import ScheduleProfile
from .templatetags.confirmation_alerts import confirmation_alert


class ConfirmationAlertTests(TestCase):
    def setUp(self):
        self.day = timezone.localdate()
        self.primary = AdminUser.objects.create_user(username="alert-primary", employee_id="alert-1")
        self.backup = AdminUser.objects.create_user(username="alert-backup", employee_id="alert-2")
        ScheduleProfile.objects.create(schedule_id=1, primary_operator=self.primary, backup_operator=self.backup)
        self.entry = {"job_id": 1, "job_name": "Daily control", "state": "WAITING_CONFIRMATION",
                      "occurrence_key": "daily-current", "calendar_date": self.day.isoformat(),
                      "execution_date": self.day.isoformat(), "confirmation_needed": True,
                      "confirmation_confirmed": False, "is_projection": False}
        self.payload = {"occurrences": [self.entry]}
        worker = Mock()
        worker.snapshot.return_value = {
            "schedule_master": [{"id": 1, "name": "Daily control", "is_active": 1,
                                 "confirmation_needed": 1, "run_config": {"RUNS_ON": ["DAILY"]}}],
            "controls": [], "staging": [self.entry], "ready": [], "executions": [], "meta": {},
        }
        worker.calendar.return_value = self.payload
        self.adapter = SchedulerApiReadAdapter(client=worker)
        self.context = Context({"user": self.primary, "scheduler_adapter": self.adapter})

    def test_pending_alert_is_persistent_and_links_to_confirmation_list(self):
        result = confirmation_alert(self.context)
        self.assertEqual(result["confirmation_alert_count"], 1)
        self.assertEqual(result["confirmation_alert_mine"], 1)
        html = render_to_string("components/confirmation_alert.html", result)
        self.assertIn("1 schedule confirmation pending today", html)
        self.assertIn("1 assigned to you.", html)
        self.assertIn('href="/scheduler/confirmation/"', html)
        self.assertIn('role="status"', html)
        self.assertNotIn("alert-dismissible", html)

    def test_scoped_confirmation_and_terminal_states_are_excluded(self):
        ignored = ["SUCCESS", "COMPLETED", "FAILED", "RUNNING", "CANCELLED", "DISABLED", "MANUAL_REQUIRED", "RETRY_EXHAUSTED"]
        self.payload["occurrences"] = [
            {**self.entry, "occurrence_key": state, "state": state} for state in ignored
        ] + [
            {**self.entry, "occurrence_key": "confirmed", "confirmation_confirmed": True},
            {**self.entry, "occurrence_key": "projection", "is_projection": True},
            {**self.entry, "occurrence_key": "automated", "confirmation_needed": False},
            {**self.entry, "occurrence_key": "past", "calendar_date": (self.day-timedelta(days=1)).isoformat()},
            {**self.entry, "occurrence_key": "future", "calendar_date": (self.day+timedelta(days=1)).isoformat()},
            self.entry, dict(self.entry),
        ]
        self.assertEqual(confirmation_alert(self.context)["confirmation_alert_count"], 1)

    def test_confirming_removes_alert_without_waiting_for_worker_state_transition(self):
        self.entry["confirmation_confirmed"] = True
        result = confirmation_alert(self.context)
        self.assertEqual(result["confirmation_alert_count"], 0)
        self.assertNotIn("pending today", render_to_string("components/confirmation_alert.html", result))

    def test_leave_moves_personal_alert_to_available_cover(self):
        LeaveRequest.objects.create(admin=self.primary, start_date=self.day, end_date=self.day, status=LeaveStatus.APPROVED)
        self.assertEqual(confirmation_alert(self.context)["confirmation_alert_mine"], 0)
        self.context["user"] = self.backup
        self.assertEqual(confirmation_alert(self.context)["confirmation_alert_mine"], 1)

    def test_unassigned_gate_is_visible_for_coverage_action(self):
        ScheduleProfile.objects.filter(schedule_id=1).update(primary_operator=None)
        self.assertEqual(confirmation_alert(self.context)["confirmation_alert_unassigned"], 1)

    @patch("apps.scheduler.templatetags.confirmation_alerts.get_scheduler_read_adapter")
    def test_anonymous_and_inactive_accounts_do_not_read_or_show_alerts(self, factory):
        self.assertEqual(confirmation_alert(Context({"user": AnonymousUser()})), {})
        self.primary.is_active_admin = False
        self.assertEqual(confirmation_alert(self.context), {})
        factory.assert_not_called()

    def test_calendar_reuses_payload_without_another_worker_calendar_read(self):
        self.context.update({"calendar_range_start": self.day-timedelta(days=10),
                             "calendar_range_end": self.day+timedelta(days=10), "calendar_payload": self.payload})
        self.assertEqual(confirmation_alert(self.context)["confirmation_alert_count"], 1)
        self.adapter.client.calendar.assert_not_called()

    def test_unavailable_worker_does_not_present_fallback_as_live_alerts(self):
        adapter = Mock()
        adapter.get_status.return_value = {"label": "READ-ONLY DEVELOPMENT FALLBACK"}
        self.context["scheduler_adapter"] = adapter
        self.assertEqual(confirmation_alert(self.context), {})
        adapter.get_operations_calendar.assert_not_called()
