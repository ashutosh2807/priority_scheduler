from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import AdminRole, AdminUser
from apps.leave.models import LeaveRequest, LeaveStatus


class DashboardPageTests(TestCase):
    def setUp(self):
        self.owner = AdminUser.objects.create_user(
            username="agm", password="safe-password", employee_id="EMP100",
            display_name="Asha Verma", role=AdminRole.SUPERUSER,
        )
        delegate = AdminUser.objects.create_user(
            username="delegate", password="safe-password", employee_id="EMP101",
            display_name="Rahul Singh",
        )
        LeaveRequest.objects.create(
            admin=delegate, start_date=date.today(), end_date=date.today(),
            reason="Planned leave", status=LeaveStatus.APPROVED, approved_by=self.owner,
        )
        self.client.force_login(self.owner)

    def test_dashboard_displays_calendar_and_responsibility(self):
        response = self.client.get(reverse("dashboard:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Operations calendar")
        self.assertContains(response, "Rahul Singh")

    def test_full_calendar_displays_django_owned_events(self):
        response = self.client.get(reverse("dashboard:calendar"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rahul Singh on leave")
