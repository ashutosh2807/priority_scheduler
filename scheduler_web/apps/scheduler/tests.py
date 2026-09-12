import json
import sqlite3
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import AdminUser
from apps.audit.models import AuditLog

from .control_client import SchedulerApiClient
from .file_adapter import SchedulerApiReadAdapter, SchedulerProjectReadAdapter
from .forms import ScheduleMasterConfigForm
from .models import ScheduleProfile


class SchedulerUiTests(TestCase):
    def setUp(self):
        self.user = AdminUser.objects.create_user(
            username="scheduler_user", password="safe-password", employee_id="EMP300", display_name="Scheduler User",
        )
        self.client.force_login(self.user)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.master_path = root / "Schedule_Master.json"
        self.state_path = root / "scheduler.db"
        self.master_path.write_text(json.dumps([{
            "id": 1, "name": "DAILY_REPORT", "package_name": "SCHEDULE_EXTRACTS.report",
            "run_config": {"RUNS_ON": ["DAILY"], "RUN_BY": {"FROM_TIME": "09:00", "TO_TIME": "10:00"}},
            "margin": "T", "same_day": 1, "time_flag": 1, "is_active": 1,
            "created_date": "2026-09-11T09:00:00", "confirmation_needed": 0,
        }]), encoding="utf-8")
        self._create_scheduler_state()
        self.settings_override = override_settings(
            SCHEDULER_MASTER_PATH=self.master_path,
            SCHEDULER_STATE_DB_PATH=self.state_path,
            SCHEDULER_API_BASE_URL="",
            SCHEDULER_ALLOW_LOCAL_READ_FALLBACK=True,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def _create_scheduler_state(self):
        connection = sqlite3.connect(self.state_path)
        connection.executescript("""
            CREATE TABLE job_control (job_id INTEGER PRIMARY KEY, control_status TEXT, manual_run INTEGER, confirmation INTEGER, override_datetime TEXT, updated_at TEXT);
            CREATE TABLE staging_jobs (job_id INTEGER PRIMARY KEY, job_name TEXT, state TEXT, occurrence_date TEXT, execution_date TEXT, t_date TEXT, report_date TEXT, target_date TEXT, margin TEXT, confirmation_required INTEGER, confirmation_status TEXT, time_flag INTEGER, from_time TEXT, to_time TEXT, waiting_for TEXT, reason TEXT, next_evaluation TEXT, calculated_at TEXT, updated_at TEXT);
            CREATE TABLE ready_jobs (job_id INTEGER PRIMARY KEY, job_name TEXT, occurrence_date TEXT, execution_date TEXT, t_date TEXT, report_date TEXT, target_date TEXT, ready_since TEXT, time_priority INTEGER, date_priority INTEGER, job_priority INTEGER, priority_key TEXT, from_time TEXT, to_time TEXT, time_state TEXT, updated_at TEXT);
            CREATE TABLE execution_jobs (id INTEGER PRIMARY KEY, job_id INTEGER, job_name TEXT, procedure_name TEXT, report_date TEXT, status TEXT, attempt_no INTEGER, count INTEGER, started_at TEXT, finished_at TEXT, error TEXT, error_type TEXT, duration_seconds INTEGER, created_at TEXT, updated_at TEXT);
        """)
        connection.commit()
        connection.close()

    def test_monitoring_pages_render(self):
        for url_name in ("scheduler:dashboard", "scheduler:upcoming", "scheduler:jobs", "scheduler:queue", "scheduler:history", "scheduler:failed", "scheduler:confirmation"):
            with self.subTest(url_name=url_name):
                self.assertEqual(self.client.get(reverse(url_name)).status_code, 200)

    @patch("apps.scheduler.views.get_scheduler_control_client")
    def test_manual_run_is_sent_to_scheduler_control_api_and_audited(self, get_client):
        get_client.return_value.control.return_value = {
            "job_id": 1, "control_status": "ACTIVE", "manual_run": 1,
        }
        response = self.client.post(reverse("scheduler:action", args=[1, "manual_run"]), {"reason": "Corrected source"})
        self.assertRedirects(response, reverse("scheduler:detail", args=[1]))
        get_client.return_value.control.assert_called_once_with(
            1,
            "manual_run",
            override_datetime=None,
            actor="scheduler_user",
            reason="Corrected source",
        )
        connection = sqlite3.connect(self.state_path)
        row = connection.execute("SELECT control_status, manual_run FROM job_control WHERE job_id = 1").fetchone()
        connection.close()
        self.assertIsNone(row)
        self.assertTrue(AuditLog.objects.filter(action="SCHEDULE_MANUAL_RUN", object_id="1").exists())

    def test_schedule_creation_requires_manager_access(self):
        before = self.master_path.read_text(encoding="utf-8")
        self.assertEqual(self.client.get("/scheduler/jobs/new/").status_code, 403)
        self.assertEqual(self.master_path.read_text(encoding="utf-8"), before)

    def test_schedule_editor_keeps_description_and_steps_in_the_ui_layer(self):
        self.user.role = "SUPERUSER"
        self.user.save()
        response = self.client.post(reverse("scheduler:edit", args=[1]), {
            "description": "Produces the daily operations report.",
            "operational_steps": "Confirm source readiness\nReview reconciliation\nNotify the owner",
        })
        self.assertRedirects(response, reverse("scheduler:detail", args=[1]))
        profile = ScheduleProfile.objects.get(schedule_id=1)
        self.assertEqual(profile.description, "Produces the daily operations report.")
        self.assertEqual(profile.operational_steps, ["Confirm source readiness", "Review reconciliation", "Notify the owner"])

    def test_confirmation_schedule_requires_and_saves_a_primary_operator(self):
        self.user.role = "SUPERUSER"
        self.user.save()
        response = self.client.post(reverse("scheduler:edit", args=[1]), {
            "responsible_operator": self.user.pk,
        })

        self.assertRedirects(response, reverse("scheduler:detail", args=[1]))
        profile = ScheduleProfile.objects.get(schedule_id=1)
        self.assertEqual(profile.primary_operator, self.user)
        record = json.loads(self.master_path.read_text(encoding="utf-8"))[0]
        self.assertEqual(record["confirmation_needed"], 0)

    def test_runbook_editor_and_confirmation_gate_are_visible_to_operators(self):
        self.user.role = "SUPERUSER"
        self.user.save()
        records = json.loads(self.master_path.read_text(encoding="utf-8"))
        records[0]["confirmation_needed"] = 1
        self.master_path.write_text(json.dumps(records), encoding="utf-8")

        editor = self.client.get(reverse("scheduler:edit", args=[1]))
        detail = self.client.get(reverse("scheduler:detail", args=[1]))
        confirmation_queue = self.client.get(reverse("scheduler:confirmation"))

        self.assertContains(editor, 'data-runbook-builder')
        self.assertContains(editor, "Add another step")
        self.assertContains(detail, "background scheduler control API is not connected")
        self.assertContains(confirmation_queue, "Confirmation gates")

    @patch("apps.scheduler.views.get_scheduler_control_client")
    def test_confirmation_action_calls_only_the_scheduler_control_api(self, get_client):
        records = json.loads(self.master_path.read_text(encoding="utf-8"))
        records[0]["confirmation_needed"] = 1
        self.master_path.write_text(json.dumps(records), encoding="utf-8")
        ScheduleProfile.objects.create(schedule_id=1, primary_operator=self.user)
        connection = sqlite3.connect(self.state_path)
        connection.execute("ALTER TABLE staging_jobs ADD COLUMN occurrence_key TEXT")
        connection.execute("INSERT INTO staging_jobs (job_id, job_name, state, report_date, execution_date, occurrence_key) VALUES (1, 'DAILY_REPORT', 'WAITING_CONFIRMATION', '2026-09-10', '2026-09-11', 'report-1')")
        connection.commit()
        connection.close()

        get_client.return_value.control.return_value = {
            "job_id": 1, "control_status": "ACTIVE", "confirmation": 1,
        }
        response = self.client.post(reverse("scheduler:action", args=[1, "confirm"]), {"reason": "Source file checked", "occurrence_key": "report-1"})

        self.assertRedirects(response, reverse("scheduler:detail", args=[1]))
        get_client.return_value.control.assert_called_once_with(
            1,
            "confirm",
            override_datetime=None,
            actor="scheduler_user",
            reason="Source file checked",
            occurrence_key="report-1",
        )
        connection = sqlite3.connect(self.state_path)
        self.assertIsNone(connection.execute("SELECT confirmation FROM job_control WHERE job_id = 1").fetchone())
        connection.close()
        self.assertTrue(AuditLog.objects.filter(action="SCHEDULE_CONFIRM", object_id="1").exists())

    @patch("apps.scheduler.views.get_scheduler_control_client")
    def test_superuser_can_stop_future_cycles_without_local_scheduler_write(self, get_client):
        self.user.role = "SUPERUSER"
        self.user.save(update_fields=["role"])
        get_client.return_value.service_control.return_value = {
            "scheduler_enabled": 0,
            "updated_by": "scheduler_user",
            "reason": "Planned maintenance",
        }

        response = self.client.post(
            reverse("scheduler:service-action", args=["stop"]),
            {"reason": "Planned maintenance"},
        )

        self.assertRedirects(response, reverse("scheduler:dashboard"))
        get_client.return_value.service_control.assert_called_once_with(
            "stop",
            actor="scheduler_user",
            reason="Planned maintenance",
        )
        self.assertTrue(AuditLog.objects.filter(
            action="SCHEDULER_SERVICE_STOP", object_id="scheduler-service",
        ).exists())

    def test_non_agm_cannot_change_scheduler_wide_service_state(self):
        response = self.client.post(reverse("scheduler:service-action", args=["stop"]))

        self.assertEqual(response.status_code, 403)

    @patch("apps.scheduler.views.get_scheduler_control_client")
    def test_superuser_can_update_only_permitted_master_configuration_through_worker(self, get_client):
        self.user.role = "SUPERUSER"
        self.user.save(update_fields=["role"])
        before_master = self.master_path.read_text(encoding="utf-8")
        get_client.return_value.update_schedule_configuration.return_value = {
            "id": 1,
            "after": {
                "id": 1,
                "is_active": False,
                "run_by": {"from_time": "20:00", "to_time": "02:00"},
            },
        }

        response = self.client.post(
            reverse("scheduler:master-configuration", args=[1]),
            {
                "from_time": "20:00",
                "to_time": "02:00",
                "reason": "Overnight maintenance window",
            },
        )

        self.assertRedirects(response, reverse("scheduler:detail", args=[1]))
        get_client.return_value.update_schedule_configuration.assert_called_once_with(
            1,
            is_active=False,
            run_by={"from_time": "20:00", "to_time": "02:00"},
            actor="scheduler_user",
            reason="Overnight maintenance window",
        )
        self.assertEqual(self.master_path.read_text(encoding="utf-8"), before_master)
        self.assertTrue(AuditLog.objects.filter(
            action="SCHEDULE_MASTER_CONFIGURATION_UPDATED", object_id="1",
        ).exists())

    def test_non_agm_cannot_open_schedule_master_configuration(self):
        response = self.client.get(reverse("scheduler:master-configuration", args=[1]))

        self.assertEqual(response.status_code, 403)

    def test_master_configuration_form_rejects_incomplete_or_equal_window(self):
        incomplete = ScheduleMasterConfigForm({"is_active": "on", "from_time": "09:00"})
        equal = ScheduleMasterConfigForm({"is_active": "on", "from_time": "09:00", "to_time": "09:00"})

        self.assertFalse(incomplete.is_valid())
        self.assertFalse(equal.is_valid())

    def test_calendar_anchors_occurrence_on_report_date_not_execution_date(self):
        report_date = date.today()
        execution_date = report_date + timedelta(days=1)
        connection = sqlite3.connect(self.state_path)
        connection.execute(
            """INSERT INTO staging_jobs (job_id, job_name, state, occurrence_date, execution_date, report_date, calculated_at, updated_at)
               VALUES (1, 'DAILY_REPORT', 'WAITING_TIME', ?, ?, ?, '2026-09-11T09:00:00', '2026-09-11T09:00:00')""",
            (report_date.isoformat(), execution_date.isoformat(), report_date.isoformat()),
        )
        connection.commit()
        connection.close()
        days = SchedulerProjectReadAdapter().get_calendar_days(report_date.replace(day=1))
        self.assertIn(report_date, days)
        self.assertNotIn(execution_date, days)
        self.assertEqual(days[report_date]["occurrences"][0]["occurrence"]["execution_date"], execution_date.isoformat())

    def test_upcoming_page_uses_persisted_future_occurrences(self):
        report_date = date.today()
        execution_date = report_date + timedelta(days=2)
        connection = sqlite3.connect(self.state_path)
        connection.execute(
            """INSERT INTO staging_jobs (job_id, job_name, state, occurrence_date, execution_date, report_date,
                                          from_time, to_time, calculated_at, updated_at)
               VALUES (1, 'DAILY_REPORT', 'WAITING_TIME', ?, ?, ?, '09:00', '10:00', '2026-09-11T09:00:00', '2026-09-11T09:00:00')""",
            (report_date.isoformat(), execution_date.isoformat(), report_date.isoformat()),
        )
        connection.commit()
        connection.close()

        response = self.client.get(reverse("scheduler:upcoming"))

        self.assertContains(response, "Upcoming processes")
        self.assertContains(response, "DAILY_REPORT")
        self.assertContains(response, "Read-only scheduling view")
        self.assertContains(response, 'id="app-content"')

    def test_api_snapshot_is_normalized_without_reimplementing_scheduler_rules(self):
        report_date = date.today()

        class SnapshotClient:
            def snapshot(self):
                return {
                    "meta": {"generated_at": "2026-09-11T09:00:00", "queue_size": 1, "scheduler_interval_seconds": 180},
                    "schedule_master": [{
                        "id": 1, "name": "DAILY_REPORT", "package_name": "SCHEDULE_EXTRACTS.report",
                        "run_config": {"RUNS_ON": ["DAILY"]}, "margin": "T", "same_day": 1,
                        "time_flag": 0, "is_active": 1, "confirmation_needed": 0,
                    }],
                    "controls": [{"job_id": 1, "control_status": "ACTIVE", "manual_run": 0, "confirmation": 0}],
                    "staging": [],
                    "ready": [{
                        "job_id": 1, "job_name": "DAILY_REPORT", "report_date": report_date.isoformat(),
                        "execution_date": report_date.isoformat(), "priority_key": [1, 2, 3],
                    }],
                    "priority_queue": [{"job_id": 1, "priority_key": [1, 2, 3]}],
                    "executions": [],
                    "upcoming": [{
                        "job_id": 1,
                        "job_name": "DAILY_REPORT",
                        "occurrence_date": report_date.isoformat(),
                        "report_date": report_date.isoformat(),
                        "execution_date": (report_date + timedelta(days=2)).isoformat(),
                        "from_time": "09:00",
                        "to_time": "10:00",
                        "state": "WAITING_TIME",
                    }],
                    "service_control": {
                        "scheduler_enabled": 0,
                        "updated_at": "2026-09-11T10:15:00",
                        "updated_by": "ops-42",
                        "reason": "Planned maintenance",
                    },
                    "operation_audit": [{
                        "occurred_at": "2026-09-11T10:15:00",
                        "actor": "ops-42",
                        "action": "SCHEDULER_SERVICE_STOP",
                        "reason": "Planned maintenance",
                    }],
                }

        adapter = SchedulerApiReadAdapter(client=SnapshotClient())
        self.assertEqual(adapter.get_ready()[0]["queue_position"], 1)
        status = adapter.get_status()
        self.assertEqual(status["label"], "BACKGROUND SCHEDULER API")
        self.assertFalse(status["service_control"]["scheduler_enabled"])
        self.assertTrue(status["service_control"]["available"])
        self.assertEqual(status["operation_audit"][0]["actor"], "ops-42")
        future = adapter.get_upcoming_execution_plan(after=report_date)
        self.assertEqual(future[0]["name"], "DAILY_REPORT")
        self.assertEqual(future[0]["occurrence"]["execution_date"], (report_date + timedelta(days=2)).isoformat())
        self.assertEqual(future[0]["upcoming_source"], "SCHEDULER_FORECAST")

    def test_control_client_keeps_operator_metadata_optional(self):
        client = SchedulerApiClient(base_url="http://scheduler.invalid")
        with patch.object(client, "_request", return_value={"control": {"job_id": 1}}) as request:
            client.control(1, "pause", actor="scheduler_user", reason="Operations review")

        request.assert_called_once_with(
            "POST",
            "/v1/jobs/1/controls/pause",
            {
                "override_datetime": None,
                "actor": "scheduler_user",
                "reason": "Operations review",
            },
        )

    def test_service_control_client_forwards_actor_and_reason(self):
        client = SchedulerApiClient(base_url="http://scheduler.invalid")
        with patch.object(client, "_request", return_value={"service_control": {"scheduler_enabled": 0}}) as request:
            client.service_control("stop", actor="scheduler_user", reason="Planned maintenance")

        request.assert_called_once_with(
            "POST",
            "/v1/scheduler/controls/stop",
            {"actor": "scheduler_user", "reason": "Planned maintenance"},
        )

    def test_master_configuration_client_uses_bounded_patch_payload(self):
        client = SchedulerApiClient(base_url="http://scheduler.invalid")
        with patch.object(client, "_request", return_value={"configuration": {"id": 1}}) as request:
            client.update_schedule_configuration(
                1,
                is_active=False,
                run_by={"from_time": "20:00", "to_time": "02:00"},
                actor="scheduler_user",
                reason="Overnight maintenance window",
            )

        request.assert_called_once_with(
            "PATCH",
            "/v1/jobs/1/configuration",
            {
                "is_active": False,
                "run_by": {"from_time": "20:00", "to_time": "02:00"},
                "actor": "scheduler_user",
                "reason": "Overnight maintenance window",
            },
        )
