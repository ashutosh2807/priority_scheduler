"""Logging API contracts use a private SQLite fixture and no Oracle connection."""
import json
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from control_api import SchedulerControlApi, start_control_api
from repositories.oracle_logging_repository import OracleLoggingRepository


def portal_event(**extra):
    identity = str(uuid.uuid4())
    return {"event_id": identity, "event_type": "SCHEDULE_CONFIRM", "source": "PORTAL",
            "occurred_at": "2026-09-12T09:30:00+05:30", "actor": "fixture-operator",
            "reason": "Inputs checked", "correlation_id": identity, "job_id": 7,
            "name": "Daily report", "report_date": "2026-09-11",
            "payload": {"portal_audit_id": 1, "object_type": "scheduler.ScheduleMaster",
                        "object_id": "7", "changes": {"occurrence_key": "7:2026-09-11:daily"}}, **extra}


class LoggingApiFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="scheduler-logging-api-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "worker.sqlite3"
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.addCleanup(self.connection.close)
        self.oracle_connect = Mock(side_effect=AssertionError("Contract tests must never connect to Oracle"))
        self.repository = OracleLoggingRepository(connection=self.connection, enabled=False,
                                                   connection_factory=self.oracle_connect)
        empty = SimpleNamespace(get_all=lambda: [])
        self.application = {name: empty for name in (
            "schedule_master_repository", "job_control_repository", "staging_repository",
            "ready_repository", "priority_queue", "execution_repository",
        )}
        self.application.update(oracle_logging=self.repository, execution_enabled=False, mode="monitor")
        self.api = SchedulerControlApi(self.application)

    def persisted_events(self):
        # A separate connection proves the ACK follows COMMIT, not just INSERT.
        with closing(sqlite3.connect(self.path)) as reader:
            return [json.loads(row[0]) for row in reader.execute(
                "SELECT event_json FROM scheduler_oracle_log_outbox ORDER BY event_seq"
            )]


