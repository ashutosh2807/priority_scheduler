import sqlite3
from pathlib import Path

from config.settings import SQLITE_DB


# ============================================================================
# CONSTANTS
# ============================================================================

LINE = "=" * 80
SMALL_LINE = "-" * 80


# ============================================================================
# DATABASE
# ============================================================================

def get_connection():
    """
    Open the scheduler SQLite database.
    """

    connection = sqlite3.connect(
        SQLITE_DB
    )

    connection.row_factory = sqlite3.Row

    return connection


# ============================================================================
# DISPLAY HELPERS
# ============================================================================

def print_header(title):
    print()
    print(LINE)
    print(title)
    print(LINE)


def print_rows(rows, columns):
    """
    Print SQLite rows in a simple table.
    """

    if not rows:
        print("No records.")
        return

    # ------------------------------------------------------------------------
    # Calculate column widths
    # ------------------------------------------------------------------------

    widths = {}

    for column in columns:

        width = len(column)

        for row in rows:

            value = row[column]

            if value is None:
                value = "NULL"

            value = str(value)

            width = max(
                width,
                len(value),
            )

        widths[column] = width

    # ------------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------------

    header = " | ".join(
        column.ljust(widths[column])
        for column in columns
    )

    print(header)

    print(
        "-+-".join(
            "-" * widths[column]
            for column in columns
        )
    )

    # ------------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------------

    for row in rows:

        values = []

        for column in columns:

            value = row[column]

            if value is None:
                value = "NULL"

            value = str(value)

            values.append(
                value.ljust(widths[column])
            )

        print(" | ".join(values))


# ============================================================================
# TABLE EXISTENCE
# ============================================================================

def table_exists(connection, table_name):
    """
    Check whether a table exists.
    """

    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()

    return row is not None


# ============================================================================
# TABLE COLUMNS
# ============================================================================

def get_table_columns(connection, table_name):
    """
    Return column names for a table.
    """

    rows = connection.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    return [
        row["name"]
        for row in rows
    ]


# ============================================================================
# JOB COUNTS
# ============================================================================

