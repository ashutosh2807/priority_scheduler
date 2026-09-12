from itertools import product

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from apps.audit.models import AuditDelivery, AuditLog


class SuperuserRoleMigrationTests(TransactionTestCase):
    migrate_from = [("accounts", "0001_initial")]
    migrate_to = [("accounts", "0002_rename_agm_role_superuser")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        self.latest_migrations = executor.loader.graph.leaf_nodes()
        self.addCleanup(self.restore_latest_schema)
        executor.migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def restore_latest_schema(self):
        MigrationExecutor(connection).migrate(self.latest_migrations)

    def user_snapshot(self, user_model):
        return {row["id"]: row for row in user_model.objects.values()}

    def test_forward_and_reverse_change_only_role_and_preserve_history(self):
        user_model = self.old_apps.get_model("accounts", "AdminUser")
        group = Group.objects.create(name="Preserved permissions")
        content_type, _ = ContentType.objects.get_or_create(app_label="accounts", model="adminuser")
        permission, _ = Permission.objects.get_or_create(
            content_type=content_type, codename="view_adminuser",
            defaults={"name": "Can view administrator"},
        )
        group.permissions.add(permission)
        for index, flags in enumerate(product((False, True), repeat=4)):
            user = user_model.objects.create(
                username=f"agm-{index}", employee_id=f"LEGACY-{index}",
                display_name=f"AGM operator {index}", designation="AGM", role="AGM",
                email=f"agm-{index}@example.test", password="preserved-password-hash",
                last_login=timezone.now(), first_name="AGM", last_name="Operator",
                is_active=flags[0], is_active_admin=flags[1],
                is_staff=flags[2], is_superuser=flags[3],
            )
            user.groups.add(group.pk)
            user.user_permissions.add(permission.pk)
        user_model.objects.create(username="ordinary-agm", employee_id="ORDINARY",
                                  display_name="AGM name retained", role="ADMIN")
        before = self.user_snapshot(user_model)
        groups_before = set(user_model.groups.through.objects.values_list("adminuser_id", "group_id"))
        permissions_before = set(user_model.user_permissions.through.objects.values_list("adminuser_id", "permission_id"))
        audit = AuditLog.objects.create(
            actor_id=user.pk, action="ADMINISTRATOR_UPDATED", object_type="accounts.AdminUser",
            object_id=str(user.pk), object_label="AGM operator", reason="Approved by AGM",
            changes={"before": {"role": "ADMIN"}, "after": {"role": "AGM"}},
        )
        AuditDelivery.objects.create(audit=audit, payload={"actor": user.username, "role": "AGM"})
        audit_before = AuditLog.objects.values().get(pk=audit.pk)
        delivery_before = AuditDelivery.objects.values().get(audit_id=audit.pk)

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        migrated_user = executor.loader.project_state(self.migrate_to).apps.get_model("accounts", "AdminUser")
        expected = {key: {**row, "role": "SUPERUSER" if row["role"] == "AGM" else row["role"]}
                    for key, row in before.items()}
        self.assertEqual(self.user_snapshot(migrated_user), expected)
        self.assertEqual(migrated_user._meta.get_field("role").choices,
                         [("ADMIN", "Admin"), ("SUPERUSER", "SUPERUSER")])
        self.assertEqual(set(migrated_user.groups.through.objects.values_list("adminuser_id", "group_id")), groups_before)
        self.assertEqual(set(migrated_user.user_permissions.through.objects.values_list("adminuser_id", "permission_id")), permissions_before)
        self.assertEqual(list(group.permissions.values_list("pk", flat=True)), [permission.pk])
        self.assertEqual(AuditLog.objects.values().get(pk=audit.pk), audit_before)
        self.assertEqual(AuditDelivery.objects.values().get(audit_id=audit.pk), delivery_before)

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        restored_user = executor.loader.project_state(self.migrate_from).apps.get_model("accounts", "AdminUser")
        self.assertEqual(self.user_snapshot(restored_user), before)
        self.assertEqual(set(restored_user.groups.through.objects.values_list("adminuser_id", "group_id")), groups_before)
        self.assertEqual(set(restored_user.user_permissions.through.objects.values_list("adminuser_id", "permission_id")), permissions_before)
        self.assertEqual(AuditLog.objects.values().get(pk=audit.pk), audit_before)
        self.assertEqual(AuditDelivery.objects.values().get(audit_id=audit.pk), delivery_before)
