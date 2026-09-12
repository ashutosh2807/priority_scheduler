import json
import tempfile
import unittest
from pathlib import Path

from import_schedule_master import canonical_record, plan_import, reconcile, validate_records
from repositories.oracle_schedule_master_repository import OracleScheduleMasterRepository


def source(job_id=1, **kwargs):
    return {"id": job_id, "name": f"REPORT_{job_id}", "package_name": "REPORTS.DAILY",
            "is_active": 1, "confirmation_needed": 1, "time_flag": 0,
            "run_config": {"RUNS_ON": ["DAILY"], "MAX_ATTEMPTS": 8}, **kwargs}


class Connection:
    def __init__(self, rows=(), fail_at=None):
        self.rows = rows
        self.calls = []
        self.committed = self.rolled_back = False
        self.writes = 0
        self.fail_at = fail_at
        self.rowcount = 1

    def cursor(self): return self
    def fetchall(self): return self.rows
    def execute(self, sql, binds=None):
        self.calls.append((sql, binds))
        if sql.startswith(("UPDATE", "INSERT")):
            self.writes += 1
            if self.writes == self.fail_at:
                raise RuntimeError("Simulated failure")
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): pass


class ImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.validator = OracleScheduleMasterRepository(self.directory / "source.json", optional_columns=("TIME_FLAG", "CONFIRMATION"))

    def existing_row(self, row):
        return (row['id'], row['name'], row['package_name'], json.dumps(row['run_config']),
                row['margin'], row['same_day'], row['is_active'], row['created_date'],
                row['time_flag'], row['confirmation_needed'])

    def test_dry_run_and_repeated_import_do_not_write(self):
        incoming = validate_records([source(is_active=0)], self.validator)
        connection = Connection([self.existing_row(incoming[0])])
        result = reconcile(incoming, self.validator, connection_factory=lambda: connection)
        self.assertEqual(result['counts'], {'insert': 0, 'update': 0, 'unchanged': 1})
        self.assertFalse(result['applied'])
        self.assertEqual(connection.writes, 0)
        self.assertFalse(connection.committed)
        self.assertFalse(list(self.directory.iterdir()))

    def test_insert_update_one_transaction_backup_and_actual_confirmation_column(self):
        old = canonical_record(source(), self.validator)
        incoming = validate_records([source(is_active=0), source(2)], self.validator)
        connection = Connection([self.existing_row(old)])
        result = reconcile(incoming, self.validator, apply=True, backup_dir=self.directory,
                           connection_factory=lambda: connection)
        self.assertEqual(result['counts'], {'insert': 1, 'update': 1, 'unchanged': 0})
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        update_sql, update_binds = connection.calls[1]
        self.assertIn('CONFIRMATION = :confirmation', update_sql)
        self.assertNotIn('LAST_RUN', update_sql)
        self.assertNotIn('CREATED_DATE =', update_sql)
        self.assertEqual(update_binds['is_active'], 0)
        self.assertEqual(update_binds['confirmation'], 1)
        backup = json.loads(Path(result['backup']).read_text(encoding='utf-8'))
        self.assertEqual(backup['oracle_before'][0]['IS_ACTIVE'], 1)
        self.assertTrue(all('DELETE' not in sql for sql, _ in connection.calls))

    def test_any_write_failure_rolls_back_entire_batch_and_retains_backup(self):
        connection = Connection(fail_at=2)
        incoming = validate_records([source(), source(2)], self.validator)
        with self.assertRaises(RuntimeError):
            reconcile(incoming, self.validator, apply=True, backup_dir=self.directory,
                      connection_factory=lambda: connection)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertEqual(len(list(self.directory.glob('schedule-master-*.json'))), 1)

    def test_repeat_import_without_optional_columns_is_unchanged(self):
        validator = OracleScheduleMasterRepository(self.directory / "source.json", optional_columns=())
        incoming = validate_records([source(time_flag=0, confirmation_needed=1)], validator)
        first = Connection()
        reconcile(incoming, validator, apply=True, backup_dir=self.directory, connection_factory=lambda: first)
        inserted = first.calls[-1][1]
        stored = (inserted['id'], inserted['name'], inserted['package_name'], inserted['run_config'],
                  inserted['margin'], inserted['same_day'], inserted['is_active'], inserted['created_date'])
        second = Connection([stored])
        result = reconcile(validate_records([source()], validator), validator, apply=True,
                           backup_dir=self.directory, connection_factory=lambda: second)
        self.assertEqual(result['counts'], {'insert': 0, 'update': 0, 'unchanged': 1})
        self.assertEqual(second.writes, 0)

    def test_conflicting_identity_and_invalid_input_rejected_before_writes(self):
        old = validate_records([source()], self.validator)
        for rows in ([source(), source()], [source(2, name='REPORT_1')], [source(name='Different report')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                plan_import(validate_records(rows, self.validator), old)
        for invalid in ([], [source(id=True)], [source(run_config={'RUNS_ON': ['DAILY'], 'MAX_ATTEMPTS': 101})]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_records(invalid, self.validator)


if __name__ == '__main__':
    unittest.main()
