from itertools import product

from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.leave.services import can_approve
from apps.scheduler.services import is_manager, is_operator
from .forms import AdministratorForm
from .models import AdminRole, AdminUser


class RolePermissionMatrixTests(SimpleTestCase):
    def test_active_status_and_authentication_flags_keep_their_existing_meaning(self):
        for role, active, active_admin, auth_superuser, staff in product(
            (AdminRole.ADMIN, AdminRole.SUPERUSER), (False, True),
            (False, True), (False, True), (False, True),
        ):
            with self.subTest(role=role, active=active, active_admin=active_admin,
                              auth_superuser=auth_superuser, staff=staff):
                user = AdminUser(role=role, is_active=active,
                                 is_active_admin=active_admin,
                                 is_superuser=auth_superuser, is_staff=staff)
                expected_operator = active and active_admin
                expected_manager = expected_operator and (
                    role == AdminRole.SUPERUSER or auth_superuser
                )
                self.assertEqual(is_operator(user), expected_operator)
                self.assertEqual(is_manager(user), expected_manager)
                self.assertEqual(can_approve(user), expected_manager)

        self.assertFalse(is_manager(AnonymousUser()))
        self.assertFalse(can_approve(AnonymousUser()))

    def test_only_the_new_role_is_a_current_choice(self):
        self.assertEqual(AdminRole.choices, [("ADMIN", "Admin"), ("SUPERUSER", "SUPERUSER")])
        self.assertEqual(AdministratorForm().fields["role"].choices,
                         [("ADMIN", "Admin"), ("SUPERUSER", "SUPERUSER")])
        self.assertFalse(is_manager(AdminUser(role="AGM")))


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RoleAccessTests(TestCase):
    def make_user(self, role, **flags):
        return AdminUser.objects.create_user(
            username=f"operator-{AdminUser.objects.count()}", employee_id=f"ROLE-{AdminUser.objects.count()}",
            display_name="Operations user", role=role, password="test-only", **flags,
        )

    def test_portal_superuser_role_does_not_grant_django_superuser_permissions(self):
        user = self.make_user(AdminRole.SUPERUSER)
        other = self.make_user(AdminRole.ADMIN)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.has_perm("accounts.delete_adminuser"))
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("accounts:create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts:detail", args=[other.pk])).status_code, 200)

    def test_ordinary_admin_remains_restricted_and_django_superuser_keeps_access(self):
        ordinary = self.make_user(AdminRole.ADMIN, is_staff=True)
        privileged = self.make_user(AdminRole.ADMIN, is_superuser=True)
        self.client.force_login(ordinary)
        self.assertEqual(self.client.get(reverse("accounts:create")).status_code, 403)
        self.assertEqual(self.client.get(reverse("accounts:detail", args=[ordinary.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts:detail", args=[privileged.pk])).status_code, 404)
        self.client.force_login(privileged)
        self.assertEqual(self.client.get(reverse("accounts:create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("accounts:detail", args=[ordinary.pk])).status_code, 200)

    def test_role_form_preserves_existing_identity_and_authentication_flags(self):
        user = self.make_user(AdminRole.ADMIN, is_staff=False, is_superuser=False)
        data = {
            "username": user.username, "employee_id": user.employee_id,
            "display_name": "AGM named operator", "designation": "AGM",
            "email": "agm@example.test", "role": "SUPERUSER",
            "is_active": True, "is_active_admin": True,
        }
        form = AdministratorForm(data=data, instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.pk, user.pk)
        self.assertEqual(saved.display_name, "AGM named operator")
        self.assertEqual(saved.designation, "AGM")
        self.assertEqual(saved.email, "agm@example.test")
        self.assertEqual(saved.role, AdminRole.SUPERUSER)
        self.assertFalse(saved.is_staff)
        self.assertFalse(saved.is_superuser)
        invalid = AdministratorForm(data={**data, "role": "AGM"}, instance=saved)
        self.assertFalse(invalid.is_valid())
        self.assertIn("role", invalid.errors)
