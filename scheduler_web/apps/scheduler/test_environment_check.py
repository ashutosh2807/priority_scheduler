from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from django.test import SimpleTestCase

from check_environment import probe_oracle, EnvironmentCheckError
from project_paths import project_path
from apps.scheduler.control_client import SchedulerApiClient
from apps.scheduler.file_adapter import SchedulerProjectReadAdapter


class EnvironmentCheckTests(SimpleTestCase):
    def test_timestamp_transport_accepts_z_offsets_consistently(self):
        value = "2026-09-12T10:20:30Z"
        self.assertEqual(SchedulerApiClient._normalise_datetime(value), "2026-09-12T10:20:30+00:00")
        self.assertEqual(SchedulerProjectReadAdapter._to_datetime(value).isoformat(), "2026-09-12T10:20:30+00:00")
        self.assertEqual(SchedulerProjectReadAdapter._to_date("2026-03-31").isoformat(), "2026-03-31")

    def test_moved_paths_use_installation_directory_instead_of_working_directory(self):
        with TemporaryDirectory() as folder:
            base = Path(folder) / "moved project" / "scheduler_web"
            self.assertEqual(project_path("../scheduler_project", "unused", base=base), base.parent / "scheduler_project")
            self.assertEqual(project_path("runtime/office/portal.sqlite3", "unused", base=base), base / "runtime/office/portal.sqlite3")
            self.assertEqual(project_path("", base / "db.sqlite3", base=base), base / "db.sqlite3")

    def test_connection_check_only_uses_zero_row_selects_and_closes_cursor(self):
        connection = Mock()
        checks = probe_oracle(connection, {"SCHEDULER_ORACLE_LOGGING": "1"})
        self.assertEqual(len(checks), 4)
        cursor = connection.cursor.return_value
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(statements[0], "SELECT 1 FROM DUAL")
        self.assertTrue(all(sql.startswith("SELECT ") and sql.endswith("WHERE 1=0") for sql in statements[1:]))
        cursor.fetchall.assert_not_called()
        connection.commit.assert_not_called()
        cursor.callproc.assert_not_called()
        cursor.close.assert_called_once()

    def test_invalid_configuration_is_rejected_before_oracle_is_queried(self):
        connection = Mock()
        with self.assertRaises(EnvironmentCheckError):
            probe_oracle(connection, {"SCHEDULER_MASTER_TABLE": "MASTER; DROP TABLE MASTER"})
        connection.cursor.assert_not_called()

    def test_table_errors_are_sanitized(self):
        connection = Mock()
        connection.cursor.return_value.execute.side_effect = [None, RuntimeError("secret-password endpoint")]
        with self.assertRaisesRegex(RuntimeError, "Schedule Master check failed") as error:
            probe_oracle(connection, {})
        self.assertNotIn("secret-password", str(error.exception))
        connection.cursor.return_value.close.assert_called_once()
