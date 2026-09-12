"""Durable logging tests use SQLite fixtures and a transactional Oracle fake."""

import copy
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from repositories.oracle_logging_repository import OracleLoggingRepository, ensure_schema


def event(status="PENDING", **extra):
    return {"event_type": "OCCURRENCE_STATE", "source": "SCHEDULER", "job_id": 7,
            "name": "Cash position", "record_key": "occurrence:7:2026-09-11",
            "report_date": "2026-09-11", "planned_execution_date": "2026-09-12",
            "status": status, "payload": {"status": status, "run_config": {"RUNS_ON": ["DAILY"]}}, **extra}


class OracleFake:
    def __init__(self):
        self.log = {}
        self.current = {}
        self.calls = []
        self.clob_sizes = []
        self.cursors = []
        self.fail_connect = False
        self.fail_current = False
        self.lose_commit_ack = False
        self.connections = []

    def connect(self):
        if self.fail_connect:
            raise RuntimeError("password=do-not-report user/private-password@private-host")
        connection = OracleConnection(self)
        self.connections.append(connection)
        return connection


class OracleConnection:
    def __init__(self, database):
        self.database = database
        self.log = copy.deepcopy(database.log)
        self.current = copy.deepcopy(database.current)
        self.rolled_back = self.closed = False

    def cursor(self):
        cursor = OracleCursor(self)
        self.database.cursors.append(cursor)
        return cursor

    def setinputsizes(self, **kwargs):
        self.database.clob_sizes.append(kwargs)

    def execute(self, sql, values):
        self.database.calls.append((sql, copy.deepcopy(values)))
        if "SCHEDULE_EXTG_LOG" in sql:
            self.log.setdefault(values["event_id"], dict(values))
        else:
            if self.database.fail_current:
                raise RuntimeError("Simulated current-table outage")
            key = values["source_id"], values["record_key"]
            previous = self.current.get(key)
            if previous is None or previous["event_seq"] < values["event_seq"]:
                self.current[key] = dict(values)

    def commit(self):
        self.database.log = self.log
        self.database.current = self.current
        if self.database.lose_commit_ack:
            self.database.lose_commit_ack = False
            raise RuntimeError("Connection lost after commit")

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class OracleCursor:
    def __init__(self, connection):
        self.connection = connection
        self.sizes = {}
        self.closed = False

    def setinputsizes(self, **kwargs):
        self.sizes.update(kwargs)
        self.connection.setinputsizes(**kwargs)

    def execute(self, sql, values):
        if set(self.sizes) - set(re.findall(r":([a-z_]+)", sql)):
            raise AssertionError("Named input bindings leaked across different SQL shapes.")
        values = {name: value.replace(microsecond=0) if isinstance(value, datetime) and self.sizes.get(name) != "TIMESTAMP_TYPE" else value
                  for name, value in values.items()}
        self.connection.execute(sql, values)

    def close(self):
        self.closed = True


class LoggingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "scheduler.db"
        self.connection = sqlite3.connect(self.path)
        self.addCleanup(self.connection.close)
        self.oracle = OracleFake()
        self.logger = OracleLoggingRepository(self.connection, enabled=True, connection_factory=self.oracle.connect)
        self.driver_patch = patch("repositories.oracle_logging_repository.load_oracle_driver",
                                  return_value=SimpleNamespace(DB_TYPE_CLOB="CLOB_TYPE", DB_TYPE_TIMESTAMP="TIMESTAMP_TYPE"))
        self.driver_patch.start()
        self.addCleanup(self.driver_patch.stop)

    def test_utc_z_timestamps_deliver_on_python_310_with_local_oracle_time(self):
        timestamp = "2026-09-12T05:17:19.123456Z"
        expected = datetime(2026, 9, 12, 5, 17, 19, 123456, tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        parsed_values = []

        def python_310_parse(value):
            # Simulate 3.10 even when this regression runs on Python 3.12.
            self.assertFalse(value.endswith("Z"))
            parsed_values.append(value)
            return datetime.fromisoformat(value)

        with patch("datetime_compat.datetime", SimpleNamespace(fromisoformat=python_310_parse)):
            event_id = self.logger.enqueue(event("SUCCESS", occurred_at=timestamp, payload={
                "run_date": timestamp, "executed_at": timestamp, "last_run": timestamp,
            }))
            self.assertEqual(self.logger.flush()["delivered"], 1)
        self.assertIn("2026-09-12T05:17:19.123456+00:00", parsed_values)
        self.assertEqual(self.oracle.log[event_id]["occurred_at"], expected)
        current = next(iter(self.oracle.current.values()))
        for name in ("run_date", "executed_at", "last_run", "occurred_at"):
            self.assertEqual(current[name], expected)
        self.assertEqual(current["report_date"].isoformat(), "2026-09-11")
        self.assertEqual(current["planned_execution_date"].isoformat(), "2026-09-12")
        self.assertEqual(self.logger.stats()["pending"], 0)

    def test_outage_retains_ordered_events_and_replays_with_original_identity(self):
        ids = [self.logger.enqueue(event(status)) for status in ("PENDING", "RUNNING", "SUCCESS")]
        self.oracle.fail_connect = True
        failed = self.logger.flush()
        self.assertEqual((failed["failed"], failed["pending"]), (1, 3))
        self.assertNotIn("do-not-report", self.logger.stats()["last_error"])
        self.assertTrue(self.logger.flush()["deferred"])
        self.oracle.fail_connect = False
        result = self.logger.flush(force=True)
        self.assertEqual(result["delivered"], 3)
        self.assertEqual(list(self.oracle.log), ids)
        current = next(iter(self.oracle.current.values()))
        self.assertEqual(current["status"], "SUCCESS")
        self.assertEqual(current["event_seq"], 3)
        self.assertEqual(self.logger.stats()["pending"], 0)
        self.assertEqual(self.logger.flush()["delivered"], 0)

    def test_lost_commit_ack_replays_log_once_and_never_reverts_newer_current(self):
        first = self.logger.enqueue(event("RUNNING"))
        self.oracle.lose_commit_ack = True
        result = self.logger.flush()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(self.oracle.log), 1)
        self.assertEqual(self.logger.stats()["pending"], 1)
        self.logger.enqueue(event("SUCCESS"))
        self.assertEqual(self.logger.flush(force=True)["delivered"], 2)
        self.assertEqual(len(self.oracle.log), 2)
        persisted_event, sequence = self.connection.execute(
            "SELECT event_json,event_seq FROM scheduler_oracle_log_outbox WHERE event_id=?", (first,)
        ).fetchone()
        self.logger._deliver(json.loads(persisted_event), sequence)
        self.assertEqual(next(iter(self.oracle.current.values()))["status"], "SUCCESS")
        self.assertEqual(len(self.oracle.log), 2)
        self.assertIn("WHERE target.event_seq < :event_seq", self.oracle.calls[-1][0])

    def test_log_and_current_are_one_oracle_transaction(self):
        self.logger.enqueue(event("RUNNING"))
        self.oracle.fail_current = True
        self.assertEqual(self.logger.flush()["failed"], 1)
        self.assertEqual(self.oracle.log, {})
        self.assertEqual(self.oracle.current, {})
        self.assertTrue(self.oracle.connections[-1].rolled_back)
        self.assertEqual(self.logger.stats()["pending"], 1)

    def test_source_id_and_sequence_survive_restart(self):
        first_id = self.logger.enqueue(event())
        other_connection = sqlite3.connect(self.path)
        self.addCleanup(other_connection.close)
        restarted = OracleLoggingRepository(other_connection, enabled=True, connection_factory=self.oracle.connect)
        self.assertEqual(restarted.source_id, self.logger.source_id)
        second_id = restarted.enqueue(event("RUNNING"))
        self.assertNotEqual(first_id, second_id)
        restarted.flush()
        self.assertEqual([row["event_seq"] for row in self.oracle.log.values()], [1, 2])
        self.assertEqual({row["source_id"] for row in self.oracle.log.values()}, {self.logger.source_id})

    def test_unchanged_capture_does_not_flood_log_but_repeated_transition_is_retained(self):
        key = "occurrence:7:2026-09-11"
        first = self.logger.capture_state(key, event())
        refresh = event(occurred_at="2026-09-12T10:02:00")
        refresh["payload"].update(calculated_at="2026-09-12T10:02:00", updated_at="2026-09-12T10:02:00", next_evaluation="2026-09-12T10:03:00")
        self.assertIsNone(self.logger.capture_state(key, refresh))
        self.logger.capture_state(key, event("RUNNING"))
        third = self.logger.capture_state(key, event())
        self.assertNotEqual(first, third)
        self.assertEqual(self.logger.stats()["total"], 3)

    def test_commit_false_is_atomic_with_business_insert_and_does_not_commit_caller(self):
        self.connection.execute("CREATE TABLE business(id INTEGER PRIMARY KEY,status TEXT)")
        self.connection.commit()
        self.connection.execute("INSERT INTO business VALUES(1,'RUNNING')")
        self.logger.capture_state(event()["record_key"], event("RUNNING"), connection=self.connection, commit=False)
        self.assertTrue(self.connection.in_transaction)
        with self.assertRaises(RuntimeError):
            self.logger.flush()
        self.connection.rollback()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM business").fetchone()[0], 0)
        self.assertEqual(self.logger.stats()["total"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM scheduler_oracle_log_state").fetchone()[0], 0)
        self.connection.execute("INSERT INTO business VALUES(2,'SUCCESS')")
        self.logger.enqueue(event("SUCCESS"), connection=self.connection, commit=False)
        self.connection.commit()
        self.assertEqual(self.logger.stats()["total"], 1)

    def test_dedicated_flush_does_not_deliver_uncommitted_event_or_commit_business_state(self):
        dedicated_connection = sqlite3.connect(self.path)
        self.addCleanup(dedicated_connection.close)
        dedicated = OracleLoggingRepository(dedicated_connection, enabled=True, connection_factory=self.oracle.connect)
        self.logger.enqueue(event(), commit=False)
        self.assertEqual(dedicated.flush()["delivered"], 0)
        self.assertTrue(self.connection.in_transaction)
        self.connection.commit()
        self.assertEqual(dedicated.flush()["delivered"], 1)

    def test_explicit_event_and_dedupe_keys_replay_existing_id_but_reject_collision(self):
        first = self.logger.enqueue(event(event_id="stable-event-id"), dedupe_key="execution:8:running")
        self.assertEqual(self.logger.enqueue(event(event_id="stable-event-id")), first)
        self.assertEqual(self.logger.enqueue(event(), dedupe_key="execution:8:running"), first)
        with self.assertRaises(ValueError):
            self.logger.enqueue(event("SUCCESS", event_id="stable-event-id"))
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.logger.stats()["total"], 1)

    def test_failed_sqlite_delivery_bookkeeping_recovers_without_restart(self):
        class FailCommitOnce(sqlite3.Connection):
            fail_commit = False

            def commit(self):
                if self.fail_commit:
                    self.fail_commit = False
                    raise sqlite3.OperationalError("database is locked")
                return super().commit()

        for oracle_available in (True, False):
            with self.subTest(oracle_available=oracle_available):
                connection = sqlite3.connect(":memory:", factory=FailCommitOnce)
                self.addCleanup(connection.close)
                oracle = OracleFake()
                oracle.fail_connect = not oracle_available
                logger = OracleLoggingRepository(connection, enabled=True, connection_factory=oracle.connect)
                logger.enqueue(event())
                connection.fail_commit = True
                with self.assertRaises(sqlite3.OperationalError):
                    logger.flush()
                self.assertFalse(connection.in_transaction)
                self.assertEqual(logger.stats()["pending"], 1)
                oracle.fail_connect = False
                self.assertEqual(logger.flush(force=True)["delivered"], 1)
                self.assertEqual(len(oracle.log), 1)
                self.assertEqual(logger.stats()["pending"], 0)

    def test_large_payloads_bind_as_clobs_and_credentials_are_redacted_before_local_storage(self):
        payload = {"error_info": "ORA-20001 " + "x" * 50000 + " password=private user/private@host",
                   "run_config": {"RUNS_ON": ["DAILY"], "notes": "y" * 40000},
                   "ORACLE_PASSWORD": "never-persist", "nested": {"api_token": "never-persist"},
                   "count": 12345, "executed_at": "2026-09-12T10:00:00"}
        self.logger.enqueue(event("FAILED", payload=payload, actor="operator.one", reason="z" * 5000))
        local = self.connection.execute("SELECT event_json FROM scheduler_oracle_log_outbox").fetchone()[0]
        self.assertNotIn("never-persist", local)
        self.assertNotIn("password=private", local)
        self.assertNotIn("user/private@host", local)
        self.assertEqual(self.logger.flush()["delivered"], 1)
        values = next(iter(self.oracle.current.values()))
        self.assertGreater(len(values["error_info"]), 50000)
        self.assertGreater(len(values["run_config"]), 40000)
        self.assertEqual(values["actor"], "operator.one")
        self.assertEqual(values["records_loaded"], 12345)
        self.assertEqual({name: self.oracle.clob_sizes[-1][name] for name in ("payload", "reason", "error_info", "run_config")},
                         {name: "CLOB_TYPE" for name in ("payload", "reason", "error_info", "run_config")})
        self.assertEqual(len(self.oracle.cursors), 2)
        self.assertIsNot(self.oracle.cursors[0], self.oracle.cursors[1])
        self.assertTrue(all(cursor.closed for cursor in self.oracle.cursors))

    def test_oracle_timestamps_retain_microseconds_in_current_and_history(self):
        happened = "2026-09-12T10:00:00.123456"
        self.logger.enqueue(event("SUCCESS", occurred_at=happened,
                                  payload={"executed_at": happened, "last_run": happened}))
        self.assertEqual(self.logger.flush()["delivered"], 1)
        self.assertEqual(next(iter(self.oracle.log.values()))["occurred_at"].microsecond, 123456)
        current = next(iter(self.oracle.current.values()))
        self.assertEqual(current["executed_at"].microsecond, 123456)
        self.assertEqual(current["last_run"].microsecond, 123456)

    def test_audit_only_event_does_not_create_current_occurrence(self):
        self.logger.enqueue({"event_type": "JOB_PAUSED", "actor": "operator", "payload": {"before": "ACTIVE", "after": "PAUSED"}})
        self.assertEqual(self.logger.flush()["delivered"], 1)
        self.assertEqual(len(self.oracle.log), 1)
        self.assertEqual(self.oracle.current, {})

    def test_incomplete_current_event_is_rejected_before_it_can_block_delivery(self):
        for invalid in (event(name=None), event(status=None), event(name="")):
            with self.subTest(event=invalid), self.assertRaises(ValueError):
                self.logger.enqueue(invalid)
        self.assertEqual(self.logger.stats()["total"], 0)
        self.assertFalse(self.connection.in_transaction)

    def test_disabled_delivery_still_retains_local_events(self):
        self.logger.enabled = False
        self.logger.enqueue(event())
        self.assertEqual(self.logger.flush(), {"enabled": False, "delivered": 0, "failed": 0, "deferred": False, "pending": 1})
        self.assertEqual(self.oracle.calls, [])


if __name__ == "__main__":
    unittest.main()
