from datetime import timedelta
from io import StringIO
import json
from unittest.mock import Mock, patch
import uuid

from django.core.management import call_command
from django.db import transaction
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AdminUser
from apps.leave.models import LeaveRequest, LeaveStatus
from apps.leave.services import reject
from apps.scheduler.control_client import SchedulerApiClient, SchedulerApiError
from .delivery import backfill_audits, deliver_batch, delivery_status, enqueue_audit
from .models import AuditLog, AuditDelivery
from .services import record_action, record_external_action


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuditDeliveryTests(TestCase):
    def setUp(self):
        self.actor = AdminUser.objects.create_user(username="audit-operator", employee_id="AUD-1", display_name="Audit operator", role="SUPERUSER")
        self.transport = Mock(timeout=3)

    def event(self, **changes):
        return record_external_action(self.actor, "SCHEDULE_CONFIRM", object_type="scheduler.ScheduleMaster",
                                      object_id="12", object_label="Daily report", reason="Inputs checked",
                                      changes={"report_date": "2026-09-12", "occurrence_key": "daily-12", **changes})

    def test_audit_and_delivery_share_a_transaction_without_network(self):
        with patch("apps.audit.delivery.SchedulerApiClient") as client:
            entry = self.event()
            client.assert_not_called()
        event = entry.delivery.payload
        self.assertEqual(event["source"], "PORTAL")
        self.assertEqual(event["event_type"], "SCHEDULE_CONFIRM")
        self.assertEqual(event["job_id"], 12)
        self.assertEqual(event["report_date"], "2026-09-12")
        self.assertEqual(event["name"], "Daily report")
        self.assertNotIn("job_name", event)
        self.assertEqual(event["correlation_id"], event["event_id"])
        self.assertEqual(event["payload"]["changes"]["occurrence_key"], "daily-12")
        self.assertNotIn("record_key", event)
        self.assertEqual(event["actor"], self.actor.username)
        self.assertEqual(event["payload"]["portal_audit_id"], entry.pk)
        self.assertEqual(str(uuid.UUID(event["event_id"])), str(entry.delivery.event_id))

    def test_outbox_failure_rolls_back_audit_and_local_leave_change(self):
        leave = LeaveRequest.objects.create(admin=self.actor, start_date=timezone.localdate(), end_date=timezone.localdate())
        with patch("apps.audit.delivery.enqueue_audit", side_effect=RuntimeError("outbox unavailable")):
            with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                reject(leave, self.actor)
        leave.refresh_from_db()
        self.assertEqual(leave.status, LeaveStatus.PENDING)
        self.assertFalse(AuditLog.objects.exists())

    def test_outer_rollback_discards_audit_and_delivery_together(self):
        with self.assertRaises(ValueError):
            with transaction.atomic():
                self.event()
                raise ValueError("operation rolled back")
        self.assertEqual(AuditLog.objects.count(), 0)
        self.assertEqual(AuditDelivery.objects.count(), 0)

    def test_replay_after_lost_ack_uses_same_event_and_does_not_duplicate(self):
        entry = self.event()
        delivery = entry.delivery
        frozen = json.loads(json.dumps(delivery.payload))
        self.actor.username = "renamed-operator"
        self.actor.save(update_fields=["username"])
        now = timezone.now()
        self.transport.submit_logging_events.side_effect = [SchedulerApiError("Acknowledgement lost"), {"accepted": [str(delivery.event_id)]}]
        first = deliver_batch(self.transport, now=now)
        self.assertEqual(first, {"attempted": 1, "accepted": 0, "pending": 1})
        self.assertEqual(deliver_batch(self.transport, now=now+timedelta(seconds=4))["attempted"], 0)
        self.assertEqual(deliver_batch(self.transport, now=now+timedelta(seconds=5))["accepted"], 1)
        self.assertEqual(self.transport.submit_logging_events.call_args_list[0].args[0], [frozen])
        self.assertEqual(self.transport.submit_logging_events.call_args_list[1].args[0], [frozen])
        self.assertEqual(deliver_batch(self.transport, now=now+timedelta(days=1))["attempted"], 0)
        self.assertEqual(AuditDelivery.objects.count(), 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempts, 2)
        self.assertIsNotNone(delivery.accepted_at)
        self.assertEqual(delivery.last_error, "")

    def test_partial_ack_retries_only_unaccepted_events(self):
        first, second = self.event(), self.event()
        self.transport.submit_logging_events.return_value = {"accepted": [str(first.delivery.event_id)]}
        result = deliver_batch(self.transport)
        self.assertEqual(result["accepted"], 1)
        self.assertIsNone(AuditDelivery.objects.get(audit=second).accepted_at)
        self.assertEqual(delivery_status()["pending"], 1)

    def test_large_batches_split_without_losing_or_changing_event_ids(self):
        first, second = self.event(), self.event()
        limit = len(json.dumps(first.delivery.payload).encode()) + 50
        self.transport.submit_logging_events.return_value = {"accepted": [str(first.delivery.event_id)]}
        with patch("apps.audit.delivery.MAX_BATCH_BYTES", limit):
            result = deliver_batch(self.transport)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(self.transport.submit_logging_events.call_args.args[0]), 1)
        second_delivery = AuditDelivery.objects.get(audit=second)
        self.assertIsNone(second_delivery.accepted_at)
        self.assertIsNone(second_delivery.lease_token)
        self.assertEqual(second_delivery.attempts, 0)

    def test_oversized_event_is_retained_and_smaller_events_can_progress(self):
        oversized, small = self.event(large="x" * 3000), self.event()
        self.transport.submit_logging_events.return_value = {"accepted": [str(small.delivery.event_id)]}
        with patch("apps.audit.delivery.MAX_BATCH_BYTES", 1500):
            result = deliver_batch(self.transport)
        self.assertEqual(result["accepted"], 1)
        oversized_delivery = AuditDelivery.objects.get(audit=oversized)
        self.assertIsNone(oversized_delivery.accepted_at)
        self.assertIn("retained locally", oversized_delivery.last_error)

    def test_invalid_ack_and_foreign_ids_never_mark_event_delivered(self):
        entry = self.event()
        self.transport.submit_logging_events.return_value = {"accepted": "not-a-list"}
        now = timezone.now()
        self.assertEqual(deliver_batch(self.transport, now=now)["accepted"], 0)
        self.transport.submit_logging_events.return_value = {"accepted": [str(uuid.uuid4())]}
        self.assertEqual(deliver_batch(self.transport, now=now+timedelta(seconds=5))["accepted"], 0)
        self.assertIsNone(AuditDelivery.objects.get(audit=entry).accepted_at)

    def test_active_lease_is_respected_and_expired_lease_recovers_after_crash(self):
        entry = self.event()
        now = timezone.now()
        AuditDelivery.objects.filter(audit=entry).update(lease_token=uuid.uuid4(), leased_until=now+timedelta(seconds=60))
        self.assertEqual(deliver_batch(self.transport, now=now)["attempted"], 0)
        self.transport.submit_logging_events.return_value = {"accepted": [str(entry.delivery.event_id)]}
        self.assertEqual(deliver_batch(self.transport, now=now+timedelta(seconds=61))["accepted"], 1)

    def test_backfill_and_enqueue_are_idempotent_and_freeze_existing_payload(self):
        entry = AuditLog.objects.create(actor=self.actor, action="LEGACY_NOTE", object_type="leave.LeaveRequest", object_id="5", object_label="Historic leave", changes={"status": "APPROVED"})
        self.assertEqual(delivery_status()["awaiting_backfill"], 1)
        self.assertEqual(backfill_audits(), 1)
        delivery = AuditDelivery.objects.get(audit=entry)
        entry.reason = "Changed after enqueue"
        entry.save(update_fields=["reason"])
        self.assertEqual(enqueue_audit(entry).payload, delivery.payload)
        self.assertEqual(backfill_audits(), 0)
        self.assertEqual(AuditDelivery.objects.count(), 1)

    def test_leave_snapshot_is_serializable_and_credentials_are_excluded(self):
        self.actor.set_password("Never put this into a log")
        entry = record_action(self.actor, "ACCOUNT_TEST", self.actor)
        self.assertNotIn("password", entry.changes["snapshot"])
        self.assertNotIn("Never put this", json.dumps(entry.delivery.payload))
        leave = LeaveRequest.objects.create(admin=self.actor, start_date=timezone.localdate(), end_date=timezone.localdate())
        entry = record_action(self.actor, "LEAVE_CREATED", leave)
        self.assertEqual(entry.changes["snapshot"]["start_date"], timezone.localdate().isoformat())

    @patch("apps.audit.management.commands.deliver_audit_events.deliver_batch")
    def test_one_shot_command_backfills_before_delivering(self, delivery):
        AuditLog.objects.create(action="LEGACY", object_type="scheduler.Queue", object_id="queue", object_label="Queue")
        delivery.return_value = {"attempted": 1, "accepted": 0, "pending": 1}
        stream = StringIO()
        call_command("deliver_audit_events", stdout=stream)
        self.assertEqual(AuditDelivery.objects.count(), 1)
        self.assertIn("retained for retry", stream.getvalue())

    def test_account_create_and_update_audit_only_allowed_fields(self):
        self.client.force_login(self.actor)
        fields = {"username": "new-operator", "employee_id": "AUD-2", "display_name": "New Operator", "role": "ADMIN", "is_active": "on", "is_active_admin": "on", "password1": "Complete_Test_Password_936", "password2": "Complete_Test_Password_936"}
        response = self.client.post(reverse("accounts:create"), fields)
        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(action="ADMINISTRATOR_CREATED")
        encoded = json.dumps(entry.delivery.payload)
        self.assertNotIn("password", encoded)
        self.assertNotIn(fields["password1"], encoded)
        created = AdminUser.objects.get(username="new-operator")
        response = self.client.post(reverse("accounts:edit", args=[created.pk]), {**fields, "display_name": "Updated Operator"})
        self.assertEqual(response.status_code, 302)
        entry = AuditLog.objects.get(action="ADMINISTRATOR_UPDATED")
        self.assertEqual(entry.changes["before"]["display_name"], "New Operator")
        self.assertEqual(entry.changes["after"]["display_name"], "Updated Operator")

    def test_status_distinguishes_worker_acceptance_from_oracle_pending(self):
        html = render_to_string("audit/_delivery_notice.html", {"portal_audit_delivery": {"pending": 2},
                                "oracle_logging": {"enabled": True, "pending": 4, "delivered": 9, "last_error": "Oracle unavailable"}})
        self.assertIn("2 portal events awaiting worker", html)
        self.assertIn("4 awaiting Oracle", html)
        self.assertIn("Oracle logging needs attention", html)
        self.assertIn("Oracle unavailable", html)

    @patch.object(SchedulerApiClient, "_request")
    def test_client_uses_logging_contract(self, request):
        request.return_value = {"accepted": []}
        client = SchedulerApiClient()
        self.assertEqual(client.submit_logging_events([]), {"accepted": []})
        request.assert_called_with("POST", "/v1/logging/events", {"events": []})
        client.logging_status()
        request.assert_called_with("GET", "/v1/logging/status")
