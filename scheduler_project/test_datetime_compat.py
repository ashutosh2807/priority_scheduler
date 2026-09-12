"""Timestamp ingress compatibility; no Oracle calls or persistent writes."""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from datetime_compat import parse_iso_datetime
from execution.oracle_executor import OracleExecutor
from execution.execution_repository import ExecutionRepository as LegacyExecutionRepository
from import_schedule_master import canonical_record
from repositories.job_control_repository import JobControlRepository
from repositories.execution_repository import ExecutionRepository
from repositories.oracle_schedule_extg_mirror import OracleScheduleExtgMirror
from repositories.oracle_schedule_master_repository import OracleScheduleMasterRepository
from scheduler.eligibility import EligibilityEvaluator
from scheduler.occurrence_scheduler import _to_datetime
from scheduler.priority import PriorityCalculator
from scheduler.retry_policy import RetryPolicy
from scheduler.scheduler import Scheduler
from scheduler.staging import StagingManager
from scheduler.time_window import TimeWindowEvaluator


class TimestampCompatibilityTests(unittest.TestCase):
    timestamp = "2026-03-31T23:45:12.123456Z"

    def strict_python_310(self, value):
        self.assertFalse(value.endswith("Z"))
        return datetime.fromisoformat(value)

    def test_utc_suffix_is_normalized_before_python_310_parses(self):
        with patch("datetime_compat.datetime", SimpleNamespace(fromisoformat=self.strict_python_310)):
            value = parse_iso_datetime(self.timestamp)
        self.assertEqual(value.utcoffset(), timedelta(0))
        self.assertEqual(value.microsecond, 123456)

    def test_naive_timestamps_offsets_and_date_only_input_keep_existing_meaning(self):
        for value in ("2026-03-31", "2026-03-31T10:30:00", "2026-03-31T10:30:00+05:30"):
            with self.subTest(value=value):
                self.assertEqual(parse_iso_datetime(value), datetime.fromisoformat(value))
        with self.assertRaises(ValueError):
            parse_iso_datetime("2026-03-31Z")
        with self.assertRaises(ValueError):
            parse_iso_datetime("not-a-timestampZ")

    def test_current_datetime_inputs_preserve_utc_offset(self):
        expected = datetime(2026, 3, 31, 23, 45, 12, 123456, tzinfo=timezone.utc)
        with patch("datetime_compat.datetime", SimpleNamespace(fromisoformat=self.strict_python_310)):
            for parser in (EligibilityEvaluator._to_datetime, _to_datetime,
                           PriorityCalculator._to_datetime, TimeWindowEvaluator._to_datetime,
                           OracleScheduleExtgMirror._as_datetime, RetryPolicy._as_datetime,
                           Scheduler._to_datetime, StagingManager._to_datetime,
                           ExecutionRepository._parse_datetime, LegacyExecutionRepository._parse_datetime):
                with self.subTest(parser=parser.__qualname__):
                    self.assertEqual(parser(self.timestamp), expected)
            self.assertEqual(JobControlRepository._normalize_override_datetime(self.timestamp),
                             "2026-03-31T23:45:12+00:00")

    def test_import_created_timestamp_and_executor_timestamp_input_accept_z(self):
        validator = OracleScheduleMasterRepository(Path("unused-source.json"))
        row = {"id": 1, "name": "TEST_REPORT", "package_name": "TEST.RUN",
               "run_config": {"RUNS_ON": ["DAILY"]}, "created_date": self.timestamp}
        with patch("datetime_compat.datetime", SimpleNamespace(fromisoformat=self.strict_python_310)):
            self.assertEqual(canonical_record(row, validator)["created_date"], self.timestamp)
            self.assertEqual(OracleExecutor._to_date(self.timestamp).isoformat(), "2026-03-31")
