"""Definition CRUD persists to the configured source without running jobs."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from control_api import SchedulerControlApi, StaleOperationError, start_control_api
from repositories.master_configuration_repository import MasterConfigurationError, MasterConfigurationRepository
from repositories.schedule_definition_repository import ScheduleDefinitionRepository
from repositories.schedule_master_repository import ScheduleMasterRepository


def definition(job_id=7, **changes):
    return {"id": job_id, "name": "Cash position", "package_name": "REPORTING.CASH_POSITION",
            "run_config": {"RUNS_ON": ["DAILY"], "MAX_ATTEMPTS": 8}, **changes}


class FakeCursor:
    rowcount = 1

    def __init__(self, row=None, duplicate=None, fail=False):
        self.results = iter([row, duplicate])
        self.executed = []
        self.fail = fail

    def execute(self, sql, values=None):
        self.executed.append((sql, values))
        if self.fail and sql.startswith(("UPDATE ", "INSERT ", "DELETE ")):
            raise RuntimeError("private connection failure")

    def fetchone(self):
        return next(self.results)

    def close(self):
        pass


class FakeConnection:
    def __init__(self, **kwargs):
        self.cursor_value = FakeCursor(**kwargs)
        self.committed = self.rolled_back = self.closed = False

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class DefinitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "Schedule_Master.json"
        self.path.write_text("[]", encoding="utf-8")
        self.source = ScheduleMasterRepository(self.path, source="file")
        self.repository = ScheduleDefinitionRepository(self.source)

    def test_file_create_edit_delete_retains_other_schedules_and_configuration(self):
        first = self.repository.create(definition(confirmation_needed=True))
        self.repository.create(definition(8, name="Other report"))
        self.assertEqual(first["after"]["margin"], "T+1")
        self.assertEqual(self.source.get_by_id(7).confirmation_needed, 1)
        result = self.repository.update_definition(7, {
            "name": "Quarter report", "run_config": {"RUNS_ON": ["QUARTERLY"],
            "RUN_BY": {"FROM_TIME": "10:00", "TO_TIME": "11:00"}},
        })
        self.assertEqual(result["after"]["run_config"]["MAX_ATTEMPTS"], 8)
        self.assertEqual(self.source.get_by_id(7).time_flag, 1)
        self.repository.update_definition(7, {"run_config": {"RUN_BY": None}})
        self.assertEqual(self.source.get_by_id(7).time_flag, 0)
        self.assertNotIn("RUN_BY", self.source.get_by_id(7).run_config)
        deleted = self.repository.delete_definition(7)
        self.assertIsNone(deleted["after"])
        self.assertEqual([job.id for job in self.source.get_all()], [8])

    def test_invalid_definitions_do_not_change_the_master(self):
        invalid = [
            definition(id=0), definition(id=2147483648), definition(id=True),
            definition(package_name="BEGIN DESTROY; END;"), definition(name=""),
            definition(run_config={"RUNS_ON": ["NEVER"]}),
            definition(run_config={"RUNS_ON": ["SPECIFIC_DATE"]}),
            definition(run_config={"RUNS_ON": ["DAILY"], "MAX_ATTEMPTS": 101}),
            definition(run_config={"RUNS_ON": ["DAILY"], "RUN_BY": {"FROM_TIME": "10:00", "TO_TIME": "10:00"}}),
            definition(run_config={"RUNS_ON": ["DAILY"], "runs_on": ["ANNUALLY"]}),
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(MasterConfigurationError):
                self.repository.create(payload)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[]")

    def test_duplicate_id_and_name_rejected_and_id_immutable(self):
        self.repository.create(definition())
        original = self.path.read_bytes()
        for payload in (definition(), definition(8, name="CASH POSITION")):
            with self.assertRaises(MasterConfigurationError):
                self.repository.create(payload)
        with self.assertRaises(MasterConfigurationError):
            self.repository.update_definition(7, {"id": 8})
        self.assertEqual(self.path.read_bytes(), original)

    def oracle_repository(self, connection, fail_refresh=False, optional=()):
        source = SimpleNamespace(source="oracle", file_path=self.path,
            oracle_repository=SimpleNamespace(table_name="BANK.SCHEDULE_EXTRACT_MASTER", optional_columns=optional),
            refresh_now=Mock(side_effect=RuntimeError("refresh failed") if fail_refresh else None),
            _cached_records=[definition()])
        return ScheduleDefinitionRepository(source, connection_factory=lambda: connection), source

    def test_oracle_create_uses_binds_and_stores_confirmation_without_optional_column(self):
        connection = FakeConnection()
        repository, source = self.oracle_repository(connection)
        result = repository.create(definition(name="Manager's report", confirmation_needed=True))
        sql, values = connection.cursor_value.executed[-1]
        self.assertTrue(sql.startswith("INSERT INTO BANK.SCHEDULE_EXTRACT_MASTER"))
        self.assertNotIn("Manager's", sql)
        self.assertEqual(values["name"], "Manager's report")
        self.assertEqual(json.loads(values["run_config"])["CONFIRMATION_NEEDED"], 1)
        self.assertNotIn("CONFIRMATION_NEEDED", sql)
        self.assertTrue(connection.committed)
        self.assertTrue(result["snapshot_refreshed"])
        source.refresh_now.assert_called_once()

    def test_oracle_edit_preserves_existing_config_and_updates_optional_columns(self):
        row = (7, "Cash position", "REPORTING.CASH_POSITION", json.dumps({"RUNS_ON": ["DAILY"], "MAX_ATTEMPTS": 8, "CUSTOM_FIELD": "retain"}), "T+1", 0, 1, "2026-01-01", 0, 0)
        connection = FakeConnection(row=row)
        repository, _ = self.oracle_repository(connection, optional=("TIME_FLAG", "CONFIRMATION_NEEDED"))
        result = repository.update_definition(7, {"confirmation_needed": True, "run_config": {"RUNS_ON": ["MONTHLY"]}})
        sql, values = connection.cursor_value.executed[-1]
        self.assertIn("CONFIRMATION_NEEDED = :confirmation_needed", sql)
        self.assertNotIn("CREATED_DATE =", sql)
        self.assertEqual(values["confirmation_needed"], 1)
        self.assertEqual(result["after"]["run_config"]["CUSTOM_FIELD"], "retain")
        self.assertEqual(result["after"]["run_config"]["MAX_ATTEMPTS"], 8)

    def test_oracle_delete_only_targets_master_and_preserves_commit_on_refresh_failure(self):
        row = (7, "Cash position", "REPORTING.CASH_POSITION", '{"RUNS_ON":["DAILY"]}', "T+1", 0, 1, "2026-01-01")
        connection = FakeConnection(row=row)
        repository, source = self.oracle_repository(connection, fail_refresh=True)
        result = repository.delete_definition(7)
        sql, values = connection.cursor_value.executed[-1]
        self.assertEqual(sql, "DELETE FROM BANK.SCHEDULE_EXTRACT_MASTER WHERE ID = :job_id")
        self.assertEqual(values, {"job_id": 7})
        self.assertTrue(connection.committed)
        self.assertFalse(result["snapshot_refreshed"])
        self.assertEqual(source._cached_records, [])

    def test_existing_sbi_confirmation_column_roundtrips(self):
        row = (7, "Cash position", "REPORTING.CASH_POSITION", '{"RUNS_ON":["DAILY"]}', "T+1", 0, 1, "2026-01-01", 0, 1)
        connection = FakeConnection(row=row)
        repository, _ = self.oracle_repository(connection, optional=("TIME_FLAG", "CONFIRMATION"))
        result = repository.update_definition(7, {"name": "Changed cash position"})
        self.assertEqual(result["after"]["confirmation_needed"], 1)
        sql, values = connection.cursor_value.executed[-1]
        self.assertIn("CONFIRMATION = :confirmation", sql)
        self.assertEqual(values["confirmation"], 1)

    def test_failed_oracle_mutation_rolls_back_without_changing_snapshot(self):
        connection = FakeConnection(fail=True)
        repository, source = self.oracle_repository(connection)
        with self.assertRaises(MasterConfigurationError) as raised:
            repository.create(definition())
        self.assertNotIn("private connection", str(raised.exception))
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        source.refresh_now.assert_not_called()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[]")


class DefinitionApiTests(DefinitionTests):
    def setUp(self):
        super().setUp()
        self.audit = Mock()
        self.executions = SimpleNamespace(get_all=lambda: [])
        self.ready = Mock()
        self.staging = Mock()
        self.queue = Mock()
        self.api = SchedulerControlApi({"schedule_master_repository": self.source,
            "master_configuration_repository": MasterConfigurationRepository(self.source),
            "operations_repository": self.audit, "execution_repository": self.executions,
            "ready_repository": self.ready, "staging_repository": self.staging, "priority_queue": self.queue})

    def test_api_records_actor_and_retains_execution_history_on_delete(self):
        self.api.save_definition(None, definition(), actor="operator", reason="New report")
        self.executions.get_all = lambda: [SimpleNamespace(job_id=7, status="SUCCESS")]
        self.api.delete_definition(7, actor="operator", reason="Retired")
        self.assertEqual(self.executions.get_all()[0].status, "SUCCESS")
        self.ready.delete.assert_called_once_with(7)
        self.staging.delete.assert_called_once_with(7)
        self.queue.remove.assert_called_once_with(job_id=7)
        audit = self.audit.record_audit.call_args.kwargs
        self.assertEqual(audit["action"], "SCHEDULE_DELETED")
        self.assertEqual(audit["actor"], "operator")
        self.assertEqual(audit["reason"], "Retired")
        self.assertEqual(audit["before_state"]["name"], "Cash position")

    def test_running_schedule_cannot_be_changed_or_deleted(self):
        self.repository.create(definition())
        self.executions.get_all = lambda: [SimpleNamespace(job_id=7, status="RUNNING")]
        for action in (lambda: self.api.save_definition(7, {"name": "Other"}), lambda: self.api.delete_definition(7)):
            with self.assertRaises(StaleOperationError):
                action()
        self.assertEqual(self.source.get_by_id(7).name, "Cash position")
        self.audit.record_audit.assert_not_called()

    def test_historical_schedule_id_cannot_be_reused(self):
        self.executions.get_all = lambda: [SimpleNamespace(job_id=7, status="SUCCESS")]
        with self.assertRaises(MasterConfigurationError):
            self.api.save_definition(None, definition())
        self.assertEqual(self.source.get_all(), [])

    def test_http_create_edit_delete_and_authorization(self):
        with patch.dict(os.environ, {"SCHEDULER_API_TOKEN": "local-test-token"}):
            server = start_control_api({}, self.api, host="127.0.0.1", port=0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(method, path, payload, token="local-test-token"):
            headers = {"Content-Type": "application/json", "X-Scheduler-Token": token}
            with urlopen(Request(f"http://127.0.0.1:{server.server_port}" + path,
                data=json.dumps(payload).encode(), method=method, headers=headers), timeout=2) as response:
                return response.status, json.loads(response.read())

        with self.assertRaises(HTTPError) as denied:
            request("POST", "/v1/jobs", definition(), token="wrong")
        self.assertEqual(denied.exception.code, 401)
        status, result = request("POST", "/v1/jobs", definition())
        self.assertEqual(status, 201)
        self.assertEqual(result["schedule"]["after"]["id"], 7)
        _, result = request("PATCH", "/v1/jobs/7/definition", {"name": "Changed report"})
        self.assertEqual(result["schedule"]["after"]["name"], "Changed report")
        self.executions.get_all = lambda: [SimpleNamespace(job_id=7, status="RUNNING")]
        with self.assertRaises(HTTPError) as conflict:
            request("DELETE", "/v1/jobs/7", {})
        self.assertEqual(conflict.exception.code, 409)
        self.executions.get_all = lambda: []
        _, result = request("DELETE", "/v1/jobs/7", {"reason": "Retired"})
        self.assertIsNone(result["schedule"]["after"])

    def test_edit_preserves_durable_occurrence_dates_for_midnight_cutoff(self):
        self.repository.create(definition())
        self.api.save_definition(7, {"run_config": {"MAX_ATTEMPTS": 12}})
        self.ready.delete.assert_not_called()
        self.staging.delete.assert_not_called()
        self.queue.remove.assert_called_once_with(job_id=7)

    def test_calendar_refresh_action_is_audited(self):
        result = {"refreshed": True, "available": True, "error": None}
        self.api.application["refresh_calendar"] = Mock(return_value=result)
        self.assertEqual(self.api.refresh_calendar("operator", "New DATEMAST rows"), result)
        self.assertEqual(self.audit.record_audit.call_args.kwargs["action"], "CALENDAR_REFRESHED")


if __name__ == "__main__":
    unittest.main()
