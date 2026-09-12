from datetime import date
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import AdminUser
from apps.leave.models import LeaveRequest, LeaveStatus
from apps.scheduler.control_client import SchedulerApiError
from apps.scheduler.file_adapter import SchedulerApiReadAdapter


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CalendarHolidayTests(TestCase):
    def setUp(self):
        self.user = AdminUser.objects.create_user(username="holiday-review", employee_id="CAL01", display_name="Calendar Operator")
        self.client.force_login(self.user)
        LeaveRequest.objects.create(admin=self.user, start_date=date(2026, 9, 11), end_date=date(2026, 9, 11), status=LeaveStatus.APPROVED)
        self.api = Mock()
        self.api.snapshot.return_value = {
            "schedule_master": [], "staging": [], "ready": [], "executions": [], "controls": [],
            "priority_queue": [], "meta": {},
        }
        self.api.calendar.return_value = {
            "occurrences": [],
            "calendar": {"holiday_source": "oracle", "holiday_count": 2},
            "calendar_days": [
                {"date": "2026-08-31", "kind": "HOLIDAY", "holiday_name": "Adjacent month holiday", "is_working_day": False},
                {"date": "2026-09-11", "kind": "HOLIDAY", "holiday_name": "Configured bank holiday", "is_working_day": False},
                {"date": "2026-09-12", "kind": "SAT", "holiday_name": None, "is_working_day": False},
                {"date": "2026-09-13", "kind": "SUN", "holiday_name": None, "is_working_day": False},
            ],
        }
        self.adapter = SchedulerApiReadAdapter(client=self.api)

    @patch("apps.dashboard.views.get_scheduler_read_adapter")
    def test_holidays_weekends_and_leave_coexist_in_calendar(self, factory):
        factory.return_value = self.adapter
        response = self.client.get(reverse("dashboard:calendar"), {"month": "2026-09", "date": "2026-09-11"})
        self.assertContains(response, "Configured bank holiday")
        self.assertContains(response, "Calendar Operator on leave")
        self.assertContains(response, "Saturday")
        self.assertContains(response, "Sunday")
        self.assertContains(response, "Tasks can still appear when their schedule allows holiday execution")
        self.assertContains(response, "Holiday source: Oracle")
        self.assertContains(response, "month=2026-08&date=2026-08-31")
        self.api.calendar.assert_called_once_with(date(2026, 8, 31), 35)
        cells = {cell["date"]: cell for week in response.context["calendar_weeks"] for cell in week}
        self.assertTrue(cells[date(2026, 9, 11)]["is_holiday"])
        self.assertFalse(cells[date(2026, 9, 12)]["is_holiday"])
        self.assertTrue(cells[date(2026, 9, 12)]["is_weekend"])

    @patch("apps.leave.views.get_scheduler_read_adapter")
    def test_leave_page_includes_same_holiday_calendar(self, factory):
        factory.return_value = self.adapter
        response = self.client.get(reverse("leave:list"), {"month": "2026-09"})
        self.assertContains(response, "Holiday and leave calendar")
        self.assertContains(response, "Configured bank holiday")
        self.assertContains(response, "Calendar Operator on leave")

    @patch("apps.dashboard.views.get_scheduler_read_adapter")
    def test_dashboard_includes_shared_markers(self, factory):
        factory.return_value = self.adapter
        with patch("apps.dashboard.views.timezone.localdate", return_value=date(2026, 9, 11)):
            response = self.client.get(reverse("dashboard:home"))
        self.assertContains(response, "Configured bank holiday")

    @patch("apps.dashboard.views.get_scheduler_read_adapter")
    def test_empty_retained_source_is_not_presented_as_complete_holiday_calendar(self, factory):
        self.api.calendar.return_value["calendar"].update(holiday_source="retained_snapshot", holiday_count=0)
        self.api.calendar.return_value["calendar_days"] = self.api.calendar.return_value["calendar_days"][2:]
        factory.return_value = self.adapter
        response = self.client.get(reverse("dashboard:calendar"), {"month": "2026-09"})
        self.assertContains(response, "Retained holiday snapshot")
        self.assertContains(response, "No configured holiday dates loaded")
        self.assertNotContains(response, "bank-holiday")

    @patch("apps.dashboard.views.get_scheduler_read_adapter")
    def test_unavailable_worker_does_not_invent_holidays(self, factory):
        self.api.calendar.side_effect = SchedulerApiError("Calendar unavailable")
        factory.return_value = self.adapter
        response = self.client.get(reverse("dashboard:calendar"), {"month": "2026-09"})
        self.assertContains(response, "Holiday markers unavailable")
        self.assertNotContains(response, "bank-holiday")

    @patch("apps.dashboard.views.get_scheduler_read_adapter")
    def test_bank_holidays_working_saturdays_and_t_minus_one_explanations(self, factory):
        self.api.calendar.return_value["calendar"].update(
            holiday_count=0, holiday_rule_source="datemast_and_bank_calendar", holiday_observed_through="2026-09-10")
        self.api.calendar.return_value["calendar_days"] = [
            {"date": "2026-09-09", "kind": "HOLIDAY", "is_holiday": True,
             "holiday_name": "Holiday · DATEMAST", "reason": "Absent from DATEMAST before today.", "is_provisional": False},
            {"date": "2026-09-11", "kind": "WORKING_DAY", "is_holiday": False,
             "day_label": "Working day", "reason": "Today is not published yet.", "is_provisional": True},
            {"date": "2026-09-12", "kind": "SAT", "is_holiday": True,
             "holiday_name": "2nd Saturday", "reason": "Default bank holiday.", "is_provisional": True},
            {"date": "2026-09-19", "kind": "WORKING_DAY", "is_holiday": False,
             "day_label": "Working Saturday", "reason": "3rd Saturday is normally working.", "is_provisional": True},
            {"date": "2026-09-26", "kind": "SAT", "is_holiday": True,
             "holiday_name": "4th Saturday", "reason": "Default bank holiday.", "is_provisional": True},
        ]
        factory.return_value = self.adapter
        with patch("apps.dashboard.views.timezone.localdate", return_value=date(2026, 9, 11)):
            response = self.client.get(reverse("dashboard:calendar"), {"month": "2026-09", "date": "2026-09-12"})
        self.assertContains(response, "DATEMAST + bank calendar")
        self.assertContains(response, "10/09/2026 (T−1)")
        self.assertContains(response, "Holiday · DATEMAST")
        self.assertContains(response, "2nd Saturday")
        self.assertContains(response, "4th Saturday")
        self.assertContains(response, "Working Saturday")
        self.assertContains(response, "Provisional bank calendar")
        self.assertNotContains(response, "No configured holiday dates loaded")
        cells = {cell["date"]: cell for week in response.context["calendar_weeks"] for cell in week}
        self.assertTrue(cells[date(2026, 9, 9)]["is_holiday"])
        self.assertTrue(cells[date(2026, 9, 12)]["is_holiday"])
        self.assertFalse(cells[date(2026, 9, 19)]["is_holiday"])
        self.assertFalse(cells[date(2026, 9, 11)]["is_holiday"])
        self.assertTrue(response.context["selected_is_holiday"])

    @patch("apps.leave.views.get_scheduler_read_adapter")
    def test_shared_grid_uses_worker_operating_date_for_t_plus_one_tasks(self, factory):
        self.api.calendar.return_value["occurrences"] = [{
            "job_id": 1, "occurrence_key": "1:2026-09-09", "report_date": "2026-09-09",
            "execution_date": "2026-09-11", "calendar_date": "2026-09-11", "state": "SUCCESS",
        }]
        factory.return_value = self.adapter
        response = self.client.get(reverse("leave:list"), {"month": "2026-09"})
        cells = {cell["date"]: cell for week in response.context["calendar_weeks"] for cell in week}
        self.assertIsNone(cells[date(2026, 9, 9)]["scheduler_summary"])
        self.assertEqual(cells[date(2026, 9, 11)]["scheduler_summary"]["scheduled"], 1)
        self.assertEqual(cells[date(2026, 9, 11)]["scheduler_summary"]["success"], 1)

    @patch("apps.dashboard.views.get_scheduler_read_adapter")
    def test_bank_defaults_do_not_claim_unavailable_datemast_history(self, factory):
        self.api.calendar.return_value["calendar"].update(holiday_rule_source="bank_calendar", holiday_observed_through=None)
        factory.return_value = self.adapter
        response = self.client.get(reverse("dashboard:calendar"), {"month": "2026-09"})
        self.assertContains(response, "Bank calendar defaults · DATEMAST history unavailable")
        self.assertNotContains(response, "DATEMAST + bank calendar")

    def test_calendar_dashboard_and_leave_show_loaded_and_assessed_ranges(self):
        self.api.calendar.return_value["calendar"].update(
            holiday_rule_source="datemast_and_bank_calendar",
            coverage_start="2025-03-31", coverage_end="2026-09-12",
            holiday_observed_from="2025-03-31", holiday_observed_through="2026-09-11",
        )
        with patch("apps.dashboard.views.get_scheduler_read_adapter", return_value=self.adapter), \
             patch("apps.leave.views.get_scheduler_read_adapter", return_value=self.adapter), \
             patch("apps.dashboard.views.timezone.localdate", return_value=date(2026, 9, 12)):
            for route in ("dashboard:calendar", "dashboard:home", "leave:list"):
                with self.subTest(route=route):
                    response = self.client.get(reverse(route), {"month": "2026-09"})
                    self.assertContains(response, "Loaded DATEMAST range:")
                    self.assertContains(response, "<strong>31/03/2025</strong> to <strong>12/09/2026</strong>")
                    self.assertContains(response, "Past dates assessed from 31/03/2025 through 11/09/2026 (T−1)")
                    self.assertContains(response, "Older dates outside loaded history, today and future dates use provisional bank defaults.")

    @patch("apps.dashboard.views.get_scheduler_read_adapter")
    def test_selected_older_date_preserves_worker_provisional_reason_without_false_holiday(self, factory):
        self.api.calendar.return_value["calendar"].update(
            holiday_rule_source="datemast_and_bank_calendar",
            coverage_start="2025-03-31", coverage_end="2026-09-12",
            holiday_observed_from="2025-03-31", holiday_observed_through="2026-09-11",
        )
        day_info = {
            "date": "2025-03-03", "kind": "WORKING_DAY", "is_holiday": False,
            "day_label": "Working day", "is_provisional": True,
            "reason": "Outside loaded DATEMAST history; bank-calendar defaults apply.",
        }
        self.api.calendar.return_value["calendar_days"] = [day_info]
        factory.return_value = self.adapter
        response = self.client.get(reverse("dashboard:calendar"), {"month": "2025-03", "date": "2025-03-03"})
        self.assertContains(response, day_info["reason"])
        self.assertContains(response, "Provisional bank calendar")
        self.assertNotContains(response, "bank-holiday")
        self.assertEqual(response.context["selected_calendar_info"], day_info)
        self.assertFalse(response.context["selected_is_holiday"])