class LoggingApiContractTests(LoggingApiFixture, unittest.TestCase):
    def test_batch_acknowledges_committed_rows_before_any_oracle_delivery(self):
        events = [portal_event(), portal_event(event_type="QUEUE_REORDERED")]
        response = self.api.accept_logging_events(events)
        self.assertEqual(response, {"accepted": [event["event_id"] for event in events]})
        persisted = self.persisted_events()
        self.assertEqual([row["event_id"] for row in persisted], response["accepted"])
        self.assertEqual(persisted[0]["payload"], events[0]["payload"])
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.repository.stats()["pending"], 2)
        self.assertEqual(self.repository.stats()["delivered"], 0)
        self.oracle_connect.assert_not_called()

    def test_identical_retry_returns_original_ids_without_duplicate_rows(self):
        event = portal_event()
        first = self.api.accept_logging_events([event])
        self.assertEqual(self.api.accept_logging_events([event]), first)
        self.assertEqual(len(self.persisted_events()), 1)

    def test_active_outer_transaction_cannot_receive_a_durable_ack(self):
        self.connection.execute("BEGIN")
        try:
            with self.assertRaises(RuntimeError):
                self.api.accept_logging_events([portal_event()])
            self.assertEqual(self.persisted_events(), [])
            self.assertTrue(self.connection.in_transaction)
        finally:
            self.connection.rollback()

    def test_reused_identity_with_changed_payload_is_rejected(self):
        event = portal_event()
        self.api.accept_logging_events([event])
        with self.assertRaises(ValueError):
            self.api.accept_logging_events([{**event, "reason": "Different operation"}])
        self.assertEqual(self.persisted_events()[0]["reason"], event["reason"])

    def test_invalid_later_event_rolls_back_entire_batch(self):
        first, invalid = portal_event(), portal_event(payload=[])
        with self.assertRaises(ValueError):
            self.api.accept_logging_events([first, invalid])
        self.assertEqual(self.persisted_events(), [])
        self.assertFalse(self.connection.in_transaction)

    def test_identity_conflict_rolls_back_new_rows_in_same_batch(self):
        existing = portal_event()
        self.api.accept_logging_events([existing])
        with self.assertRaises(ValueError):
            self.api.accept_logging_events([portal_event(), {**existing, "event_type": "OTHER_ACTION"}])
        self.assertEqual(len(self.persisted_events()), 1)

    def test_endpoint_rejects_source_and_current_state_injection(self):
        for event in (portal_event(source="WORKER"), portal_event(source="CONTROL_API"),
                      portal_event(record_key="occurrence:7:2026-09-11")):
            with self.subTest(event=event):
                with self.assertRaises(ValueError):
                    self.api.accept_logging_events([event])
        self.assertEqual(self.persisted_events(), [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scheduler_oracle_log_state").fetchone()[0], 0)

    def test_batch_shape_original_uuid_and_field_contract_are_enforced(self):
        for events in (None, {}, [], [portal_event()] * 201, [None],
                       [portal_event(event_id=None)], [portal_event(event_id="not-an-original-uuid")],
                       [portal_event(job_name="Unsupported alias")]):
            with self.subTest(events=str(events)[:100]):
                with self.assertRaises((ValueError, TypeError)):
                    self.api.accept_logging_events(events)
        self.assertEqual(self.persisted_events(), [])

    def test_logging_status_and_snapshot_report_pending_separately_from_delivered(self):
        self.api.accept_logging_events([portal_event()])
        status = self.api.logging_status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["pending"], 1)
        self.assertEqual(status["delivered"], 0)
        self.assertIn("last_delivered_at", status)
        self.assertIsNone(status["last_delivered_at"])
        with patch.object(self.api, "calendar_metadata", return_value={}):
            self.assertEqual(self.api.snapshot()["meta"]["oracle_logging"], status)

    def test_unconfigured_logger_never_acknowledges(self):
        missing = SchedulerControlApi({})
        with self.assertRaises(RuntimeError):
            missing.accept_logging_events([portal_event()])
        self.assertFalse(missing.logging_status()["enabled"])


class LoggingHttpContractTests(LoggingApiFixture, unittest.TestCase):
    # Run only HTTP-specific tests here; domain tests stay in their own class.
    def setUp(self):
        super().setUp()
        with patch.dict("os.environ", {"SCHEDULER_API_TOKEN": "fixture-token"}):
            self.server = start_control_api(self.application, self.api, host="127.0.0.1", port=0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, payload=None, *, method="POST", token="fixture-token", raw=None, headers=None):
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if token is not None:
            request_headers["X-Scheduler-Token"] = token
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        request = Request(self.url+path, data=data, method=method, headers=request_headers)
        try:
            with urlopen(request, timeout=3) as response:  # nosec B310 -- private ephemeral fixture
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_http_authenticated_batch_and_status_routes(self):
        event = portal_event()
        status, body = self.request("/v1/logging/events", {"events": [event]})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"accepted": [event["event_id"]]})
        status, body = self.request("/v1/logging/status", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(body["pending"], 1)
        self.assertFalse(body["enabled"])

    def test_http_rejects_missing_or_wrong_token_on_read_and_write(self):
        for token in (None, "incorrect-token"):
            self.assertEqual(self.request("/v1/logging/events", {"events": [portal_event()]}, token=token)[0], 401)
            self.assertEqual(self.request("/v1/logging/status", method="GET", token=token)[0], 401)
        self.assertEqual(self.persisted_events(), [])

    def test_http_rejects_malformed_payload_and_oversized_body(self):
        for raw in (b"[]", b"{malformed", b'{"events":[]}'):
            self.assertEqual(self.request("/v1/logging/events", raw=raw)[0], 400)
        self.assertEqual(self.request("/v1/logging/events", raw=b"{}", headers={"Content-Length": str(4*1024*1024+1)})[0], 400)
        self.assertEqual(self.persisted_events(), [])

    def test_http_unknown_route_never_writes_events(self):
        self.assertEqual(self.request("/v1/logging/unknown", {"events": [portal_event()]})[0], 404)
        self.assertEqual(self.request("/v1/logging/events", method="GET")[0], 404)
        self.assertEqual(self.persisted_events(), [])


if __name__ == "__main__":
    unittest.main()
