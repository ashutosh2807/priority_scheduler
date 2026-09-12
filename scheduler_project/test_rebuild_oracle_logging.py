"""Migration rehearsal with an Oracle-shaped fake; no live DDL is run."""

import copy
import json
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import rebuild_oracle_logging as migration


OLD_COLUMNS = ["ID", "NAME", "REPORT_DATE", "RUN_DATE", "STATUS", "SAME_DAY", "ATTEMPT_NO",
               "EXECUTED_AT", "RECORDS_LOADED", "ERROR_INFO", "CREATED_DATE", "CONFIRMATION",
               "RUN_CONFIG", "TIME_FLAG", "LAST_RUN"]


def original_row():
    return {"ID": 9, "NAME": "OLD_REPORT", "REPORT_DATE": datetime(2026, 9, 10),
            "RUN_DATE": datetime(2026, 9, 11), "STATUS": "SUCCESS", "SAME_DAY": 0,
            "ATTEMPT_NO": 2, "EXECUTED_AT": datetime(2026, 9, 11, 10, 3), "RECORDS_LOADED": 53,
            "ERROR_INFO": None, "CREATED_DATE": datetime(2026, 1, 1, 0, 0, 0, 771000), "CONFIRMATION": 1,
            "RUN_CONFIG": '{"RUNS_ON":["DAILY"]}', "TIME_FLAG": 1,
            "LAST_RUN": datetime(2026, 9, 11, 10, 3, 0, 786000)}


class Database:
    def __init__(self, rows=None, fail_index=False, change_on_backup=False):
        self.tables = {"SCHEDULE_EXTG": copy.deepcopy([original_row()] if rows is None else rows)}
        self.calls = []
        self.fail_index = fail_index
        self.change_on_backup = change_on_backup
        self.cursors = []

    def cursor(self):
        cursor = Cursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        pass

    def close(self):
        pass


class Cursor:
    def __init__(self, database):
        self.database = database
        self.results = []
        self.description = []
        self.sizes = {}

    def setinputsizes(self, **kwargs):
        self.sizes.update(kwargs)

    def execute(self, sql, values=None):
        self.database.calls.append((sql, values))
        if set(self.sizes) - set(re.findall(r":([a-z_]+)", sql)):
            raise AssertionError("CLOB bind declaration leaked into another statement.")
        if "DBMS_METADATA.GET_DDL" in sql:
            self.results = [("CREATE TABLE SCHEDULE_EXTG (ID NUMBER)",)]
        elif sql.startswith("SELECT * FROM SCHEDULE_EXTG"):
            self.description = [(column,) for column in OLD_COLUMNS]
            self.results = [tuple(row[column] for column in OLD_COLUMNS) for row in self.database.tables["SCHEDULE_EXTG"]]
        elif sql.startswith("SELECT COUNT(*)"):
            self.results = [(len(self.database.tables[sql.split(" FROM ")[1]]),)]
        elif sql.startswith("SELECT "):
            columns, table = sql[7:].split(" FROM ")
            table = table.split(" ORDER BY")[0]
            self.results = [tuple(row[column.strip()] for column in columns.split(",")) for row in self.database.tables[table]]
        elif sql.startswith("CREATE TABLE "):
            table = sql.split()[2]
            if " AS SELECT * FROM " in sql:
                if self.database.change_on_backup:
                    self.database.tables["SCHEDULE_EXTG"][0]["RECORDS_LOADED"] = 999
                self.database.tables[table] = copy.deepcopy(self.database.tables["SCHEDULE_EXTG"])
            else:
                self.database.tables[table] = []
        elif sql.startswith("INSERT INTO "):
            table = sql.split()[2]
            self.database.tables[table].append({key.upper(): value.replace(microsecond=0)
                if isinstance(value, datetime) and self.sizes.get(key) != "TIMESTAMP" else value for key, value in values.items()})
        elif sql.startswith("CREATE INDEX "):
            if self.database.fail_index:
                raise RuntimeError("Simulated index error")
        elif sql.startswith("DROP TABLE "):
            del self.database.tables[sql.split()[2]]
        elif sql.startswith("ALTER TABLE "):
            parts = sql.split()
            self.database.tables[parts[-1]] = self.database.tables.pop(parts[2])
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.results[0]

    def fetchall(self):
        return self.results

    def close(self):
        pass


class RebuildTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backups = Path(directory.name)
        self.schema = migration.ddl_statements(migration.ROOT / "sql" / "02_oracle_logging_schema.sql")
        self.state = {"columns": OLD_COLUMNS, "log_columns": [], "rows": 1,
                      "foreign_references": [], "dependencies": [], "triggers": [], "grants": []}

    def run_migration(self, database, apply=True):
        with patch.object(migration, "inspect", return_value=self.state), patch.object(migration, "connect_oracle_from_environment", return_value=database), patch.object(migration, "load_oracle_driver", return_value=SimpleNamespace(DB_TYPE_CLOB="CLOB", DB_TYPE_TIMESTAMP="TIMESTAMP")):
            return migration.rebuild(apply=apply, backup_root=self.backups)

    def test_preview_runs_no_ddl_and_creates_no_backup(self):
        database = Database()
        result = self.run_migration(database, apply=False)
        self.assertEqual(result["status"], "preview")
        self.assertEqual(database.calls, [])
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_actual_legacy_columns_survive_and_log_is_prepared_before_drop(self):
        database = Database()
        result = self.run_migration(database)
        self.assertEqual(result["status"], "complete")
        current = database.tables["SCHEDULE_EXTG"][0]
        self.assertEqual({column: current[column] for column in OLD_COLUMNS}, original_row())
        self.assertEqual(current["SOURCE_ID"], "LEGACY")
        self.assertEqual(current["RECORD_KEY"], "legacy:9")
        self.assertEqual(current["EVENT_SEQ"], 0)
        self.assertEqual(len(database.tables["SCHEDULE_EXTG_LOG"]), 1)
        sql = [call[0] for call in database.calls]
        drop_index = sql.index("DROP TABLE SCHEDULE_EXTG")
        self.assertTrue(all(index < drop_index for index, value in enumerate(sql) if value.startswith(("INSERT INTO", "CREATE INDEX"))))
        self.assertIn("START WITH 10", next(value for value in sql if value.startswith("CREATE TABLE S_EXTG_NEW")))
        backup = Path(result["backup_directory"])
        self.assertTrue((backup / "original-table.sql").is_file())
        self.assertEqual(json.loads((backup / "original-data.json").read_text())[0]["RECORDS_LOADED"], 53)

    def test_same_row_count_with_changed_values_prevents_drop(self):
        database = Database(change_on_backup=True)
        with self.assertRaisesRegex(RuntimeError, "changed during backup"):
            self.run_migration(database)
        self.assertIn("SCHEDULE_EXTG", database.tables)
        self.assertFalse(any(sql == "DROP TABLE SCHEDULE_EXTG" for sql, _ in database.calls))

    def test_index_failure_preserves_original_table(self):
        database = Database(fail_index=True)
        with self.assertRaisesRegex(RuntimeError, "index error"):
            self.run_migration(database)
        self.assertEqual(database.tables["SCHEDULE_EXTG"], [original_row()])
        self.assertFalse(any(sql.startswith("DROP") for sql, _ in database.calls))

    def test_empty_old_table_is_supported(self):
        self.state["rows"] = 0
        database = Database(rows=[])
        result = self.run_migration(database)
        self.assertEqual(result["legacy_rows"], 0)
        self.assertEqual(database.tables["SCHEDULE_EXTG"], [])

    def test_unknown_legacy_columns_and_incomplete_new_schema_are_rejected_without_ddl(self):
        for columns in (OLD_COLUMNS + ["UNKNOWN_OLD_COLUMN"], ["SOURCE_ID", "RECORD_KEY", "EVENT_SEQ", "PAYLOAD"]):
            self.state["columns"] = columns
            database = Database()
            with self.assertRaises(RuntimeError):
                self.run_migration(database)
            self.assertEqual(database.calls, [])


if __name__ == "__main__":
    unittest.main()
