"""Deployment path isolation; no access to an existing scheduler database."""
import importlib.util
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import config.settings as settings
import database.sqlite_db as sqlite_db


class RuntimeDataPathTests(unittest.TestCase):
    def load_settings(self, value):
        with patch.dict(os.environ, {"SCHEDULER_DATA_DIR": value}, clear=True), patch("dotenv.load_dotenv"), patch.object(Path, "mkdir"):
            return runpy.run_path(settings.__file__)

    def test_default_paths_preserve_existing_installation(self):
        configured = self.load_settings("")
        root = Path(settings.__file__).resolve().parent.parent
        self.assertEqual(configured["DATA_DIR"], root)
        self.assertEqual(configured["SQLITE_DB"], root / "scheduler.db")
        self.assertEqual(configured["SCHEDULE_MASTER_FILE"], root / "file_repository" / "Schedule_Master.json")

    def test_relative_paths_are_resolved_from_engine_root(self):
        configured = self.load_settings("runtime/office")
        root = Path(settings.__file__).resolve().parent.parent / "runtime" / "office"
        self.assertEqual(configured["DATA_DIR"], root)
        self.assertEqual(configured["SQLITE_DB"], root / "scheduler.db")
        self.assertEqual(configured["LOG_FILE"], root / "logs" / "scheduler.log")
        for name in ("SCHEDULE_MASTER_FILE", "SCHEDULE_EXTG_FILE", "DATEMASTER_FILE", "HOLIDAY_MASTER_FILE"):
            self.assertEqual(configured[name].parent, root / "file_repository")

    def test_new_deployment_creates_only_its_own_database(self):
        with tempfile.TemporaryDirectory() as directory:
            configured = self.load_settings(directory)
            self.assertEqual(configured["DATA_DIR"], Path(directory).resolve())
            isolated_settings = ModuleType("config.settings")
            isolated_settings.__dict__.update(configured)
            spec = importlib.util.spec_from_file_location("isolated_sqlite", sqlite_db.__file__)
            isolated_database = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {"config.settings": isolated_settings}):
                spec.loader.exec_module(isolated_database)
            connection = isolated_database.get_connection()
            try:
                self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
            finally:
                connection.close()
            self.assertTrue((Path(directory) / "scheduler.db").is_file())


if __name__ == "__main__":
    unittest.main()
