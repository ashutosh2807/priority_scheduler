import json
from datetime import date
from unittest.mock import patch

from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, Permission
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import AdminRole, AdminUser
from apps.scheduler.models import RunbookProgress, ScheduleProfile
from apps.tasks.models import Task, TaskAssignment, TaskDelegation
from .models import AuditDelivery, AuditLog
from .services import record_action


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class OperationalAdminTests(TestCase):
    def setUp(self):
        self.manager = AdminUser.objects.create_superuser(username="admin-review", password="fixture-only",
                                                        employee_id="AD1", display_name="Admin reviewer")
        self.owner = AdminUser.objects.create_user(username="owner", employee_id="AD2", display_name="Primary operator")
        self.cover = AdminUser.objects.create_user(username="cover", employee_id="AD3", display_name="Cover operator")
        self.client.force_login(self.manager)

    def url(self, model, action, obj=None):
        return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_{action}", args=[obj.pk] if obj else [])

    def profile_data(self, **changes):
        return {"primary_operator": self.owner.pk, "backup_operator": self.cover.pk, "description": "Updated guide",
                "expected_minutes": 15, "operational_steps": '["Check source", "Review report"]', **changes}

    def test_registered_models_are_real_portal_metadata(self):
        for model in (ScheduleProfile, RunbookProgress, AuditLog, AuditDelivery, Task, TaskAssignment, TaskDelegation, AdminUser):
            self.assertIn(model, admin.site._registry)
        self.assertNotIn("datemast", {model._meta.model_name for model in admin.site._registry})
        self.assertNotIn("schedulemaster", {model._meta.model_name for model in admin.site._registry})

    def test_audit_history_and_delivery_reject_all_mutations_even_for_superuser(self):
        entry = record_action(self.manager, "FIXTURE", self.owner)
        for model, obj in ((AuditLog, entry), (AuditDelivery, entry.delivery)):
            with self.subTest(model=model.__name__):
                original = model.objects.values().get(pk=obj.pk)
                self.assertEqual(self.client.get(self.url(model, "changelist")).status_code, 200)
                page = self.client.get(self.url(model, "change", obj))
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, 'name="_save"')
                self.assertEqual(self.client.post(self.url(model, "change", obj), {"action": "FORGED", "payload": "{}"}).status_code, 403)
                self.assertEqual(self.client.post(self.url(model, "add"), {}).status_code, 403)
                self.assertEqual(self.client.post(self.url(model, "delete", obj), {"post": "yes"}).status_code, 403)
                self.assertEqual(model.objects.values().get(pk=obj.pk), original)
                request = RequestFactory().get("/admin/")
                request.user = self.manager
                self.assertEqual(admin.site._registry[model].get_actions(request), {})

    def test_runbook_progress_is_inspectable_but_only_operational_workflow_can_edit_it(self):
        progress = RunbookProgress.objects.create(schedule_id=7, report_date=date(2026, 9, 11), step_index=0,
                                                  step_text="Review report", completed=True, actor=self.owner)
        self.assertEqual(self.client.get(self.url(RunbookProgress, "change", progress)).status_code, 200)
        self.assertEqual(self.client.post(self.url(RunbookProgress, "change", progress), {"completed": ""}).status_code, 403)
        self.assertEqual(self.client.post(self.url(RunbookProgress, "delete", progress), {"post": "yes"}).status_code, 403)
        progress.refresh_from_db()
        self.assertTrue(progress.completed)
        self.assertEqual(progress.actor, self.owner)

    @patch("apps.scheduler.admin.get_scheduler_read_adapter")
    def test_profile_change_is_validated_linked_and_atomically_audited(self, adapter):
        adapter.return_value.get_schedule.return_value = {"id": 7, "confirmation_needed": True}
        profile = ScheduleProfile.objects.create(schedule_id=7, primary_operator=self.owner, description="Original")
        page = self.client.get(self.url(ScheduleProfile, "change", profile))
        self.assertContains(page, reverse("scheduler:detail", args=[7]))
        response = self.client.post(self.url(ScheduleProfile, "change", profile), self.profile_data(schedule_id=999, updated_by=self.cover.pk))
        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.schedule_id, 7)
        self.assertEqual(profile.updated_by, self.manager)
        self.assertEqual(profile.operational_steps, ["Check source", "Review report"])
        audit = AuditLog.objects.get(action="ADMIN_SCHEDULEPROFILE_UPDATED")
        self.assertEqual(audit.changes["before"]["description"], "Original")
        self.assertEqual(audit.changes["after"]["description"], "Updated guide")
        self.assertEqual(audit.delivery.payload["actor"], self.manager.username)
        self.assertIsNone(audit.delivery.accepted_at)
        self.assertEqual(self.client.post(self.url(ScheduleProfile, "add"), {}).status_code, 403)
        self.assertEqual(self.client.post(self.url(ScheduleProfile, "delete", profile), {"post": "yes"}).status_code, 403)

    @patch("apps.scheduler.admin.get_scheduler_read_adapter")
    def test_profile_validation_keeps_confirmation_owner_and_guide_valid(self, adapter):
        adapter.return_value.get_schedule.return_value = {"id": 7, "confirmation_needed": True}
        profile = ScheduleProfile.objects.create(schedule_id=7, primary_operator=self.owner)
        for changes in ({"primary_operator": ""}, {"backup_operator": self.owner.pk},
                        {"operational_steps": '{"step": "invalid"}'}, {"expected_minutes": 0}):
            with self.subTest(changes=changes):
                response = self.client.post(self.url(ScheduleProfile, "change", profile), self.profile_data(**changes))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["adminform"].form.errors)
                profile.refresh_from_db()
                self.assertEqual(profile.primary_operator, self.owner)
                self.assertEqual(profile.description, "")
        self.assertFalse(AuditLog.objects.exists())

    @patch("apps.scheduler.admin.get_scheduler_read_adapter")
    def test_failed_audit_delivery_creation_rolls_back_admin_mutation(self, adapter):
        adapter.return_value.get_schedule.return_value = {"id": 7, "confirmation_needed": True}
        profile = ScheduleProfile.objects.create(schedule_id=7, primary_operator=self.owner, description="Original")
        with patch("apps.audit.delivery.enqueue_audit", side_effect=RuntimeError("outbox unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url(ScheduleProfile, "change", profile), self.profile_data())
        profile.refresh_from_db()
        self.assertEqual(profile.description, "Original")
        self.assertFalse(AuditLog.objects.exists())

    @patch("apps.scheduler.admin.get_scheduler_read_adapter")
    def test_native_model_permission_and_operational_ownership_are_both_required(self, adapter):
        adapter.return_value.get_schedule.return_value = {"id": 7, "confirmation_needed": True}
        profile = ScheduleProfile.objects.create(schedule_id=7, primary_operator=self.owner)
        self.owner.is_staff = True
        self.owner.save(update_fields=["is_staff"])
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.url(ScheduleProfile, "change", profile), self.profile_data()).status_code, 403)
        self.owner.user_permissions.add(Permission.objects.get(codename="change_scheduleprofile"))
        response = self.client.post(self.url(ScheduleProfile, "change", profile), self.profile_data(primary_operator=self.cover.pk))
        self.assertEqual(response.status_code, 302)
        profile.refresh_from_db()
        self.assertEqual(profile.primary_operator, self.owner)  # Owner fields are read-only for non-managers.
        self.assertEqual(profile.description, "Updated guide")
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_superuser)
        self.assertEqual(self.owner.role, AdminRole.ADMIN)

    def test_task_inline_changes_record_authorship_and_full_audit_snapshot(self):
        task = Task.objects.create(name="Portal task", owner=self.owner)
        data = {"name": task.name, "description": "Guide", "status": "ACTIVE", "owner": self.owner.pk,
                "assignments-TOTAL_FORMS": 1, "assignments-INITIAL_FORMS": 0,
                "assignments-0-admin": self.owner.pk, "assignments-0-is_primary": "on",
                "delegations-TOTAL_FORMS": 1, "delegations-INITIAL_FORMS": 0,
                "delegations-0-from_admin": self.owner.pk, "delegations-0-to_admin": self.cover.pk,
                "delegations-0-start_date": "2026-09-12", "delegations-0-end_date": "2026-09-15",
                "delegations-0-is_active": "on", "delegations-0-reason": "Holiday cover"}
        response = self.client.post(self.url(Task, "change", task), data)
        self.assertEqual(response.status_code, 302)
        delegation = TaskDelegation.objects.get(task=task)
        self.assertEqual(delegation.created_by, self.manager)
        audit = AuditLog.objects.get(action="ADMIN_TASK_UPDATED")
        self.assertEqual(audit.changes["before"]["assignments"], [])
        self.assertEqual(audit.changes["after"]["assignments"][0]["admin"], self.owner.pk)
        self.assertEqual(audit.changes["after"]["delegations"][0]["created_by"], self.manager.pk)
        self.assertTrue(AuditDelivery.objects.filter(audit=audit).exists())

    def test_standalone_assignment_rejects_second_primary(self):
        task = Task.objects.create(name="Portal task", owner=self.owner)
        TaskAssignment.objects.create(task=task, admin=self.owner, is_primary=True)
        response = self.client.post(self.url(TaskAssignment, "add"), {"task": task.pk, "admin": self.cover.pk, "is_primary": "on"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already has a primary administrator")
        self.assertEqual(task.assignments.count(), 1)

    def test_task_inline_rejects_two_primary_assignments_before_any_save(self):
        task = Task.objects.create(name="Portal task", owner=self.owner)
        response = self.client.post(self.url(Task, "change", task), {
            "name": task.name, "description": "Should not save", "status": "ACTIVE", "owner": self.owner.pk,
            "assignments-TOTAL_FORMS": 2, "assignments-INITIAL_FORMS": 0,
            "assignments-0-admin": self.owner.pk, "assignments-0-is_primary": "on",
            "assignments-1-admin": self.cover.pk, "assignments-1-is_primary": "on",
            "delegations-TOTAL_FORMS": 0, "delegations-INITIAL_FORMS": 0,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only one administrator can be the primary assignee")
        task.refresh_from_db()
        self.assertEqual(task.description, "")
        self.assertEqual(task.assignments.count(), 0)
        self.assertFalse(AuditLog.objects.exists())

    def test_task_creation_and_bulk_deletion_keep_durable_before_after_history(self):
        response = self.client.post(self.url(Task, "add"), {
            "name": "Admin-created task", "status": "ACTIVE", "owner": self.owner.pk,
            "assignments-TOTAL_FORMS": 0, "assignments-INITIAL_FORMS": 0,
            "delegations-TOTAL_FORMS": 0, "delegations-INITIAL_FORMS": 0,
        })
        self.assertEqual(response.status_code, 302)
        task = Task.objects.get(name="Admin-created task")
        created = AuditLog.objects.get(action="ADMIN_TASK_CREATED")
        self.assertIsNone(created.changes["before"])
        self.assertEqual(created.changes["after"]["name"], task.name)
        TaskAssignment.objects.create(task=task, admin=self.owner, is_primary=True)
        response = self.client.post(self.url(Task, "changelist"), {
            "action": "delete_selected", "_selected_action": [task.pk], "post": "yes",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Task.objects.filter(pk=task.pk).exists())
        deleted = AuditLog.objects.get(action="ADMIN_TASK_DELETED")
        self.assertEqual(deleted.object_id, str(task.pk))
        self.assertIsNone(deleted.changes["after"])
        self.assertEqual(deleted.changes["before"]["assignments"][0]["admin"], self.owner.pk)
        self.assertEqual(AuditDelivery.objects.count(), 2)

    def test_failed_bulk_delete_audit_rolls_back_task_and_children(self):
        task = Task.objects.create(name="Retained task", owner=self.owner)
        TaskAssignment.objects.create(task=task, admin=self.owner, is_primary=True)
        with patch("apps.audit.delivery.enqueue_audit", side_effect=RuntimeError("outbox unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url(Task, "changelist"), {
                    "action": "delete_selected", "_selected_action": [task.pk], "post": "yes",
                })
        self.assertTrue(Task.objects.filter(pk=task.pk).exists())
        self.assertEqual(task.assignments.count(), 1)
        self.assertFalse(AuditLog.objects.exists())
        self.assertFalse(LogEntry.objects.exists())

    def test_account_admin_records_permissions_but_never_passwords(self):
        group = Group.objects.create(name="Operations readers")
        permission = Permission.objects.get(codename="view_scheduleprofile")
        data = {"username": self.owner.username, "employee_id": self.owner.employee_id, "display_name": "Renamed operator",
                "role": "ADMIN", "is_active_admin": "on", "is_active": "on", "groups": [group.pk],
                "user_permissions": [permission.pk], "date_joined_0": "2026-09-12", "date_joined_1": "08:00:00"}
        response = self.client.post(self.url(AdminUser, "change", self.owner), data)
        self.assertEqual(response.status_code, 302)
        audit = AuditLog.objects.get(action="ADMIN_ADMINUSER_UPDATED")
        self.assertEqual(audit.changes["after"]["groups"], [group.pk])
        self.assertEqual(audit.changes["after"]["user_permissions"], [permission.pk])
        self.assertEqual(audit.changes["before"]["display_name"], "Primary operator")
        self.assertNotIn('"password"', json.dumps(audit.changes))
        self.assertNotIn(self.owner.password, json.dumps(audit.delivery.payload))
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_staff)
        self.assertFalse(self.owner.is_superuser)

    def test_account_password_change_is_audited_without_credentials(self):
        response = self.client.post(reverse("admin:auth_user_password_change", args=[self.owner.pk]),
                                    {"password1": "new-fixture-pass-789", "password2": "new-fixture-pass-789", "usable_password": "true"})
        self.assertEqual(response.status_code, 302)
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.check_password("new-fixture-pass-789"))
        audit = AuditLog.objects.get(action="ADMIN_ADMINUSER_CREDENTIALS_UPDATED")
        payload = json.dumps(audit.delivery.payload)
        self.assertNotIn("new-fixture-pass-789", payload)
        self.assertNotIn(self.owner.password, payload)
        self.assertNotIn('"password"', payload)
