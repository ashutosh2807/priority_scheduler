from datetime import timedelta
from unittest.mock import patch

from django.apps import apps
from django.contrib import admin
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AdminUser
from .models import AuditDelivery, AuditLog


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CompleteAdminRegistryTests(TestCase):
    def setUp(self):
        self.superuser = AdminUser.objects.create_superuser(
            username="registry-manager", employee_id="REG1", display_name="Registry manager", password="fixture-only",
        )
        self.staff = AdminUser.objects.create_user(
            username="registry-reader", employee_id="REG2", display_name="Registry reader", is_staff=True,
        )
        self.client.force_login(self.superuser)

    def url(self, model, action, obj=None):
        return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_{action}", args=[obj.pk] if obj else [])

    def test_every_concrete_installed_model_is_registered_without_fake_oracle_models(self):
        installed = set(apps.get_models())
        self.assertEqual(installed, set(admin.site._registry))
        self.assertEqual(len(installed), 15)
        self.assertEqual(sum(model._meta.app_config.name.startswith("apps.") for model in installed), 10)

    def test_all_model_changelists_are_accessible_to_authorized_superuser(self):
        for model in apps.get_models():
            with self.subTest(model=model._meta.label):
                self.assertEqual(self.client.get(self.url(model, "changelist")).status_code, 200)

    def test_permission_content_type_and_native_admin_history_are_read_only(self):
        content_type = ContentType.objects.get_for_model(AdminUser)
        permission = Permission.objects.get(content_type=content_type, codename="view_adminuser")
        log = LogEntry.objects.create(user=self.superuser, content_type=content_type, object_id=str(self.staff.pk),
                                      object_repr="Registry reader", action_flag=ADDITION, change_message="Fixture record")
        for model, obj in ((Permission, permission), (ContentType, content_type), (LogEntry, log)):
            with self.subTest(model=model._meta.label):
                before = model.objects.values().get(pk=obj.pk)
                page = self.client.get(self.url(model, "change", obj))
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, 'name="_save"')
                self.assertEqual(self.client.post(self.url(model, "change", obj), {"name": "Forged"}).status_code, 403)
                self.assertEqual(self.client.post(self.url(model, "add"), {}).status_code, 403)
                self.assertEqual(self.client.post(self.url(model, "delete", obj), {"post": "yes"}).status_code, 403)
                self.assertEqual(model.objects.values().get(pk=obj.pk), before)

    def test_session_list_never_exposes_keys_payload_or_decoded_account_data(self):
        session = Session.objects.create(session_key="fixture-private-session-12345", session_data="fixture-private-session-payload",
                                         expire_date=timezone.now()+timedelta(hours=1))
        with patch.object(Session, "get_decoded", side_effect=AssertionError("Admin must not decode sessions")):
            page = self.client.get(self.url(Session, "changelist"))
        self.assertContains(page, "Expires at (IST)")
        self.assertNotContains(page, session.session_key)
        self.assertNotContains(page, session.session_data)
        self.assertNotContains(page, 'name="_selected_action"')
        self.assertEqual(self.client.get(self.url(Session, "change", session)).status_code, 403)
        self.assertEqual(self.client.get(self.url(Session, "history", session)).status_code, 403)
        self.assertEqual(self.client.post(self.url(Session, "add"), {}).status_code, 403)
        self.assertEqual(self.client.post(self.url(Session, "delete", session), {"post": "yes"}).status_code, 403)
        self.assertTrue(Session.objects.filter(pk=session.pk).exists())

    def test_added_registry_entries_keep_native_view_permissions(self):
        self.client.force_login(self.staff)
        for model in (Permission, ContentType, LogEntry, Session):
            with self.subTest(model=model._meta.label):
                self.assertEqual(self.client.get(self.url(model, "changelist")).status_code, 403)
        self.staff.user_permissions.add(Permission.objects.get(codename="view_session"))
        self.assertEqual(self.client.get(self.url(Session, "changelist")).status_code, 200)
        self.staff.refresh_from_db()
        self.assertFalse(self.staff.is_superuser)

    def test_group_permission_changes_are_durably_audited_with_affected_members(self):
        group = Group.objects.create(name="Report readers")
        self.staff.groups.add(group)
        permission = Permission.objects.get(codename="view_scheduleprofile")
        response = self.client.post(self.url(Group, "change", group), {"name": "Report operators", "permissions": [permission.pk]})
        self.assertEqual(response.status_code, 302)
        audit = AuditLog.objects.get(action="ADMIN_GROUP_UPDATED")
        self.assertEqual(audit.changes["before"]["permissions"], [])
        self.assertEqual(audit.changes["after"]["permissions"], [permission.pk])
        self.assertEqual(audit.changes["after"]["member_ids"], [self.staff.pk])
        self.assertEqual(audit.changes["after"]["name"], "Report operators")
        self.assertTrue(AuditDelivery.objects.filter(audit=audit, accepted_at__isnull=True).exists())

    def test_group_permission_change_rolls_back_if_audit_cannot_be_saved(self):
        group = Group.objects.create(name="Retained access")
        permission = Permission.objects.get(codename="view_scheduleprofile")
        with patch("apps.audit.delivery.enqueue_audit", side_effect=RuntimeError("outbox unavailable")):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url(Group, "change", group), {"name": "Should roll back", "permissions": [permission.pk]})
        group.refresh_from_db()
        self.assertEqual(group.name, "Retained access")
        self.assertFalse(group.permissions.exists())
        self.assertFalse(AuditLog.objects.exists())
