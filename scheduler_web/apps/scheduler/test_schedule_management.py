from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from apps.audit.models import AuditLog
from . import test_workflow
from .control_client import SchedulerApiClient, SchedulerApiError
from .forms import ScheduleDefinitionForm
from .models import ScheduleProfile


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ScheduleManagementTests(TestCase):
    def setUp(self):
        test_workflow.WorkflowTests.setUp(self)
        self.client.force_login(self.agm)
        self.snapshot["schedule_master"][0]["package_name"] = "REPORTS.TREASURY"
        adapter_patch = patch("apps.scheduler.views.get_scheduler_read_adapter", return_value=self.adapter)
        adapter_patch.start()
        self.addCleanup(adapter_patch.stop)
        self.api = Mock()
        client_patch = patch("apps.scheduler.views.get_scheduler_control_client", return_value=self.api)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.data = {"schedule_id": 7, "name": "Monthly settlement", "package_name": "REPORTS.SETTLEMENT",
                     "frequencies": ["MONTHLY", "QUARTERLY"], "margin": "T+1", "is_active": "on",
                     "max_attempts": 9, "confirmation_needed": "on", "responsible_operator": self.primary.pk,
                     "backup_operator": self.cover.pk, "operational_steps": "Check inputs\nVerify totals",
                     "expected_minutes": 30, "reason": "New operating requirement"}
        self.api.create_schedule.return_value = {"id": 7, "source": "oracle", "after": {"id": 7}}
        self.api.update_schedule.return_value = {"id": 1, "source": "oracle", "after": {"id": 1}}
        self.api.delete_schedule.return_value = {"id": 1, "source": "oracle", "before": {"id": 1}}

    def test_create_sends_definition_and_saves_confirmation_owner(self):
        response = self.client.post(reverse("scheduler:create"), self.data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("scheduler:detail", args=[7]))
        payload = self.api.create_schedule.call_args.args[0]
        self.assertEqual(payload["id"], 7)
        self.assertEqual(payload["run_config"]["MAX_ATTEMPTS"], 9)
        self.assertEqual(payload["run_config"]["RUNS_ON"], ["MONTHLY", "QUARTERLY"])
        self.assertNotIn("responsible_operator", payload)
        profile = ScheduleProfile.objects.get(schedule_id=7)
        self.assertEqual(profile.primary_operator, self.primary)
        self.assertEqual(profile.backup_operator, self.cover)
        self.assertEqual(profile.operational_steps, ["Check inputs", "Verify totals"])
        self.assertTrue(AuditLog.objects.filter(action="SCHEDULE_CREATED", actor=self.agm).exists())

    def test_edit_id_is_immutable_and_cleared_window_is_forwarded(self):
        response = self.client.post(reverse("scheduler:definition", args=[1]), self.data)
        self.assertEqual(response.status_code, 302)
        args = self.api.update_schedule.call_args.args
        self.assertEqual(args[0], 1)
        self.assertNotIn("id", args[1])
        self.assertIsNone(args[1]["run_config"]["RUN_BY"])
        self.assertFalse(args[1]["time_flag"])

    def test_missing_confirmation_owner_blocks_write(self):
        response = self.client.post(reverse("scheduler:create"), {**self.data, "responsible_operator": ""})
        self.assertContains(response, "Select the primary confirmation operator")
        self.api.create_schedule.assert_not_called()

    def test_worker_error_preserves_form_without_local_success(self):
        self.api.create_schedule.side_effect = SchedulerApiError("Oracle save unavailable")
        response = self.client.post(reverse("scheduler:create"), self.data)
        self.assertContains(response, "Oracle save unavailable")
        self.assertContains(response, "Monthly settlement")
        self.assertFalse(ScheduleProfile.objects.filter(schedule_id=7).exists())
        self.assertFalse(AuditLog.objects.filter(action="SCHEDULE_CREATED").exists())

    def test_delete_requires_exact_name_and_preserves_profile(self):
        route = reverse("scheduler:delete", args=[1])
        response = self.client.post(route, {"confirm_name": "wrong", "reason": "Retired"})
        self.assertContains(response, "The name does not match")
        self.api.delete_schedule.assert_not_called()
        response = self.client.post(route, {"confirm_name": "Treasury daily report", "reason": "Retired"})
        self.assertEqual(response.url, reverse("scheduler:jobs"))
        self.api.delete_schedule.assert_called_once_with(1, actor="manager", reason="Retired")
        self.assertTrue(ScheduleProfile.objects.filter(schedule_id=1).exists())
        self.assertTrue(AuditLog.objects.filter(action="SCHEDULE_DELETED").exists())

    def test_operator_cannot_manage_definitions_or_refresh_calendar(self):
        self.client.force_login(self.primary)
        for route in (reverse("scheduler:create"), reverse("scheduler:definition", args=[1]),
                      reverse("scheduler:delete", args=[1]), reverse("scheduler:calendar-refresh")):
            self.assertEqual(self.client.post(route, self.data).status_code, 403)
        self.assertEqual(self.api.mock_calls, [])

    def test_management_screens_and_navigation_render(self):
        for name, args in (("create", []), ("definition", [1]), ("delete", [1])):
            self.assertEqual(self.client.get(reverse("scheduler:" + name, args=args)).status_code, 200)
        self.assertContains(self.client.get(reverse("scheduler:jobs")), "Add schedule")
        detail = self.client.get(reverse("scheduler:detail", args=[1]))
        self.assertContains(detail, "Edit schedule settings")
        self.assertContains(detail, "Delete schedule")

    def test_specific_dates_and_windows_are_validated(self):
        form = ScheduleDefinitionForm({**self.data, "frequencies": ["SPECIFIC_DATE"], "specific_dates": "2026-02-30",
                                       "from_time": "09:00"})
        self.assertFalse(form.is_valid())
        self.assertIn("specific_dates", form.errors)
        self.assertIn("__all__", form.errors)

    def test_calendar_failure_is_not_reported_as_success(self):
        self.api.refresh_calendar.return_value = {"refreshed": False, "error": "Oracle is unavailable"}
        response = self.client.post(reverse("scheduler:calendar-refresh"))
        from django.contrib.messages import get_messages
        notifications = list(get_messages(response.wsgi_request))
        self.assertEqual(len(notifications), 1)
        self.assertEqual(str(notifications[0]), "Oracle is unavailable")
        self.assertNotEqual(notifications[0].tags, "success")

    def test_client_uses_worker_crud_routes(self):
        client = SchedulerApiClient(base_url="http://worker.invalid")
        with patch.object(client, "_request", return_value={"schedule": {"source": "oracle"}}) as request:
            client.create_schedule({"id": 7}, actor="agm", reason="New")
            request.assert_called_with("POST", "/v1/jobs", {"id": 7, "actor": "agm", "reason": "New"})
            client.update_schedule(7, {"name": "Changed"}, actor="agm", reason="Edit")
            request.assert_called_with("PATCH", "/v1/jobs/7/definition", {"name": "Changed", "actor": "agm", "reason": "Edit"})
            client.delete_schedule(7, actor="agm", reason="Retired")
            request.assert_called_with("DELETE", "/v1/jobs/7", {"actor": "agm", "reason": "Retired"})
            request.return_value = {"calendar": {"refreshed": True}}
            self.assertEqual(client.refresh_calendar(actor="agm", reason="Updated dates"), {"refreshed": True})