def show_job_counts(connection):
    """
    Display scheduler state counts.

    STAGING:
        Persistent scheduler staging state.

    READY:
        Persistent ready/queue state.

    RUNNING:
        Oracle executions currently running.

    FAILED:
        Failed Oracle execution attempts.

    SUCCESS:
        Successful Oracle execution attempts.
    """

    print_header("JOB COUNTS")

    counts = []

    # ------------------------------------------------------------------------
    # STAGING
    # ------------------------------------------------------------------------

    if table_exists(
        connection,
        "staging_jobs",
    ):

        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM staging_jobs
            """
        ).fetchone()

        counts.append(
            (
                "STAGING",
                row["count"],
            )
        )

    else:

        counts.append(
            (
                "STAGING",
                "TABLE MISSING",
            )
        )

    # ------------------------------------------------------------------------
    # READY
    # ------------------------------------------------------------------------

    if table_exists(
        connection,
        "ready_jobs",
    ):

        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM ready_jobs
            """
        ).fetchone()

        counts.append(
            (
                "READY",
                row["count"],
            )
        )

    else:

        counts.append(
            (
                "READY",
                "TABLE MISSING",
            )
        )

    # ------------------------------------------------------------------------
    # RUNNING
    # ------------------------------------------------------------------------

    if table_exists(
        connection,
        "execution_jobs",
    ):

        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM execution_jobs
            WHERE UPPER(status) = 'RUNNING'
            """
        ).fetchone()

        counts.append(
            (
                "RUNNING",
                row["count"],
            )
        )

    else:

        counts.append(
            (
                "RUNNING",
                "TABLE MISSING",
            )
        )

    # ------------------------------------------------------------------------
    # FAILED
    # ------------------------------------------------------------------------

    if table_exists(
        connection,
        "execution_jobs",
    ):

        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM execution_jobs
            WHERE UPPER(status) = 'FAILED'
            """
        ).fetchone()

        counts.append(
            (
                "FAILED",
                row["count"],
            )
        )

    else:

        counts.append(
            (
                "FAILED",
                "TABLE MISSING",
            )
        )

    # ------------------------------------------------------------------------
    # SUCCESS
    # ------------------------------------------------------------------------

    if table_exists(
        connection,
        "execution_jobs",
    ):

        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM execution_jobs
            WHERE UPPER(status) = 'SUCCESS'
            """
        ).fetchone()

        counts.append(
            (
                "SUCCESS",
                row["count"],
            )
        )

    else:

        counts.append(
            (
                "SUCCESS",
                "TABLE MISSING",
            )
        )

    # ------------------------------------------------------------------------
    # Print
    # ------------------------------------------------------------------------

    print("State   | Count")
    print("--------+----------------")

    for state, count in counts:

        print(
            f"{state:<7} | {count}"
        )


# ============================================================================
# READY / QUEUE
# ============================================================================

def show_ready(connection):
    """
    Display persistent READY jobs.
    """

    print_header("READY / QUEUE")

    if not table_exists(
        connection,
        "ready_jobs",
    ):

        print("ready_jobs table does not exist.")

        return

    rows = connection.execute(
        """
        SELECT
            job_id,
            job_name,
            report_date,
            ready_since,
            time_priority,
            date_priority,
            job_priority,
            priority_key,
            from_time,
            to_time,
            time_state,
            updated_at
        FROM ready_jobs
        ORDER BY
            date_priority DESC,
            time_priority DESC,
            job_priority DESC,
            ready_since ASC,
            job_id ASC
        """
    ).fetchall()

    columns = [
        "job_id",
        "job_name",
        "report_date",
        "ready_since",
        "time_priority",
        "date_priority",
        "job_priority",
        "priority_key",
        "from_time",
        "to_time",
        "time_state",
        "updated_at",
    ]

    print_rows(
        rows,
        columns,
    )


# ============================================================================
# STAGING
# ============================================================================

def show_staging(connection):
    """
    Display STAGING and waiting jobs.
    """

    print_header("STAGING / WAITING")

    if not table_exists(
        connection,
        "staging_jobs",
    ):

        print("staging_jobs table does not exist.")

        return

    rows = connection.execute(
        """
        SELECT
            job_id,
            job_name,
            state,
            occurrence_date,
            t_date,
            report_date,
            target_date,
            margin,
            confirmation_required,
            confirmation_status,
            time_flag,
            from_time,
            to_time,
            waiting_for,
            reason,
            next_evaluation,
            calculated_at,
            updated_at
        FROM staging_jobs
        ORDER BY
            job_id
        """
    ).fetchall()

    columns = [
        "job_id",
        "job_name",
        "state",
        "occurrence_date",
        "t_date",
        "report_date",
        "target_date",
        "margin",
        "confirmation_required",
        "confirmation_status",
        "time_flag",
        "from_time",
        "to_time",
        "waiting_for",
        "reason",
        "next_evaluation",
        "calculated_at",
        "updated_at",
    ]

    print_rows(
        rows,
        columns,
    )


# ============================================================================
# RUNNING
# ============================================================================

def show_running(connection):
    """
    Display currently running execution attempts.
    """

    print_header("RUNNING")

    if not table_exists(
        connection,
        "execution_jobs",
    ):

        print("execution_jobs table does not exist.")

        return

    rows = connection.execute(
        """
        SELECT
            id,
            job_id,
            job_name,
            procedure_name,
            report_date,
            attempt_no,
            count,
            started_at,
            updated_at
        FROM execution_jobs
        WHERE UPPER(status) = 'RUNNING'
        ORDER BY
            started_at ASC,
            id ASC
        """
    ).fetchall()

    columns = [
        "id",
        "job_id",
        "job_name",
        "procedure_name",
        "report_date",
        "attempt_no",
        "count",
        "started_at",
        "updated_at",
    ]

    print_rows(
        rows,
        columns,
    )


# ============================================================================
# FAILED
# ============================================================================

def show_failed(connection):
    """
    Display failed Oracle execution attempts.
    """

    print_header("FAILED")

    if not table_exists(
        connection,
        "execution_jobs",
    ):

        print("execution_jobs table does not exist.")

        return

    rows = connection.execute(
        """
        SELECT
            id,
            job_id,
            job_name,
            procedure_name,
            report_date,
            status,
            attempt_no,
            count,
            started_at,
            finished_at,
            duration_seconds,
            error_type,
            error,
            updated_at
        FROM execution_jobs
        WHERE UPPER(status) = 'FAILED'
        ORDER BY
            finished_at DESC,
            id DESC
        """
    ).fetchall()

    columns = [
        "id",
        "job_id",
        "job_name",
        "procedure_name",
        "report_date",
        "status",
        "attempt_no",
        "count",
        "started_at",
        "finished_at",
        "duration_seconds",
        "error_type",
        "error",
        "updated_at",
    ]

    print_rows(
        rows,
        columns,
    )


# ============================================================================
# SUCCESS
# ============================================================================

def show_success(connection):
    """
    Display successful Oracle execution attempts.
    """

    print_header("SUCCESS")

    if not table_exists(
        connection,
        "execution_jobs",
    ):

        print("execution_jobs table does not exist.")

        return

    rows = connection.execute(
        """
        SELECT
            id,
            job_id,
            job_name,
            procedure_name,
            report_date,
            status,
            attempt_no,
            count,
            started_at,
            finished_at,
            duration_seconds,
            updated_at
        FROM execution_jobs
        WHERE UPPER(status) = 'SUCCESS'
        ORDER BY
            finished_at DESC,
            id DESC
        """
    ).fetchall()

    columns = [
        "id",
        "job_id",
        "job_name",
        "procedure_name",
        "report_date",
        "status",
        "attempt_no",
        "count",
        "started_at",
        "finished_at",
        "duration_seconds",
        "updated_at",
    ]

    print_rows(
        rows,
        columns,
    )


# ============================================================================
# JOB CONTROL
# ============================================================================

def show_job_control(connection):
    """
    Display authoritative scheduler control state.

    IMPORTANT:
        job_control uses control_status, NOT status.
    """

    print_header("JOB CONTROL")

    if not table_exists(
        connection,
        "job_control",
    ):

        print("job_control table does not exist.")

        return

    rows = connection.execute(
        """
        SELECT
            job_id,
            control_status,
            manual_run,
            confirmation,
            override_datetime,
            updated_at
        FROM job_control
        ORDER BY
            job_id
        """
    ).fetchall()

    columns = [
        "job_id",
        "control_status",
        "manual_run",
        "confirmation",
        "override_datetime",
        "updated_at",
    ]

    print_rows(
        rows,
        columns,
    )


# ============================================================================
# EXECUTION HISTORY
# ============================================================================

def show_execution_history(connection):
    """
    Display complete execution history.

    This is useful for seeing retries:

        attempt 1 -> FAILED
        attempt 2 -> FAILED
        attempt 3 -> SUCCESS
    """

    print_header("EXECUTION HISTORY")

    if not table_exists(
        connection,
        "execution_jobs",
    ):

        print("execution_jobs table does not exist.")

        return

    rows = connection.execute(
        """
        SELECT
            id,
            job_id,
            job_name,
            procedure_name,
            report_date,
            status,
            attempt_no,
            count,
            started_at,
            finished_at,
            duration_seconds,
            error_type,
            error,
            created_at,
            updated_at
        FROM execution_jobs
        ORDER BY
            id DESC
        """
    ).fetchall()

    columns = [
        "id",
        "job_id",
        "job_name",
        "procedure_name",
        "report_date",
        "status",
        "attempt_no",
        "count",
        "started_at",
        "finished_at",
        "duration_seconds",
        "error_type",
        "error",
        "created_at",
        "updated_at",
    ]

    print_rows(
        rows,
        columns,
    )


# ============================================================================
# DATABASE SCHEMA
# ============================================================================

def show_schema(connection):
    """
    Display SQLite table structure.

    Useful for diagnosing schema mismatches.
    """

    print_header("DATABASE SCHEMA")

    tables = [
        "job_control",
        "staging_jobs",
        "ready_jobs",
        "execution_jobs",
    ]

    for table_name in tables:

        print()
        print(f"[{table_name}]")

        if not table_exists(
            connection,
            table_name,
        ):

            print("TABLE MISSING")

            continue

        columns = get_table_columns(
            connection,
            table_name,
        )

        for column in columns:

            print(
                f"  - {column}"
            )


# ============================================================================
# MAIN
# ============================================================================

def main():

    print()
    print("Scheduler SQLite Monitor")
    print(LINE)

    database_path = Path(
        SQLITE_DB
    ).resolve()

    print(
        f"Database: {database_path}"
    )

    print(
        f"Exists:   {database_path.exists()}"
    )

    if not database_path.exists():

        print()
        print(
            "ERROR: scheduler.db does not exist."
        )

        return

    connection = None

    try:

        connection = get_connection()

        # --------------------------------------------------------------------
        # Main monitoring sections
        # --------------------------------------------------------------------

        show_job_counts(connection)

        show_ready(connection)

        show_staging(connection)

        show_running(connection)

        show_failed(connection)

        show_success(connection)

        show_job_control(connection)

        show_execution_history(connection)

        # --------------------------------------------------------------------
        # Schema is useful during development but can be commented out later.
        # --------------------------------------------------------------------

        show_schema(connection)

    except sqlite3.Error as exc:

        print()
        print(
            f"SQLite ERROR: {exc}"
        )

    finally:

        if connection is not None:

            connection.close()


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()