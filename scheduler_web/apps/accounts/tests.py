from django.test import TestCase
from django.urls import reverse

from .models import AdminRole, AdminUser


class AccountUiTests(TestCase):
    def test_login_screen_is_public(self):
        response = self.client.get(reverse("accounts:login"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in")

    def test_superuser_can_open_administrator_create_form(self):
        agm = AdminUser.objects.create_user(
            username="agm_account", password="safe-password", employee_id="EMP400",
            display_name="Account AGM", role=AdminRole.SUPERUSER,
        )
        self.client.force_login(agm)
        response = self.client.get(reverse("accounts:create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Save administrator")
