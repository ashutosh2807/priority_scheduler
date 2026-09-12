import json
import unittest
from unittest.mock import patch

from operator_cli import (
    ControlApiError,
    SchedulerControlClient,
    _configuration_changes_from_args,
    build_parser,
    render_jobs,
    render_status,
)


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class OperatorCliTests(unittest.TestCase):
    @patch("operator_cli.urlopen")
    def test_client_uses_the_versioned_control_route_and_token(self, mocked_urlopen):
        mocked_urlopen.return_value = _Response({"control": {"control_status": "PAUSED"}})
        client = SchedulerControlClient(
            "http://127.0.0.1:8091/",
            token="secret",
            timeout=2,
            actor="test-operator",
        )

        response = client.control(42, "pause", reason="Treasury maintenance")

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8091/v1/jobs/42/controls/pause")
        self.assertEqual(request.get_header("X-scheduler-token"), "secret")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"actor": "test-operator", "reason": "Treasury maintenance"},
        )
        self.assertEqual(response["control"]["control_status"], "PAUSED")

    def test_status_and_job_rendering_are_operator_readable(self):
        snapshot = {
            "meta": {
                "generated_at": "2026-09-11T10:00:00",
                "service_started_at": "2026-09-11T09:00:00",
                "next_cycle_at": "2026-09-11T10:03:00",
            },
            "schedule_master": [
                {
                    "id": 7,
                    "name": "CASH_POSITION",
                    "is_active": 1,
                    "run_config": {"RUNS_ON": ["DAILY", "MONTHLY"]},
                    "margin": "T-1",
                }
            ],
            "controls": [{"job_id": 7, "control_status": "PAUSED"}],
            "staging": [{"state": "WAITING_TIME"}],
            "ready": [],
            "priority_queue": [],
            "executions": [{"status": "SUCCESS"}],
            "upcoming": [{"job_name": "CASH_POSITION", "execution_date": "2026-09-12"}],
        }

        status = render_status(snapshot, "http://127.0.0.1:8091")
        jobs = render_jobs(snapshot)

        self.assertIn("Schedule Master jobs: 1", status)
        self.assertIn("Controls: PAUSED: 1", status)
        self.assertIn("Next upcoming: CASH_POSITION (2026-09-12)", status)
        self.assertIn("CASH_POSITION", jobs)
        self.assertIn("DAILY, MONTHLY", jobs)
        self.assertIn("PAUSED", jobs)

    @patch("operator_cli.urlopen")
    def test_client_records_service_stop_with_actor_and_reason(self, mocked_urlopen):
        mocked_urlopen.return_value = _Response({"service_control": {"scheduler_enabled": 0}})
        client = SchedulerControlClient("http://127.0.0.1:8091", actor="ops-42")

        response = client.service_control("stop", "Planned maintenance")

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8091/v1/scheduler/controls/stop")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {"actor": "ops-42", "reason": "Planned maintenance"},
        )
        self.assertFalse(response["service_control"]["scheduler_enabled"])

    @patch("operator_cli.urlopen")
    def test_client_uses_bounded_patch_route_for_master_configuration(self, mocked_urlopen):
        mocked_urlopen.return_value = _Response(
            {"configuration": {"after": {"is_active": False}}}
        )
        client = SchedulerControlClient("http://127.0.0.1:8091", actor="ops-42")

        response = client.configure(
            42,
            is_active=False,
            run_by={"from_time": "22:00", "to_time": "06:00"},
            reason="Approved overnight window",
        )

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "PATCH")
        self.assertEqual(request.full_url, "http://127.0.0.1:8091/v1/jobs/42/configuration")
        self.assertEqual(
            json.loads(request.data.decode("utf-8")),
            {
                "actor": "ops-42",
                "is_active": False,
                "run_by": {"from_time": "22:00", "to_time": "06:00"},
                "reason": "Approved overnight window",
            },
        )
        self.assertFalse(response["configuration"]["after"]["is_active"])

    def test_configure_parser_requires_complete_and_non_equal_time_window(self):
        parser = build_parser()
        args = parser.parse_args(
            ["configure", "42", "--inactive", "--from", "22:00", "--to", "06:00"]
        )
        changes = _configuration_changes_from_args(args)
        self.assertFalse(changes["is_active"])
        self.assertEqual(changes["run_by"]["to_time"], "06:00")

        missing_to = parser.parse_args(["configure", "42", "--from", "22:00"])
        with self.assertRaises(ControlApiError):
            _configuration_changes_from_args(missing_to)

        equal_window = parser.parse_args(
            ["configure", "42", "--from", "09:00", "--to", "09:00"]
        )
        with self.assertRaises(ControlApiError):
            _configuration_changes_from_args(equal_window)


if __name__ == "__main__":
    unittest.main()
