"""Focused tests for the bounded Scheduler Master configuration surface."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from control_api import SchedulerControlApi, start_control_api
from repositories.master_configuration_repository import (
    MasterConfigurationError,
    MasterConfigurationRepository,
)


class FileMasterSource:
    source = "file"

    def __init__(self, path):
        self.file_path = Path(path)

    def get_all(self):
        return [SimpleNamespace(id=7, name="CASH_POSITION")]


class AuditRecorder:
    def __init__(self):
        self.calls = []

    def record_audit(self, **kwargs):
        self.calls.append(kwargs)


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, parameters=None):
        self.executed.append((sql, parameters))

    def fetchone(self):
        return (7, 1, '{"RUNS_ON":["DAILY"],"RUN_BY":{"FROM_TIME":"09:00","TO_TIME":"10:00"}}')

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.cursor_value = FakeCursor()
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.committed = True

    def rollback(self):
        raise AssertionError("A valid update must not roll back.")

    def close(self):
        self.closed = True


class OracleMasterSource:
    source = "oracle"
    file_path = Path("Schedule_Master.json")
    oracle_repository = SimpleNamespace(table_name="SCHEDULE_EXTRACT_MASTER")

    def __init__(self):
        self.refresh_count = 0

    def refresh_now(self):
        self.refresh_count += 1

    def get_all(self):
        return [SimpleNamespace(id=7, name="CASH_POSITION")]


class MasterConfigurationRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "Schedule_Master.json"
        self.path.write_text(
            json.dumps(
                [
                    {
                        "id": 7,
                        "name": "CASH_POSITION",
                        "package_name": "REPORTING.CASH_POSITION",
                        "is_active": 1,
                        "run_config": {
                            "RUNS_ON": ["DAILY", "MONTHLY"],
                            "RUN_BY": {"FROM_TIME": "09:00", "TO_TIME": "10:00"},
                            "HOLIDAY_RUN": ["SAT"],
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.source = FileMasterSource(self.path)
        self.repository = MasterConfigurationRepository(self.source)

    def test_file_update_changes_only_active_and_run_by_and_preserves_other_fields(self):
        result = self.repository.update(
            7,
            is_active=False,
            run_by={"from_time": "22:00", "to_time": "06:00"},
        )

        saved = json.loads(self.path.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["package_name"], "REPORTING.CASH_POSITION")
        self.assertEqual(saved["run_config"]["RUNS_ON"], ["DAILY", "MONTHLY"])
        self.assertEqual(saved["run_config"]["HOLIDAY_RUN"], ["SAT"])
        self.assertEqual(
            saved["run_config"]["RUN_BY"],
            {"FROM_TIME": "22:00", "TO_TIME": "06:00"},
        )
        self.assertEqual(saved["is_active"], 0)
        self.assertFalse(result["after"]["is_active"])
        self.assertTrue(result["snapshot_refreshed"])

    def test_run_by_null_removes_only_the_time_window(self):
        self.repository.update(7, run_by=None)
        saved = json.loads(self.path.read_text(encoding="utf-8"))[0]
        self.assertNotIn("RUN_BY", saved["run_config"])
        self.assertEqual(saved["run_config"]["RUNS_ON"], ["DAILY", "MONTHLY"])

    def test_task_attempt_limit_changes_independently_and_survives_other_updates(self):
        before = json.loads(self.path.read_text(encoding="utf-8"))[0]
        result = self.repository.update(7, max_attempts=8)
        self.assertEqual(result["before"]["max_attempts"], 3)
        self.assertEqual(result["after"]["max_attempts"], 8)
        self.repository.update(7, is_active=False)
        saved = json.loads(self.path.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["run_config"], {**before["run_config"], "MAX_ATTEMPTS": 8})
        self.assertEqual(saved["package_name"], before["package_name"])

    def test_text_config_attempt_limit_preserves_other_fields_and_removes_case_variants(self):
        records = json.loads(self.path.read_text(encoding="utf-8"))
        config = {**records[0]["run_config"], "max_attempts": 5}
        records[0]["run_config"] = json.dumps(config)
        self.path.write_text(json.dumps(records), encoding="utf-8")
        self.repository.update(7, max_attempts=10)
        saved = json.loads(self.path.read_text(encoding="utf-8"))[0]["run_config"]
        self.assertIsInstance(saved, str)
        updated = json.loads(saved)
        self.assertEqual(updated["MAX_ATTEMPTS"], 10)
        self.assertNotIn("max_attempts", updated)
        self.assertEqual(updated["RUN_BY"], config["RUN_BY"])

    def test_invalid_attempt_limits_do_not_change_master(self):
        original = self.path.read_bytes()
        for value in (None, True, False, 0, -1, 101, 3.5, "6", [], {}):
            with self.subTest(value=value), self.assertRaises(MasterConfigurationError):
                self.repository.update(7, max_attempts=value)
        self.assertEqual(self.path.read_bytes(), original)

    def test_oracle_attempt_only_update_is_bound_and_preserves_run_window(self):
        connection = FakeConnection()
        source = OracleMasterSource()
        result = MasterConfigurationRepository(
            source, connection_factory=lambda: connection,
        ).update(7, max_attempts=7)
        sql, values = connection.cursor_value.executed[1]
        self.assertIn("RUN_CONFIG = :run_config", sql)
        self.assertNotIn("IS_ACTIVE =", sql)
        config = json.loads(values["run_config"])
        self.assertEqual(config["MAX_ATTEMPTS"], 7)
        self.assertEqual(config["RUNS_ON"], ["DAILY"])
        self.assertEqual(config["RUN_BY"], {"FROM_TIME": "09:00", "TO_TIME": "10:00"})
        self.assertEqual(result["after"]["max_attempts"], 7)
        self.assertTrue(connection.committed)
        self.assertEqual(source.refresh_count, 1)

    def test_rejects_invalid_time_or_unbounded_field(self):
        with self.assertRaises(MasterConfigurationError):
            self.repository.update(7, run_by={"from_time": "09:00", "to_time": "09:00"})
        with self.assertRaises(MasterConfigurationError):
            self.repository.update(7, run_by={"from_time": "9am", "to_time": "10:00"})

    def test_oracle_uses_bound_values_and_refreshes_snapshot_after_commit(self):
        connection = FakeConnection()
        source = OracleMasterSource()
        repository = MasterConfigurationRepository(
            source,
            connection_factory=lambda: connection,
        )

        result = repository.update(7, is_active=False, run_by={"from_time": "17:30", "to_time": "19:00"})

        select_sql, select_values = connection.cursor_value.executed[0]
        update_sql, update_values = connection.cursor_value.executed[1]
        self.assertIn("WHERE ID = :job_id FOR UPDATE", select_sql)
        self.assertEqual(select_values, {"job_id": 7})
        self.assertIn("IS_ACTIVE = :is_active", update_sql)
        self.assertIn("RUN_CONFIG = :run_config", update_sql)
        self.assertEqual(update_values["is_active"], 0)
        self.assertEqual(update_values["job_id"], 7)
        self.assertEqual(json.loads(update_values["run_config"])["RUN_BY"]["FROM_TIME"], "17:30")
        self.assertTrue(connection.committed)
        self.assertTrue(connection.closed)
        self.assertEqual(source.refresh_count, 1)
        self.assertTrue(result["snapshot_refreshed"])

    def test_oracle_commit_is_reported_even_when_follow_up_snapshot_refresh_fails(self):
        connection = FakeConnection()

        class FailingRefreshSource(OracleMasterSource):
            def refresh_now(self):
                raise RuntimeError("temporary snapshot problem")

        result = MasterConfigurationRepository(
            FailingRefreshSource(),
            connection_factory=lambda: connection,
        ).update(7, is_active=False)

        self.assertTrue(connection.committed)
        self.assertFalse(result["snapshot_refreshed"])
        self.assertIn("saved in Oracle", result["warning"])


class MasterConfigurationApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        path = Path(self.temporary.name) / "Schedule_Master.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "id": 7,
                        "name": "CASH_POSITION",
                        "is_active": 1,
                        "run_config": {"RUNS_ON": ["DAILY"]},
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.source = FileMasterSource(path)
        self.audit = AuditRecorder()
        self.api = SchedulerControlApi(
            {
                "master_configuration_repository": MasterConfigurationRepository(self.source),
                "operations_repository": self.audit,
                "schedule_master_repository": self.source,
            }
        )

    def test_api_records_actor_reason_and_before_after_state(self):
        result = self.api.configure_master(
            7,
            {"is_active": False, "run_by": {"from_time": "07:00", "to_time": "08:00"}},
            actor="operations.user",
            reason="Approved maintenance window",
        )

        self.assertEqual(result["after"]["run_by"]["from_time"], "07:00")
        self.assertEqual(self.audit.calls[0]["action"], "MASTER_CONFIGURATION_UPDATED")
        self.assertEqual(self.audit.calls[0]["actor"], "operations.user")
        self.assertEqual(self.audit.calls[0]["reason"], "Approved maintenance window")
        self.assertFalse(self.audit.calls[0]["after_state"]["is_active"])

    def test_http_patch_uses_only_the_bounded_configuration_route(self):
        server = start_control_api({}, self.api, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = Request(
            f"http://127.0.0.1:{server.server_port}/v1/jobs/7/configuration",
            data=json.dumps({"run_by": {"from_time": "06:00", "to_time": "07:00"}}).encode("utf-8"),
            method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=2) as response:  # nosec B310 - loopback test server
            body = json.loads(response.read())
        self.assertEqual(body["configuration"]["after"]["run_by"]["to_time"], "07:00")

        with self.assertRaises(MasterConfigurationError):
            self.api.configure_master(7, {"package_name": "NOT_ALLOWED"})

    def test_attempt_only_api_update_is_persisted_and_audited(self):
        result = self.api.configure_master(
            7, {"max_attempts": 6}, actor="operations.user", reason="Approved task retry limit",
        )
        self.assertEqual(result["after"]["max_attempts"], 6)
        self.assertEqual(self.audit.calls[0]["before_state"]["max_attempts"], 3)
        self.assertEqual(self.audit.calls[0]["after_state"]["max_attempts"], 6)
        self.assertEqual(self.audit.calls[0]["reason"], "Approved task retry limit")


if __name__ == "__main__":
    unittest.main()
