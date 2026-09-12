"""Driver selection tests use DB-API doubles only; never contact Oracle."""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from database import oracle_driver
from execution.oracle_executor import OracleExecutor
from repositories.oracle_schedule_master_repository import (
    OracleMasterSyncError, OracleScheduleMasterRepository, connect_oracle_from_environment,
)


def fake_driver(name):
    driver = ModuleType(name)
    driver.init_oracle_client = Mock()
    driver.connect = Mock(return_value=Mock())
    driver.makedsn = Mock(return_value="test-descriptor")
    driver.NUMBER = object()
    return driver


class OracleDriverTests(unittest.TestCase):
    def setUp(self):
        temporary_root = tempfile.gettempdir()
        self.environment = patch.dict(os.environ, {"TEMP": temporary_root, "TMP": temporary_root}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.initialized = patch.object(oracle_driver, "_INITIALIZED_CLIENTS", {})
        self.initialized.start()
        self.addCleanup(self.initialized.stop)
        self.modern = fake_driver("oracledb")
        self.legacy = fake_driver("cx_Oracle")

    def imports(self, mapping=None):
        mapping = mapping or {"oracledb": self.modern, "cx_Oracle": self.legacy}
        def importer(name):
            value = mapping.get(name)
            if isinstance(value, BaseException):
                raise value
            if value is None:
                raise ModuleNotFoundError(name=name)
            return value
        return patch.object(oracle_driver.importlib, "import_module", side_effect=importer)

    def test_explicit_legacy_wins_even_when_both_drivers_are_installed(self):
        os.environ["ORACLE_DRIVER"] = "cx_Oracle"
        with self.imports() as importer:
            self.assertIs(oracle_driver.load_oracle_driver(), self.legacy)
        importer.assert_called_once_with("cx_Oracle")
        self.modern.init_oracle_client.assert_not_called()

    def test_modern_default_is_thin_and_selection_is_lazy(self):
        with self.imports() as importer:
            os.environ["ORACLE_DRIVER"] = "oracledb"
            self.assertIs(oracle_driver.load_oracle_driver(), self.modern)
            os.environ["ORACLE_DRIVER"] = "cx_oracle"
            self.assertIs(oracle_driver.load_oracle_driver(), self.legacy)
        self.assertEqual([call.args[0] for call in importer.call_args_list], ["oracledb", "cx_Oracle"])
        self.modern.init_oracle_client.assert_not_called()
        self.legacy.init_oracle_client.assert_not_called()

    def test_explicit_missing_driver_does_not_try_another(self):
        os.environ["ORACLE_DRIVER"] = "cx_oracle"
        with self.imports({"oracledb": self.modern}) as importer:
            with self.assertRaisesRegex(oracle_driver.OracleDriverError, "not installed"):
                oracle_driver.load_oracle_driver()
        importer.assert_called_once_with("cx_Oracle")

    def test_auto_falls_back_only_for_absent_top_level_module(self):
        with self.imports({"cx_Oracle": self.legacy}) as importer:
            self.assertIs(oracle_driver.load_oracle_driver(), self.legacy)
        self.assertEqual([call.args[0] for call in importer.call_args_list], ["oracledb", "cx_Oracle"])
        for error in (ModuleNotFoundError(name="cryptography"), ImportError("broken binary")):
            with self.subTest(error=type(error).__name__), self.imports({"oracledb": error, "cx_Oracle": self.legacy}) as importer:
                with self.assertRaises(oracle_driver.OracleDriverError):
                    oracle_driver.load_oracle_driver()
                importer.assert_called_once_with("oracledb")

    def test_invalid_driver_and_missing_modules_have_safe_configuration_errors(self):
        os.environ["ORACLE_DRIVER"] = "unsupported-password-value"
        with self.imports() as importer:
            with self.assertRaises(oracle_driver.OracleDriverError) as raised:
                oracle_driver.load_oracle_driver()
            self.assertNotIn("unsupported-password-value", str(raised.exception))
            importer.assert_not_called()
        os.environ["ORACLE_DRIVER"] = "auto"
        with self.imports({"oracledb": ModuleNotFoundError(name="oracledb"), "cx_Oracle": ModuleNotFoundError(name="cx_Oracle")}):
            with self.assertRaisesRegex(oracle_driver.OracleDriverError, "No Oracle driver"):
                oracle_driver.load_oracle_driver()

    def test_explicit_client_is_initialized_once_across_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["ORACLE_CLIENT_LIB_DIR"] = directory
            with self.imports(), ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: oracle_driver.load_oracle_driver(), range(18)))
            self.assertTrue(all(result is self.modern for result in results))
            self.modern.init_oracle_client.assert_called_once_with(lib_dir=str(Path(directory).resolve()))

    def test_relative_client_path_follows_engine_root_not_current_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "client").mkdir()
            os.environ.update(ORACLE_DRIVER="cx_oracle", ORACLE_CLIENT_LIB_DIR="client")
            with patch.object(oracle_driver, "ENGINE_ROOT", root), self.imports():
                oracle_driver.load_oracle_driver()
            self.legacy.init_oracle_client.assert_called_once_with(lib_dir=str((root / "client").resolve()))

    def test_client_initialization_failure_does_not_fallback_or_leak_error_text(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["ORACLE_CLIENT_LIB_DIR"] = directory
            self.modern.init_oracle_client.side_effect = RuntimeError("password=must-stay-private")
            with self.imports() as importer:
                with self.assertRaises(oracle_driver.OracleDriverError) as raised:
                    oracle_driver.load_oracle_driver()
            self.assertNotIn("must-stay-private", str(raised.exception))
            importer.assert_called_once_with("oracledb")
            self.assertFalse(oracle_driver._INITIALIZED_CLIENTS)

    def test_changed_or_missing_client_path_fails_before_connection(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            os.environ["ORACLE_CLIENT_LIB_DIR"] = first
            with self.imports():
                oracle_driver.load_oracle_driver()
                os.environ["ORACLE_CLIENT_LIB_DIR"] = second
                with self.assertRaisesRegex(oracle_driver.OracleDriverError, "restart"):
                    oracle_driver.load_oracle_driver()
                os.environ["ORACLE_CLIENT_LIB_DIR"] = str(Path(second) / "missing")
                with self.assertRaisesRegex(oracle_driver.OracleDriverError, "existing"):
                    oracle_driver.load_oracle_driver()
            self.modern.connect.assert_not_called()

    def test_only_legacy_gets_encoding_and_threaded_options(self):
        os.environ.update(ORACLE_ENCODING="UTF-8", ORACLE_NENCODING="UTF-8")
        for driver in (self.modern, self.legacy):
            oracle_driver.connect_oracle(driver=driver, user="test-user", password="test-password", dsn="test-dsn")
        self.modern.connect.assert_called_once_with(user="test-user", password="test-password", dsn="test-dsn")
        self.legacy.connect.assert_called_once_with(user="test-user", password="test-password", dsn="test-dsn",
                                                  encoding="UTF-8", nencoding="UTF-8", threaded=True)

    def test_authentication_and_network_failures_never_trigger_fallback(self):
        for code in ("ORA-01017", "ORA-12541"):
            with self.subTest(code=code):
                self.modern.connect.side_effect = RuntimeError(code)
                with self.imports() as importer:
                    with self.assertRaisesRegex(RuntimeError, code):
                        oracle_driver.connect_oracle(user="test-user", password="test-password", dsn="test-dsn")
                    importer.assert_called_once_with("oracledb")
        self.legacy.connect.assert_not_called()

    def test_repository_and_executor_honor_same_selected_driver_and_sid(self):
        os.environ.update(ORACLE_DRIVER="cx_oracle", ORACLE_USER="test-user", ORACLE_PASSWORD="test-password",
                          ORACLE_HOST="office-host", ORACLE_SID="OFFICE", ORACLE_PORT="1521")
        self.legacy.connect.return_value = SimpleNamespace(ping=Mock(), close=Mock())
        with self.imports():
            connection = connect_oracle_from_environment()
            self.legacy.makedsn.assert_called_once_with("office-host", 1521, sid="OFFICE")
            self.assertIs(connection, self.legacy.connect.return_value)
            executor = OracleExecutor()
            self.assertIn("(SID=OFFICE)", executor.dsn)
            self.assertIs(executor.connect(), connection)
            self.assertIs(executor.connect(), connection)
            self.assertEqual(self.legacy.connect.call_count, 2)
            connection.ping.assert_called_once_with()
        self.modern.connect.assert_not_called()

    def test_repository_authentication_failure_is_redacted_and_not_retried(self):
        os.environ.update(ORACLE_DRIVER="oracledb", ORACLE_USER="test-user", ORACLE_PASSWORD="test-password", ORACLE_DSN="test-dsn")
        self.modern.connect.side_effect = RuntimeError("ORA-01017 secret-password")
        with self.imports():
            with self.assertRaises(OracleMasterSyncError) as raised:
                connect_oracle_from_environment()
        self.assertIn("authentication", str(raised.exception))
        self.assertNotIn("secret-password", str(raised.exception))
        self.assertEqual(self.modern.connect.call_count, 1)
        self.legacy.connect.assert_not_called()

    def test_injected_executor_runs_mock_procedure_without_loading_any_driver(self):
        cursor = Mock()
        cursor.var.return_value.getvalue.return_value = 7
        connection = Mock()
        connection.cursor.return_value = cursor
        os.environ["ORACLE_DRIVER"] = "invalid-on-purpose"
        with self.imports() as importer:
            result = OracleExecutor(connection_factory=lambda: connection).execute("TEST_PKG.RUN_TEST", date(2026, 3, 31))
        importer.assert_not_called()
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.count, 7)
        connection.commit.assert_called_once_with()
        cursor.callproc.assert_called_once()

    def test_injected_repository_does_not_load_driver(self):
        connection = Mock()
        connection.cursor.return_value.description = [(name,) for name in (
            "ID", "NAME", "PACKAGE_NAME", "RUN_CONFIG", "MARGIN", "SAME_DAY", "IS_ACTIVE", "CREATED_DATE")]
        connection.cursor.return_value.fetchall.return_value = []
        with tempfile.TemporaryDirectory() as directory, self.imports() as importer:
            repository = OracleScheduleMasterRepository(Path(directory) / "master.json", connection_factory=lambda: connection)
            self.assertEqual(repository.fetch_records(), [])
        importer.assert_not_called()

    def test_legacy_ping_and_modern_health_failures_force_reconnection(self):
        self.assertTrue(oracle_driver.connection_is_healthy(SimpleNamespace(ping=Mock())))
        self.assertTrue(oracle_driver.connection_is_healthy(SimpleNamespace(is_healthy=lambda: True)))
        self.assertFalse(oracle_driver.connection_is_healthy(SimpleNamespace(is_healthy=lambda: False)))
        self.assertFalse(oracle_driver.connection_is_healthy(SimpleNamespace(ping=Mock(side_effect=RuntimeError("disconnected")))))


if __name__ == "__main__":
    unittest.main()
