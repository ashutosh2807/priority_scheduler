from datetime import date
from unittest.mock import Mock, patch

from django.contrib import admin
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import AdminRole, AdminUser
from apps.audit.models import AuditDelivery, AuditLog
from apps.scheduler.models import ScheduleProfile

from .models import LeaveRequest, LeaveScheduleCoverage, LeaveStatus
from . import services


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LeaveAdminTests(TestCase):
    def setUp(self):
        self.manager = AdminUser.objects.create_superuser(username="leave-manager", employee_id="LA01",
            display_name="Leave manager", role=AdminRole.SUPERUSER, password="test-password")
        self.operator = AdminUser.objects.create_user(username="leave-owner", employee_id="LA02", display_name="Owner", is_staff=True)
        self.cover = AdminUser.objects.create_user(username="leave-cover", employee_id="LA03", display_name="Cover")
        self.backup = AdminUser.objects.create_user(username="second-cover", employee_id="LA04", display_name="Other cover")
        self.operator.user_permissions.add(*Permission.objects.filter(content_type__app_label="leave"))
        self.profile = ScheduleProfile.objects.create(schedule_id=501, primary_operator=self.operator, backup_operator=self.cover)
        adapter = Mock()
        adapter.get_schedules.return_value = [
            {"id": 501, "name": "Confirmed report", "confirmation_needed": True},
            {"id": 502, "name": "Automated report", "confirmation_needed": False},
        ]
        adapter.get_status.return_value = {"available": True}
        self.source = patch("apps.leave.services.get_scheduler_read_adapter", return_value=adapter)
        self.source.start()
        self.addCleanup(self.source.stop)
        self.client.force_login(self.manager)

    def leave(self, **kwargs):
        return LeaveRequest.objects.create(admin=kwargs.pop("admin", self.operator), start_date=date(2026, 9, 12),
            end_date=date(2026, 9, 14), reason="Planned leave", **kwargs)

    def action(self, name, leaves):
        return self.client.post(reverse("admin:leave_leaverequest_changelist"), {
            "action": name, "_selected_action": [str(leave.pk) for leave in leaves],
        })

    def add_cover(self, leave, user=None, schedule_id=501):
        return self.client.post(reverse("admin:leave_leaveschedulecoverage_add"), {
            "leave_request": leave.pk, "schedule_id": schedule_id, "covering_admin": (user or self.cover).pk,
        })

    def test_models_registered_with_filters_search_autocomplete_and_pagination(self):
        for model in (LeaveRequest, LeaveScheduleCoverage):
            config = admin.site._registry[model]
            self.assertEqual(config.list_per_page, 25)
            self.assertTrue(config.search_fields)
            self.assertTrue(config.list_filter)
            self.assertTrue(config.autocomplete_fields)
        for route in ("admin:leave_leaverequest_changelist", "admin:leave_leaveschedulecoverage_changelist"):
            self.assertEqual(self.client.get(reverse(route)).status_code, 200)

    def test_admin_create_forces_pending_and_audits_with_durable_outbox(self):
        response = self.client.post(reverse("admin:leave_leaverequest_add"), {
            "admin": self.operator.pk, "start_date": "2026-09-12", "end_date": "2026-09-14", "reason": "Planned leave",
            "status": "APPROVED", "approved_by": self.cover.pk, "approved_at": "2026-09-01",
        })
        self.assertEqual(response.status_code, 302)
        leave = LeaveRequest.objects.get()
        self.assertEqual(leave.status, LeaveStatus.PENDING)
        self.assertIsNone(leave.approved_by)
        self.assertIsNone(leave.approved_at)
        audit = AuditLog.objects.get(action="LEAVE_CREATED")
        self.assertEqual(audit.actor, self.manager)
        self.assertEqual(audit.changes["after"]["admin"], self.operator.pk)
        self.assertTrue(AuditDelivery.objects.filter(audit=audit).exists())

    def test_pending_edits_validate_period_preserve_owner_and_audit(self):
        leave = self.leave()
        url = reverse("admin:leave_leaverequest_change", args=[leave.pk])
        invalid = self.client.post(url, {"start_date": "2026-09-14", "end_date": "2026-09-12", "reason": "Changed"})
        self.assertContains(invalid, "End date cannot be before start date")
        self.assertFalse(AuditLog.objects.exists())
        response = self.client.post(url, {"admin": self.cover.pk, "start_date": "2026-09-13", "end_date": "2026-09-14", "reason": "Updated reason"})
        self.assertEqual(response.status_code, 302)
        leave.refresh_from_db()
        self.assertEqual(leave.admin, self.operator)
        self.assertEqual(leave.start_date, date(2026, 9, 13))
        self.assertEqual(AuditLog.objects.get().action, "LEAVE_UPDATED")

    def test_approval_selects_available_cover_and_records_reviewer_and_time(self):
        leave = self.leave()
        self.assertEqual(self.action("approve_requests", [leave]).status_code, 302)
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveStatus.APPROVED)
        self.assertEqual(leave.approved_by, self.manager)
        self.assertIsNotNone(leave.approved_at)
        coverage = leave.schedule_coverages.get()
        self.assertEqual(coverage.covering_admin, self.cover)
        self.assertEqual(coverage.assigned_by, self.manager)
        self.assertEqual(set(AuditLog.objects.values_list("action", flat=True)), {"LEAVE_APPROVED", "LEAVE_SCHEDULE_COVER_ASSIGNED"})
        self.assertEqual(AuditDelivery.objects.count(), 2)
        response = self.client.post(reverse("admin:leave_leaverequest_change", args=[leave.pk]), {
            "start_date": "2026-10-01", "end_date": "2026-10-02", "reason": "Alter approval",
        })
        self.assertEqual(response.status_code, 403)
        leave.refresh_from_db()
        self.assertEqual(leave.start_date, date(2026, 9, 12))

    def test_reject_and_cancel_are_audited_and_request_delete_is_disabled(self):
        rejected, cancelled = self.leave(), self.leave(status=LeaveStatus.APPROVED, approved_by=self.manager)
        self.action("reject_requests", [rejected])
        self.action("cancel_requests", [cancelled])
        rejected.refresh_from_db()
        cancelled.refresh_from_db()
        self.assertEqual(rejected.status, LeaveStatus.REJECTED)
        self.assertEqual(rejected.approved_by, self.manager)
        self.assertIsNotNone(rejected.approved_at)
        self.assertEqual(cancelled.status, LeaveStatus.CANCELLED)
        self.assertEqual(cancelled.approved_by, self.manager)
        self.assertEqual(AuditDelivery.objects.count(), 2)
        self.assertEqual(self.client.post(reverse("admin:leave_leaverequest_delete", args=[rejected.pk]), {"post": "yes"}).status_code, 403)

    def test_bulk_approval_failure_rolls_back_prior_requests_covers_and_events(self):
        pending, actioned = self.leave(), self.leave(status=LeaveStatus.REJECTED)
        self.action("approve_requests", [pending, actioned])
        pending.refresh_from_db()
        self.assertEqual(pending.status, LeaveStatus.PENDING)
        self.assertFalse(LeaveScheduleCoverage.objects.exists())
        self.assertFalse(AuditLog.objects.exists())
        self.assertFalse(AuditDelivery.objects.exists())

    def test_cover_add_reassign_and_remove_use_workflow_audit(self):
        leave = self.leave()
        self.assertEqual(self.add_cover(leave).status_code, 302)
        coverage = leave.schedule_coverages.get()
        self.assertEqual(coverage.schedule_name, "Confirmed report")
        self.assertEqual(coverage.assigned_by, self.manager)
        response = self.client.post(reverse("admin:leave_leaveschedulecoverage_change", args=[coverage.pk]), {
            "covering_admin": self.backup.pk, "schedule_id": 999, "schedule_name": "Forged", "assigned_by": self.operator.pk,
        })
        self.assertEqual(response.status_code, 302)
        coverage.refresh_from_db()
        self.assertEqual(coverage.schedule_id, 501)
        self.assertEqual(coverage.covering_admin, self.backup)
        self.assertEqual(coverage.assigned_by, self.manager)
        response = self.client.post(reverse("admin:leave_leaveschedulecoverage_delete", args=[coverage.pk]), {"post": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LeaveScheduleCoverage.objects.exists())
        self.assertEqual(set(AuditLog.objects.values_list("action", flat=True)), {
            "LEAVE_SCHEDULE_COVER_ASSIGNED", "LEAVE_SCHEDULE_COVER_REASSIGNED", "LEAVE_SCHEDULE_COVER_REMOVED",
        })
        self.assertEqual(AuditDelivery.objects.count(), 3)

    def test_invalid_cover_is_form_error_for_self_unavailable_or_wrong_schedule(self):
        leave = self.leave()
        self.leave(admin=self.cover, status=LeaveStatus.APPROVED)
        for user, schedule in ((self.operator, 501), (self.cover, 501), (self.backup, 502), (self.backup, 999)):
            with self.subTest(user=user.pk, schedule=schedule):
                response = self.add_cover(leave, user, schedule)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["adminform"].form.errors)
        self.assertFalse(LeaveScheduleCoverage.objects.exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_pending_date_extension_rechecks_existing_cover(self):
        leave = self.leave()
        self.add_cover(leave)
        LeaveRequest.objects.create(admin=self.cover, start_date=date(2026, 9, 15), end_date=date(2026, 9, 15),
                                    reason="Cover unavailable", status=LeaveStatus.APPROVED)
        response = self.client.post(reverse("admin:leave_leaverequest_change", args=[leave.pk]), {
            "start_date": "2026-09-12", "end_date": "2026-09-15", "reason": "Extend leave",
        })
        self.assertContains(response, "Existing cover is unavailable")
        leave.refresh_from_db()
        self.assertEqual(leave.end_date, date(2026, 9, 14))
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_audit_outbox_failure_rolls_back_cover_save(self):
        leave = self.leave()
        with patch("apps.audit.delivery.enqueue_audit", side_effect=RuntimeError("audit storage failed")), self.assertRaises(RuntimeError):
            self.add_cover(leave)
        self.assertFalse(LeaveScheduleCoverage.objects.exists())
        self.assertFalse(AuditLog.objects.exists())

    def test_native_permissions_and_staff_gate_are_not_replaced_by_business_role(self):
        staff = AdminUser.objects.create_user(username="no-permissions", employee_id="LA05", display_name="Staff",
                                              role=AdminRole.SUPERUSER, is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("admin:leave_leaverequest_changelist")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:leave_leaverequest_add"), {}).status_code, 403)
        staff.is_staff = False
        staff.save(update_fields=["is_staff"])
        self.assertEqual(self.client.get(reverse("admin:leave_leaverequest_changelist")).status_code, 302)

    def test_operator_with_model_permissions_can_only_manage_own_pending_leave(self):
        own, others = self.leave(), self.leave(admin=self.cover)
        self.client.force_login(self.operator)
        response = self.client.get(reverse("admin:leave_leaverequest_changelist"))
        self.assertEqual(list(response.context["cl"].queryset), [own])
        self.assertNotIn("approve_requests", [value for value, label in response.context["action_form"].fields["action"].choices])
        self.action("approve_requests", [own])
        own.refresh_from_db()
        self.assertEqual(own.status, LeaveStatus.PENDING)
        self.assertIn(self.client.get(reverse("admin:leave_leaverequest_change", args=[others.pk])).status_code, (302, 404))

    def test_shared_services_reject_stale_and_unauthorized_transitions(self):
        leave = self.leave()
        with self.assertRaises(ValidationError):
            services.reject(leave, self.operator)
        services.reject(leave, self.manager)
        with self.assertRaises(ValidationError):
            services.cancel(leave, self.manager)
        self.assertEqual(AuditLog.objects.count(), 1)

    def test_actioned_coverage_cannot_be_changed_or_removed_by_admin_actions(self):
        leave = self.leave()
        self.add_cover(leave)
        self.action("approve_requests", [leave])
        coverage = leave.schedule_coverages.get()
        before = AuditLog.objects.count()
        response = self.client.post(reverse("admin:leave_leaveschedulecoverage_change", args=[coverage.pk]), {"covering_admin": self.backup.pk})
        self.assertEqual(response.status_code, 403)
        self.client.post(reverse("admin:leave_leaveschedulecoverage_changelist"), {
            "action": "remove_coverages", "_selected_action": [str(coverage.pk)],
        })
        self.assertTrue(LeaveScheduleCoverage.objects.filter(pk=coverage.pk).exists())
        self.assertEqual(AuditLog.objects.count(), before)

    def test_bulk_coverage_removal_is_audited(self):
        leave = self.leave()
        self.add_cover(leave)
        coverage = leave.schedule_coverages.get()
        response = self.client.post(reverse("admin:leave_leaveschedulecoverage_changelist"), {
            "action": "remove_coverages", "_selected_action": [str(coverage.pk)],
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LeaveScheduleCoverage.objects.exists())
        self.assertEqual(AuditLog.objects.filter(action="LEAVE_SCHEDULE_COVER_REMOVED").count(), 1)
        self.assertEqual(AuditDelivery.objects.count(), 2)

    def test_audit_failure_rolls_back_pending_leave_edit(self):
        leave = self.leave()
        with patch("apps.audit.delivery.enqueue_audit", side_effect=RuntimeError("audit storage failed")), self.assertRaises(RuntimeError):
            self.client.post(reverse("admin:leave_leaverequest_change", args=[leave.pk]), {
                "start_date": "2026-09-13", "end_date": "2026-09-14", "reason": "Updated",
            })
        leave.refresh_from_db()
        self.assertEqual(leave.start_date, date(2026, 9, 12))
        self.assertEqual(leave.reason, "Planned leave")
        self.assertFalse(AuditLog.objects.exists())

    def test_portal_reports_shared_transition_validation_errors_without_server_error(self):
        leave = self.leave()
        for action in ("reject", "cancel"):
            with self.subTest(action=action), patch(f"apps.leave.views.{action}", side_effect=ValidationError("Already actioned")):
                response = self.client.post(reverse("leave:action", args=[leave.pk, action]))
                self.assertEqual(response.status_code, 302)
        self.add_cover(leave)
        coverage = leave.schedule_coverages.get()
        with patch("apps.leave.views.remove_schedule_coverage", side_effect=ValidationError("Already actioned")):
            response = self.client.post(reverse("leave:coverage-remove", args=[leave.pk, coverage.pk]))
            self.assertEqual(response.status_code, 302)
