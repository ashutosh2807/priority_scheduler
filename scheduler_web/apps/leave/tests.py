import json
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import AdminRole, AdminUser
from apps.audit.models import AuditLog
from apps.scheduler.models import ScheduleProfile

from .models import LeaveRequest, LeaveScheduleCoverage, LeaveStatus


class LeaveWorkflowTests(TestCase):
    def setUp(self):
        self.agm = AdminUser.objects.create_user(
            username="approver", password="safe-password", employee_id="EMP200",
            display_name="Nisha Rao", role=AdminRole.SUPERUSER,
        )
        self.admin = AdminUser.objects.create_user(
            username="operator", password="safe-password", employee_id="EMP201",
            display_name="Kunal Shah",
        )
        self.cover_operator = AdminUser.objects.create_user(
            username="cover", password="safe-password", employee_id="EMP202",
            display_name="Aditi Sen",
        )
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.master_path = root / "Schedule_Master.json"
        self.state_path = root / "scheduler.db"
        self.master_path.write_text(json.dumps([{
            "id": 1, "name": "CONFIRMATION_REPORT", "package_name": "SCHEDULE_EXTRACTS.confirmation",
            "run_config": {"RUNS_ON": ["DAILY"]}, "margin": "T", "same_day": 1,
            "time_flag": 0, "is_active": 1, "confirmation_needed": 1,
        }, {
            "id": 2, "name": "AUTOMATED_REPORT", "package_name": "SCHEDULE_EXTRACTS.automated",
            "run_config": {"RUNS_ON": ["DAILY"]}, "margin": "T", "same_day": 1,
            "time_flag": 0, "is_active": 1, "confirmation_needed": 0,
        }]), encoding="utf-8")
        connection = sqlite3.connect(self.state_path)
        connection.executescript("""
            CREATE TABLE job_control (job_id INTEGER PRIMARY KEY, control_status TEXT, manual_run INTEGER, confirmation INTEGER, override_datetime TEXT, updated_at TEXT);
            CREATE TABLE staging_jobs (job_id INTEGER PRIMARY KEY, job_name TEXT, state TEXT, occurrence_date TEXT, execution_date TEXT, t_date TEXT, report_date TEXT, target_date TEXT, margin TEXT, confirmation_required INTEGER, confirmation_status TEXT, time_flag INTEGER, from_time TEXT, to_time TEXT, waiting_for TEXT, reason TEXT, next_evaluation TEXT, calculated_at TEXT, updated_at TEXT);
            CREATE TABLE ready_jobs (job_id INTEGER PRIMARY KEY, job_name TEXT, occurrence_date TEXT, execution_date TEXT, t_date TEXT, report_date TEXT, target_date TEXT, ready_since TEXT, time_priority INTEGER, date_priority INTEGER, job_priority INTEGER, priority_key TEXT, from_time TEXT, to_time TEXT, time_state TEXT, updated_at TEXT);
            CREATE TABLE execution_jobs (id INTEGER PRIMARY KEY, job_id INTEGER, job_name TEXT, procedure_name TEXT, report_date TEXT, status TEXT, attempt_no INTEGER, count INTEGER, started_at TEXT, finished_at TEXT, error TEXT, error_type TEXT, duration_seconds INTEGER, created_at TEXT, updated_at TEXT);
        """)
        connection.close()
        self.settings_override = override_settings(
            SCHEDULER_MASTER_PATH=self.master_path,
            SCHEDULER_STATE_DB_PATH=self.state_path,
            SCHEDULER_API_BASE_URL="",
            SCHEDULER_ALLOW_LOCAL_READ_FALLBACK=True,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def test_administrator_can_request_leave(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("leave:create"), {
            "start_date": date.today(), "end_date": date.today() + timedelta(days=1), "reason": "Family commitment",
        })
        request = LeaveRequest.objects.get()
        self.assertRedirects(response, reverse("leave:detail", args=[request.pk]))
        self.assertEqual(request.status, LeaveStatus.PENDING)
        self.assertTrue(AuditLog.objects.filter(action="LEAVE_CREATED").exists())

    def test_superuser_can_approve_pending_leave(self):
        leave = LeaveRequest.objects.create(
            admin=self.admin, start_date=date.today(), end_date=date.today(), reason="Medical appointment",
        )
        self.client.force_login(self.agm)
        response = self.client.post(reverse("leave:action", args=[leave.pk, "approve"]))
        self.assertRedirects(response, reverse("leave:detail", args=[leave.pk]))
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveStatus.APPROVED)
        self.assertEqual(leave.approved_by, self.agm)

    def test_confirmation_schedule_coverage_uses_only_available_operators(self):
        ScheduleProfile.objects.create(schedule_id=1, primary_operator=self.admin)
        leave = LeaveRequest.objects.create(
            admin=self.admin,
            start_date=date.today(),
            end_date=date.today() + timedelta(days=2),
            reason="Planned leave",
        )
        self.client.force_login(self.admin)

        response = self.client.get(reverse("leave:detail", args=[leave.pk]))

        self.assertContains(response, "CONFIRMATION_REPORT")
        self.assertContains(response, "Aditi Sen")
        self.assertNotContains(response, "AUTOMATED_REPORT")
        self.assertContains(response, "Coverage required")

        response = self.client.post(reverse("leave:coverage-assign", args=[leave.pk]), {
            "schedule_id": 1,
            "covering_admin": self.cover_operator.pk,
        })

        self.assertRedirects(response, reverse("leave:detail", args=[leave.pk]))
        coverage = LeaveScheduleCoverage.objects.get(leave_request=leave, schedule_id=1)
        self.assertEqual(coverage.covering_admin, self.cover_operator)
        self.assertTrue(AuditLog.objects.filter(action="LEAVE_SCHEDULE_COVER_ASSIGNED").exists())

        self.client.force_login(self.agm)
        response = self.client.post(reverse("leave:action", args=[leave.pk, "approve"]))
        self.assertRedirects(response, reverse("leave:detail", args=[leave.pk]))
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveStatus.APPROVED)
