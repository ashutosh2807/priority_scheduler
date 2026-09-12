from datetime import date
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import AdminUser
from .control_client import SchedulerApiError
from .file_adapter import SchedulerApiReadAdapter


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class QueueDiagnosticsTests(TestCase):
    def setUp(self):
        self.today = date(2026, 9, 12)
        self.user = AdminUser.objects.create_user(username="queue-check", employee_id="Q1", password="test-only")
        self.client.force_login(self.user)
        self.snapshot = {
            "schedule_master": [
                {"id": number, "name": f"Schedule {number}", "is_active": int(number == 1),
                 "confirmation_needed": 0, "run_config": {"RUNS_ON": ["DAILY"]}}
                for number in range(1, 11)
            ],
            "controls": [], "ready": [], "priority_queue": [], "executions": [],
            "staging": [{"job_id": 1, "job_name": "Schedule 1", "state": "STAGING",
                         "occurrence_key": "1:2026-09-11", "report_date": "2026-09-11",
                         "execution_date": "2026-09-12", "waiting_for": "CALENDAR_DAY",
                         "reason": "This task is not configured to run on a closed bank Saturday."}],
            "meta": {"cycle_status": "OK", "mode": "monitor", "execution_enabled": False},
            "service_control": {"scheduler_enabled": True},
        }
        self.payload = {"occurrences": [], "calendar_days": [
            {"date": self.today.isoformat(), "day_label": "2nd Saturday",
             "reason": "Second or fourth Saturday bank holiday.", "is_provisional": True},
        ]}
        self.worker = Mock()
        self.worker.snapshot.return_value = self.snapshot
        self.worker.calendar.return_value = self.payload

    def response(self, query=None, alert=True):
        adapter = SchedulerApiReadAdapter(client=self.worker)
        with patch("apps.scheduler.views.get_scheduler_read_adapter", return_value=adapter), \
             patch("apps.scheduler.views.timezone.localdate", return_value=self.today), \
             patch("apps.scheduler.templatetags.confirmation_alerts.is_operator", return_value=alert):
            return self.client.get(reverse("scheduler:queue"), query or {})

    def test_empty_queue_explains_counts_real_gate_and_calendar_with_one_read(self):
        response = self.response()
        self.assertContains(response, "10 configured")
        self.assertContains(response, "1 active")
        self.assertContains(response, "9 inactive")
        self.assertContains(response, "1 waiting occurrence")
        self.assertContains(response, "Calendar day")
        self.assertContains(response, "not configured to run on a closed bank Saturday")
        self.assertContains(response, "2nd Saturday")
        self.assertContains(response, "Oracle procedure execution is disabled")
        self.assertEqual(response.context["queue_size"], 0)
        self.worker.calendar.assert_called_once_with(self.today, 1)
        self.assertEqual({call[0] for call in self.worker.mock_calls}, {"snapshot", "calendar"})

    def test_zero_filter_results_do_not_claim_the_live_queue_is_empty(self):
        self.snapshot["priority_queue"] = [{**self.snapshot["staging"][0], "state": "READY"}]
        response = self.response({"date": "2026-09-10"}, alert=False)
        self.assertContains(response, "No READY occurrences match report date 10/09/2026")
        self.assertContains(response, "1 occurrence remains in the live queue above")
        self.assertContains(response, "Clear report-date filter")
        self.assertNotContains(response, "No tasks are ready to run")
        self.assertNotIn("queue_diagnostics", response.context)
        self.worker.calendar.assert_not_called()

    def test_worker_failure_and_stop_have_specific_explanations(self):
        for meta, service, expected in (
            ({"cycle_status": "ERROR"}, {"scheduler_enabled": True}, "The last scheduler cycle failed."),
            ({"cycle_status": "OK"}, {"scheduler_enabled": False}, "The scheduler is stopped."),
        ):
            with self.subTest(expected=expected):
                self.snapshot["meta"].update(meta)
                self.snapshot["service_control"] = service
                response = self.response()
                self.assertEqual(response.context["queue_diagnostics"]["title"], expected)
                self.assertContains(response, expected)

    def test_disconnected_worker_does_not_fetch_calendar_or_claim_empty_live_state(self):
        self.worker.snapshot.side_effect = SchedulerApiError("Scheduler API unavailable")
        response = self.response()
        self.assertContains(response, "Live queue status is unavailable.")
        self.assertContains(response, "last saved state")
        self.assertNotContains(response, "No tasks are ready to run.")
        self.worker.calendar.assert_not_called()

    def test_disabled_and_paused_schedules_are_not_presented_as_active_gates(self):
        self.snapshot["staging"].append({**self.snapshot["staging"][0], "job_id": 2, "occurrence_key": "2:2026-09-11"})
        self.snapshot["controls"] = [{"job_id": 1, "control_status": "PAUSED"}]
        response = self.response()
        self.assertEqual(response.context["queue_diagnostics"]["waiting_count"], 0)
        self.assertContains(response, "Paused tasks")
        self.assertNotContains(response, "Recorded gates for active schedules")

    def test_calendar_failure_does_not_invent_holiday_explanation(self):
        self.worker.calendar.side_effect = SchedulerApiError("Calendar unavailable")
        response = self.response()
        self.assertContains(response, "Calendar day")
        self.assertNotContains(response, "Second or fourth Saturday bank holiday")
        self.assertNotIn("calendar_day", response.context["queue_diagnostics"])

    def test_all_inactive_and_empty_configuration_have_actionable_messages(self):
        for masters, expected in (
            ([{**row, "is_active": 0} for row in self.snapshot["schedule_master"]], "All configured schedules are inactive."),
            ([], "No schedules are configured."),
        ):
            with self.subTest(expected=expected):
                self.snapshot["schedule_master"] = masters
                self.assertContains(self.response(), expected)
