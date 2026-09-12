import sqlite3

from config.settings import SQLITE_DB


def get_connection():
    """
    Return a SQLite connection to the scheduler database.
    """

    connection = sqlite3.connect(
        SQLITE_DB,
        check_same_thread=False,
    )

    connection.row_factory = sqlite3.Row

    return connection


def create_tables():
    """
    Create all scheduler tables if they do not already exist.

    SQLite is used for persistent scheduler/application state.

    The tables are:

        job_control
        staging_jobs
        ready_jobs

    The Python priority heap is NOT persisted here.
    READY is the persistent source of truth and the heap can
    always be rebuilt from it.
    """

    connection = get_connection()

    try:
        cursor = connection.cursor()

        # =====================================================
        # JOB CONTROL
        # =====================================================
        #
        # Authoritative control state for each job.
        #
        # control_status:
        #
        #     ACTIVE
        #     PAUSED
        #     CANCELLED
        #
        # manual_run:
        #
        #     0 = normal scheduling
        #     1 = manual run requested
        #
        # confirmation:
        #
        #     0 = not confirmed
        #     1 = confirmed
        #
        # override_datetime:
        #
        #     Optional scheduler datetime override.
        #
        # =====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS job_control (
                job_id              INTEGER PRIMARY KEY,

                control_status      TEXT NOT NULL
                                    DEFAULT 'ACTIVE',

                manual_run          INTEGER NOT NULL
                                    DEFAULT 0,

                confirmation        INTEGER NOT NULL
                                    DEFAULT 0,

                override_datetime   TEXT,

                updated_at          TEXT NOT NULL
            )
            """
        )

        # =====================================================
        # SCHEDULER SERVICE CONTROL + AUDIT
        # =====================================================
        #
        # A global stop is deliberately a *future-cycle gate*.
        # It does not terminate an Oracle call which has already begun; that
        # prevents a web click or CLI command from corrupting an active bank
        # process.  Every operational request is captured in the immutable
        # local audit table before the next scheduler cycle observes it.
        # =====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_control (
                control_id          INTEGER PRIMARY KEY CHECK (control_id = 1),
                scheduler_enabled   INTEGER NOT NULL DEFAULT 1,
                updated_at          TEXT NOT NULL,
                updated_by          TEXT,
                reason              TEXT
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_operation_audit (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at         TEXT NOT NULL,
                actor               TEXT,
                action              TEXT NOT NULL,
                target_type         TEXT NOT NULL,
                target_id           TEXT,
                target_label        TEXT,
                reason              TEXT,
                before_state        TEXT,
                after_state         TEXT,
                source              TEXT NOT NULL DEFAULT 'CONTROL_API'
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_scheduler_operation_audit_when
            ON scheduler_operation_audit (occurred_at DESC)
            """
        )

        # =====================================================
        # STAGING JOBS
        # =====================================================
        #
        # Persistent state for jobs that are not yet READY.
        #
        # Important fields:
        #
        # occurrence_date
        #     Scheduled occurrence represented by this record.
        #
        # t_date
        #     DATEMAST T date used for this occurrence.
        #
        # target_date
        #     T + margin calculated for this occurrence.
        #
        # These three values allow the scheduler to preserve
        # an occurrence across multiple 3-minute cycles.
        #
        # Example:
        #
        #     occurrence_date = 2026-08-31
        #     t_date          = 2026-08-31
        #     target_date     = 2026-09-03
        #
        # The scheduler must not recalculate target_date merely
        # because DATEMAST changes on a later cycle.
        #
        # =====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_queue_order (
                occurrence_key TEXT PRIMARY KEY,
                position INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT,
                reason TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS occurrence_confirmation (
                occurrence_key TEXT PRIMARY KEY,
                job_id INTEGER NOT NULL,
                confirmed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS staging_jobs (
                job_id                  INTEGER PRIMARY KEY,

                job_name                TEXT NOT NULL,

                state                   TEXT NOT NULL,

                occurrence_date        TEXT,

                execution_date         TEXT,

                t_date                  TEXT,

                report_date             TEXT,

                target_date             TEXT,

                margin                  TEXT,

                confirmation_required  INTEGER NOT NULL
                                       DEFAULT 0,

                confirmation_status    TEXT,

                time_flag               INTEGER NOT NULL
                                       DEFAULT 0,

                from_time               TEXT,

                to_time                 TEXT,

                waiting_for             TEXT,

                reason                  TEXT,

                next_evaluation         TEXT,

                calculated_at           TEXT NOT NULL,

                updated_at              TEXT NOT NULL
            )
            """
        )

        # =====================================================
        # READY JOBS
        # =====================================================
        #
        # Persistent READY view.
        #
        # The READY record retains the occurrence context:
        #
        #     occurrence_date
        #     execution_date
        #     t_date
        #     report_date
        #     target_date
        #
        # The Python priority heap is rebuilt from these records.
        #
        # Priority values are persisted because they are useful
        # for monitoring and debugging.
        #
        # They are recalculated on every scheduler cycle because
        # time priority is dynamic.
        #
        # =====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ready_jobs (
                job_id                  INTEGER PRIMARY KEY,

                job_name                TEXT NOT NULL,

                occurrence_date         TEXT,

                execution_date          TEXT,

                t_date                  TEXT,

                report_date             TEXT,

                target_date             TEXT,

                ready_since             TEXT NOT NULL,

                time_priority           INTEGER NOT NULL
                                        DEFAULT 0,

                date_priority           INTEGER NOT NULL
                                        DEFAULT 0,

                job_priority            INTEGER NOT NULL
                                        DEFAULT 0,

                priority_key            TEXT,

                from_time               TEXT,

                to_time                 TEXT,

                time_state              TEXT,

                updated_at              TEXT NOT NULL
            )
            """
        )

        # =====================================================
        # OCCURRENCE-SCOPED STAGING / READY
        # =====================================================
        #
        # The original local tables above use job_id as their primary key.
        # That representation cannot hold both a DAILY and a FORTNIGHTLY
        # occurrence for one master job.  The Oracle package instead uses
        # NAME + REPORT_DATE as the durable staging identity.  These tables
        # preserve that behavior locally using the immutable master ID plus
        # report date (``occurrence_key``), while retaining job_id for job
        # controls and audit/history joins.
        #
        # The old tables are intentionally retained for an upgrade-safe
        # migration and legacy integrations. Repositories use these new
        # occurrence-scoped tables for all new state.
        # =====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS staging_occurrences (
                occurrence_key          TEXT PRIMARY KEY,
                job_id                  INTEGER NOT NULL,
                job_name                TEXT NOT NULL,
                state                   TEXT NOT NULL,
                occurrence_date         TEXT,
                execution_date          TEXT,
                t_date                  TEXT,
                report_date             TEXT,
                target_date             TEXT,
                margin                  TEXT,
                confirmation_required  INTEGER NOT NULL DEFAULT 0,
                confirmation_status    TEXT,
                time_flag               INTEGER NOT NULL DEFAULT 0,
                from_time               TEXT,
                to_time                 TEXT,
                waiting_for             TEXT,
                reason                  TEXT,
                next_evaluation         TEXT,
                calculated_at           TEXT NOT NULL,
                updated_at              TEXT NOT NULL,
                UNIQUE(job_id, report_date)
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_staging_occurrences_job
            ON staging_occurrences (job_id, execution_date, report_date)
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ready_occurrences (
                occurrence_key          TEXT PRIMARY KEY,
                job_id                  INTEGER NOT NULL,
                job_name                TEXT NOT NULL,
                occurrence_date         TEXT,
                execution_date          TEXT,
                t_date                  TEXT,
                report_date             TEXT,
                target_date             TEXT,
                ready_since             TEXT NOT NULL,
                time_priority           INTEGER NOT NULL DEFAULT 0,
                date_priority           INTEGER NOT NULL DEFAULT 0,
                job_priority            INTEGER NOT NULL DEFAULT 0,
                priority_key            TEXT,
                from_time               TEXT,
                to_time                 TEXT,
                time_state              TEXT,
                updated_at              TEXT NOT NULL,
                UNIQUE(job_id, report_date)
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ready_occurrences_job
            ON ready_occurrences (job_id, report_date, ready_since)
            """
        )

        # A one-time marker prevents legacy job-keyed rows from being copied
        # back into the occurrence tables after an operator has completed or
        # intentionally removed the migrated occurrence.  The migration is
        # still atomic/idempotent: data copy and marker insertion commit
        # together at the end of create_tables().
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduler_schema_migrations (
                migration_key TEXT PRIMARY KEY,
                applied_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # =====================================================
        # EXECUTION HISTORY
        # =====================================================
        #
        # Each Oracle attempt receives its own durable row.  This table is
        # intentionally independent from READY so a historic failure can be
        # retried in the permitted retry window while a later occurrence of
        # the same job is progressing through STAGING/READY.
        # =====================================================

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_jobs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id              INTEGER NOT NULL,
                job_name            TEXT,
                procedure_name      TEXT,
                report_date         TEXT,
                status              TEXT NOT NULL,
                attempt_no          INTEGER NOT NULL DEFAULT 1,
                count               INTEGER,
                started_at          TEXT,
                finished_at         TEXT,
                error               TEXT,
                error_type          TEXT,
                duration_seconds    REAL,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_execution_occurrence
            ON execution_jobs (job_id, report_date, id DESC)
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_execution_retry_candidates
            ON execution_jobs (status, report_date, id ASC)
            """
        )

        # =====================================================
        # MIGRATIONS
        # =====================================================
        #
        # CREATE TABLE IF NOT EXISTS does NOT modify an existing
        # SQLite table.
        #
        # Therefore older scheduler.db files need lightweight
        # migrations.
        #
        # =====================================================

        _add_column_if_missing(
            connection=connection,
            table_name="staging_jobs",
            column_name="occurrence_date",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="staging_jobs",
            column_name="execution_date",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="staging_jobs",
            column_name="t_date",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="staging_jobs",
            column_name="time_flag",
            column_definition=(
                "INTEGER NOT NULL DEFAULT 0"
            ),
        )

        _add_column_if_missing(
            connection=connection,
            table_name="staging_jobs",
            column_name="from_time",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="staging_jobs",
            column_name="to_time",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="ready_jobs",
            column_name="occurrence_date",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="ready_jobs",
            column_name="execution_date",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="ready_jobs",
            column_name="t_date",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="ready_jobs",
            column_name="target_date",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="ready_jobs",
            column_name="from_time",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="ready_jobs",
            column_name="to_time",
            column_definition="TEXT",
        )

        _add_column_if_missing(
            connection=connection,
            table_name="ready_jobs",
            column_name="time_state",
            column_definition="TEXT",
        )

        # Copy in-flight job-keyed state once, after all legacy columns are
        # available.  ``INSERT OR IGNORE`` protects a partially upgraded
        # database which already has occurrence rows; the migration marker
        # protects completed rows from returning on later app starts.
        migration_key = "occurrence_scoped_state_v1"
        migrated = cursor.execute(
            "SELECT 1 FROM scheduler_schema_migrations WHERE migration_key = ?",
            (migration_key,),
        ).fetchone()

        if migrated is None:
            cursor.execute(
                """
                INSERT OR IGNORE INTO staging_occurrences (
                    occurrence_key, job_id, job_name, state, occurrence_date,
                    execution_date, t_date, report_date, target_date, margin,
                    confirmation_required, confirmation_status, time_flag,
                    from_time, to_time, waiting_for, reason, next_evaluation,
                    calculated_at, updated_at
                )
                SELECT
                    CAST(job_id AS TEXT) || ':' ||
                        COALESCE(report_date, occurrence_date, 'legacy'),
                    job_id, job_name, state, occurrence_date, execution_date,
                    t_date, report_date, target_date, margin,
                    confirmation_required, confirmation_status, time_flag,
                    from_time, to_time, waiting_for, reason, next_evaluation,
                    calculated_at, updated_at
                FROM staging_jobs
                """
            )

            cursor.execute(
                """
                INSERT OR IGNORE INTO ready_occurrences (
                    occurrence_key, job_id, job_name, occurrence_date,
                    execution_date, t_date, report_date, target_date,
                    ready_since, time_priority, date_priority, job_priority,
                    priority_key, from_time, to_time, time_state, updated_at
                )
                SELECT
                    CAST(job_id AS TEXT) || ':' ||
                        COALESCE(report_date, occurrence_date, 'legacy'),
                    job_id, job_name, occurrence_date, execution_date, t_date,
                    report_date, target_date, ready_since, time_priority,
                    date_priority, job_priority, priority_key, from_time,
                    to_time, time_state, updated_at
                FROM ready_jobs
                """
            )

            cursor.execute(
                "INSERT INTO scheduler_schema_migrations (migration_key) VALUES (?)",
                (migration_key,),
            )

        # -----------------------------------------------------
        # Commit all schema changes.
        # -----------------------------------------------------

        connection.commit()

    finally:
        connection.close()


def _get_table_columns(
    connection,
    table_name,
):
    """
    Return the existing column names for a SQLite table.
    """

    cursor = connection.cursor()

    rows = cursor.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return {
        row["name"]
        for row in rows
    }


def _add_column_if_missing(
    connection,
    table_name,
    column_name,
    column_definition,
):
    """
    Add a column to an existing table if it does not exist.

    This provides a lightweight schema migration mechanism for
    the scheduler's local SQLite database.
    """

    existing_columns = _get_table_columns(
        connection,
        table_name,
    )

    if column_name in existing_columns:
        return

    cursor = connection.cursor()

    cursor.execute(
        f"""
        ALTER TABLE {table_name}
        ADD COLUMN {column_name}
        {column_definition}
        """
    )


if __name__ == "__main__":
    create_tables()

    print(
        "SQLite scheduler database initialized."
    )
