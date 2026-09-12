from datetime import date, timedelta
from unittest.mock import Mock, patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AdminUser
from apps.audit.models import AuditLog
from apps.leave.models import LeaveRequest, LeaveStatus, LeaveScheduleCoverage
from apps.leave.services import approve
from .control_client import SchedulerApiClient, SchedulerApiError
from .daybook import daybook_rows, daybook_counts
from .file_adapter import SchedulerApiReadAdapter
from .models import ScheduleProfile, RunbookProgress
from .services import confirmation_responsibility, SchedulerService, record_runbook_step
from .views import _parse_date


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class WorkflowTests(TestCase):
    def setUp(self):
        self.day = timezone.localdate()
        self.primary = AdminUser.objects.create_user(username="primary", employee_id="002", display_name="Primary Operator", password="test-only")
        self.cover = AdminUser.objects.create_user(username="cover", employee_id="003", display_name="Cover Operator", password="test-only")
        self.agm = AdminUser.objects.create_user(username="manager", employee_id="001", display_name="AGM Operator", role="SUPERUSER", password="test-only")
        self.profile = ScheduleProfile.objects.create(schedule_id=1, primary_operator=self.primary,
            backup_operator=self.cover, operational_steps=["Check source", "Verify totals"], expected_minutes=15)
        self.snapshot = {
            "schedule_master": [{"id": 1, "name": "Treasury daily report", "is_active": 1,
                "same_day": 0, "confirmation_needed": 1, "run_config": {"RUNS_ON": ["DAILY"]}}],
            "controls": [{"job_id": 1, "control_status": "ACTIVE", "confirmation": 0}],
            "staging": [{"job_id": 1, "job_name": "Treasury daily report", "state": "WAITING_CONFIRMATION",
                "occurrence_key": "task-1-today", "report_date": (self.day-timedelta(days=1)).isoformat(),
                "execution_date": self.day.isoformat(), "confirmation_confirmed": False}],
            "ready": [], "executions": [], "priority_queue": [],
            "meta": {"queue_revision": "rev1"},
        }
        self.entry = {**self.snapshot["staging"][0], "calendar_date": self.day.isoformat(),
            "confirmation_needed": True, "source": "staging", "is_projection": False}
        self.payload = {"occurrences": [self.entry], "calendar": {"source": "oracle", "latest_report_date": (self.day-timedelta(days=1)).isoformat()}}
        self.worker = Mock()
        self.worker.snapshot.return_value = self.snapshot
        self.worker.calendar.return_value = self.payload
        self.adapter = SchedulerApiReadAdapter(client=self.worker)
        self.client.force_login(self.primary)

    def absent(self, user, status=LeaveStatus.APPROVED):
        return LeaveRequest.objects.create(admin=user, start_date=self.day, end_date=self.day, status=status)

    def test_responsibility_primary_pending_leave_nominated_and_fallback(self):
        leave = self.absent(self.primary, LeaveStatus.PENDING)
        self.assertEqual(confirmation_responsibility(1, self.day)["operator"], self.primary)
        leave.status = LeaveStatus.APPROVED
        leave.save()
        self.assertEqual(confirmation_responsibility(1, self.day)["source"], "Preferred cover")
        LeaveScheduleCoverage.objects.create(leave_request=leave, schedule_id=1, covering_admin=self.agm)
        self.assertEqual(confirmation_responsibility(1, self.day)["operator"], self.agm)
        self.absent(self.agm)
        self.assertEqual(confirmation_responsibility(1, self.day)["operator"], self.cover)
        self.absent(self.cover)
        self.assertIsNone(confirmation_responsibility(1, self.day)["operator"])
        self.assertEqual(confirmation_responsibility(1, self.day+timedelta(days=1))["operator"], self.primary)

    def test_fallback_uses_stable_available_operator(self):
        self.absent(self.primary)
        self.absent(self.cover)
        self.assertEqual(confirmation_responsibility(1)["operator"], self.agm)
        self.assertEqual(confirmation_responsibility(1)["source"], "Available operator")

    @patch("apps.leave.services.get_scheduler_read_adapter")
    def test_approval_revalidates_cover_and_records_replacement(self, factory):
        factory.return_value = self.adapter
        leave = self.absent(self.primary, LeaveStatus.PENDING)
        LeaveScheduleCoverage.objects.create(leave_request=leave, schedule_id=1, covering_admin=self.cover)
        self.absent(self.cover)
        approve(leave, self.agm)
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveStatus.APPROVED)
        self.assertEqual(leave.schedule_coverages.get().covering_admin, self.agm)
        self.assertTrue(AuditLog.objects.filter(action="LEAVE_SCHEDULE_COVER_REASSIGNED").exists())

    @patch("apps.leave.services.get_scheduler_read_adapter")
    def test_approval_with_no_available_cover_rolls_back(self, factory):
        factory.return_value = self.adapter
        leave = self.absent(self.primary, LeaveStatus.PENDING)
        self.absent(self.cover)
        self.absent(self.agm)
        with self.assertRaises(ValidationError):
            approve(leave, self.agm)
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveStatus.PENDING)
        self.assertFalse(leave.schedule_coverages.exists())

    def test_confirmation_enforces_assignee_and_exact_occurrence(self):
        control = Mock()
        control.control.return_value = {"confirmation": 1}
        service = SchedulerService(self.adapter, control)
        schedule = self.adapter.get_schedule(1)
        with self.assertRaises(PermissionDenied):
            service.control(schedule, "confirm", self.agm, occurrence_key="task-1-today")
        with self.assertRaises(ValidationError):
            service.control(schedule, "confirm", self.primary, occurrence_key="yesterday")
        control.control.assert_not_called()
        service.control(schedule, "confirm", self.primary, occurrence_key="task-1-today")
        control.control.assert_called_once()
        self.assertEqual(AuditLog.objects.get().changes["confirmation_assignment"]["employee_id"], "002")

    def test_inactive_operator_and_non_manager_override_are_blocked(self):
        service = SchedulerService(self.adapter, Mock())
        with self.assertRaises(PermissionDenied):
            service.control(self.adapter.get_schedule(1), "reset", self.primary, reason="Test")
        self.primary.is_active_admin = False
        with self.assertRaises(PermissionDenied):
            service.control(self.adapter.get_schedule(1), "pause", self.primary)

    def test_scoped_confirmation_is_visible_without_global_flag(self):
        self.snapshot["staging"][0]["confirmation_confirmed"] = True
        self.entry["confirmation_confirmed"] = True
        rows = daybook_rows(self.payload, self.adapter, self.primary, self.day)
        self.assertTrue(rows[0]["confirmed"])
        self.assertTrue(self.adapter.get_schedule(1)["control"]["confirmation"])
        self.assertEqual(rows[0]["display_status"], "Pending")

    def test_automated_tasks_never_offer_confirmation(self):
        self.entry["confirmation_needed"] = False
        rows = daybook_rows(self.payload, self.adapter, self.primary, self.day)
        self.assertFalse(rows[0]["can_confirm"])

    def test_sibling_occurrence_retains_its_own_confirmation_scope(self):
        sibling = {**self.snapshot["staging"][0], "occurrence_key": "second-report", "confirmation_confirmed": True}
        self.snapshot["staging"].append(sibling)
        first = self.adapter.get_schedule_occurrence(1, "task-1-today")
        second = self.adapter.get_schedule_occurrence(1, "second-report")
        self.assertFalse(first["control"]["confirmation"])
        self.assertTrue(second["control"]["confirmation"])

    def test_stale_runbook_step_is_rejected_and_progress_audited(self):
        schedule = self.adapter.get_schedule(1)
        with self.assertRaises(ValidationError):
            record_runbook_step(schedule, self.primary, report_date=self.day, step_index=0,
                                completed=True, expected_step="Old guide text")
        self.assertFalse(RunbookProgress.objects.exists())
        record_runbook_step(schedule, self.primary, report_date=self.day, step_index=0,
                            completed=True, expected_step="Check source")
        self.assertEqual(RunbookProgress.objects.get().actor, self.primary)
        self.assertEqual(AuditLog.objects.get().changes["report_date"], self.day.isoformat())

    @patch("apps.scheduler.views.get_scheduler_read_adapter")
    def test_day_confirmation_and_detail_pages_use_worker_data(self, factory):
        factory.return_value = self.adapter
        for name in ("scheduler:day", "scheduler:confirmation"):
            response = self.client.get(reverse(name))
            self.assertContains(response, "Treasury daily report")
            self.assertContains(response, "Primary Operator")
            self.assertContains(response, "oracle")
        response = self.client.get(reverse("scheduler:detail", args=[1]))
        self.assertContains(response, "Check source")
        self.assertContains(response, "Runbook and work log")

    @patch("apps.scheduler.views.get_scheduler_read_adapter")
    def test_schedule_audit_excludes_unrelated_numeric_ids(self, factory):
        factory.return_value = self.adapter
        AuditLog.objects.create(action="SECRET_UNRELATED", object_type="leave.LeaveRequest", object_id="1")
        response = self.client.get(reverse("scheduler:detail", args=[1]))
        self.assertNotContains(response, "SECRET_UNRELATED")

    @patch("apps.scheduler.views.get_scheduler_read_adapter")
    def test_operator_cannot_reassign_ownership_or_edit_unowned_profile(self, factory):
        factory.return_value = self.adapter
        response = self.client.post(reverse("scheduler:edit", args=[1]), {
            "responsible_operator": self.cover.pk, "backup_operator": self.agm.pk,
            "operational_steps": "Check source", "expected_minutes": "20"})
        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.primary_operator, self.primary)
        self.assertEqual(self.profile.backup_operator, self.cover)
        self.client.force_login(self.cover)
        self.assertEqual(self.client.get(reverse("scheduler:edit", args=[1])).status_code, 403)

    @patch("apps.dashboard.views.get_scheduler_read_adapter")
    def test_calendar_groups_by_operating_day_and_keeps_business_date(self, factory):
        factory.return_value = self.adapter
        response = self.client.get(reverse("dashboard:calendar"), {"month": self.day.strftime("%Y-%m"), "date": self.day.isoformat()})
        self.assertContains(response, "Treasury daily report")
        self.assertEqual(response.context["day_rows"][0]["report_date"], (self.day-timedelta(days=1)).isoformat())
        self.worker.calendar.assert_called_once()

    def test_date_filters_and_help_authentication(self):
        self.assertEqual(_parse_date("2026-09-11"), date(2026,9,11))
        self.assertIsNone(_parse_date("invalid"))
        self.assertContains(self.client.get(reverse("dashboard:help")), "31 March")
        self.client.logout()
        for route in ("scheduler:day", "dashboard:help", "accounts:list"):
            self.assertEqual(self.client.get(reverse(route)).status_code, 302)

    @patch("apps.scheduler.views.get_scheduler_control_client")
    def test_queue_reorder_forwards_revision_and_audits(self, factory):
        factory.return_value.reorder_queue.return_value = {"queue_revision": "rev2"}
        response = self.client.post(reverse("scheduler:queue-reorder"), {
            "occurrence_keys": ["b", "a"], "queue_revision": "rev1"})
        self.assertEqual(response.status_code, 302)
        factory.return_value.reorder_queue.assert_called_once_with(["b", "a"], "rev1", actor="primary", reason="Operator changed queue order")
        self.assertTrue(AuditLog.objects.filter(action="QUEUE_REORDERED").exists())

    def test_ready_order_comes_from_worker_and_not_local_sort(self):
        self.snapshot["priority_queue"] = [
            {"job_id": 1, "occurrence_key": "second", "report_date": "2026-09-10", "execution_date": self.day.isoformat()},
            {"job_id": 1, "occurrence_key": "first", "report_date": "2026-09-09", "execution_date": self.day.isoformat()},
        ]
        self.assertEqual([row["occurrence"]["occurrence_key"] for row in self.adapter.get_ready()], ["second", "first"])

    def test_unavailable_calendar_is_explicit_and_has_no_forecasts(self):
        self.worker.calendar.side_effect = SchedulerApiError("Worker calendar unavailable")
        result = self.adapter.get_operations_calendar(self.day, 1)
        self.assertFalse(result["projection_available"])
        self.assertEqual(result["error"], "Worker calendar unavailable")

    @patch("apps.scheduler.views.get_scheduler_read_adapter")
    def test_monitoring_mode_shows_real_tasks_with_execution_disabled(self, factory):
        self.snapshot["meta"].update(mode="monitor", execution_enabled=False)
        factory.return_value = self.adapter
        response = self.client.get(reverse("scheduler:day"))
        self.assertContains(response, "Monitoring mode")
        self.assertContains(response, "Oracle procedure execution is disabled")
        self.assertContains(response, "Treasury daily report")
        self.assertFalse(response.context["scheduler"]["execution_enabled"])

    @patch("apps.scheduler.views.get_scheduler_read_adapter")
    def test_connected_api_does_not_hide_failed_scheduling_cycle(self, factory):
        self.snapshot["meta"].update(cycle_status="ERROR", last_cycle_error={"type": "TypeError"})
        factory.return_value = self.adapter
        response = self.client.get(reverse("scheduler:day"))
        self.assertContains(response, "The last scheduling cycle failed")
        self.assertContains(response, "Treasury daily report")
