import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from datetime_compat import parse_iso_datetime
from pathlib import Path
from typing import Callable, Optional

from database.oracle_driver import connect_oracle, connection_is_healthy, load_oracle_driver
try:
    from dotenv import load_dotenv
except ImportError:  # Keep office hosts usable when environment variables are managed externally.
    def load_dotenv(*_args, **_kwargs):
        """No-op fallback; ORACLE_* may be supplied by Windows/service env."""
        return False


# ============================================================================
# ENVIRONMENT
# ============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = BASE_DIR / ".env"

load_dotenv(ENV_FILE)


# ============================================================================
# RESULT
# ============================================================================


@dataclass
class ExecutionResult:
    """
    Result returned by OracleExecutor.
    """

    success: bool
    procedure_name: str
    report_date: Optional[date] = None
    count: Optional[int] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: Optional[float] = None


# ============================================================================
# ORACLE EXECUTOR
# ============================================================================


class OracleExecutor:
    """
    Executes scheduler procedures in Oracle.

    Expected Oracle procedure signature:

        PROCEDURE procedure_name(
            p_repdt IN DATE,
            v_cnt   OUT NUMBER
        );

    Example:

        SCHEDULE_EXTRACTS.GPB
        REPORTING.SCHEDULE_EXTRACTS.GPB

    The executor is responsible only for:

        1. Creating/reusing the Oracle connection.
        2. Validating the procedure name.
        3. Converting report_date.
        4. Calling the Oracle procedure.
        5. Reading the OUT count.
        6. Committing on success.
        7. Rolling back on failure.
        8. Returning ExecutionResult.
    """

    PROCEDURE_PATTERN = re.compile(
        r"^[A-Za-z_][A-Za-z0-9_$#]*"
        r"(?:\.[A-Za-z_][A-Za-z0-9_$#]*){0,2}$"
    )

    def __init__(
        self,
        connection_factory: Optional[Callable] = None,
        *,
        user=None,
        password=None,
        dsn=None,
        host=None,
        port=None,
        service=None,
        sid=None,
        encoding=None,
        nencoding=None,
    ):
        """
        Create an Oracle executor.

        Parameters
        ----------
        connection_factory:
            Optional custom connection function.

            Primarily useful for testing.

        user:
            Oracle username.

        password:
            Oracle password.

        dsn:
            Complete Oracle DSN.

        host:
            Oracle host.

        port:
            Oracle listener port.

        service:
            Oracle service name.

        sid:
            Oracle SID.

        encoding:
            Oracle character encoding.

        nencoding:
            Oracle national character encoding.

        Values not explicitly supplied are read from .env.
        """

        self.connection_factory = connection_factory
        self._driver = None

        # ====================================================================
        # ENVIRONMENT CONFIGURATION
        # ====================================================================

        self.user = (
            user
            if user is not None
            else os.getenv("ORACLE_USER")
        )

        self.password = (
            password
            if password is not None
            else os.getenv("ORACLE_PASSWORD")
        )

        self.host = (
            host
            if host is not None
            else os.getenv("ORACLE_HOST")
        )

        self.port = (
            port
            if port is not None
            else os.getenv(
                "ORACLE_PORT",
                "1521",
            )
        )

        self.service = (
            service
            if service is not None
            else os.getenv("ORACLE_SERVICE")
        )

        self.sid = (
            sid
            if sid is not None
            else os.getenv("ORACLE_SID")
        )

        self.encoding = (
            encoding
            if encoding is not None
            else os.getenv(
                "ORACLE_ENCODING",
                "UTF-8",
            )
        )

        self.nencoding = (
            nencoding
            if nencoding is not None
            else os.getenv(
                "ORACLE_NENCODING",
                "UTF-8",
            )
        )

        # ====================================================================
        # DSN
        # ====================================================================

        self.dsn = (
            dsn
            if dsn is not None
            else os.getenv("ORACLE_DSN")
        )

        if not self.dsn:
            self.dsn = self._build_dsn()

        # ====================================================================
        # CONNECTION
        # ====================================================================

        self.connection = None

    # ========================================================================
    # CONNECTION
    # ========================================================================

    def connect(self):
        """
        Create and return an Oracle connection.

        The connection is lazy and reused.

        If the existing connection is unhealthy, it is closed
        and recreated.
        """

        # --------------------------------------------------------------------
        # Reuse existing connection if healthy.
        # --------------------------------------------------------------------

        if self.connection is not None:

            try:

                if connection_is_healthy(self.connection):

                    return self.connection

            except Exception:

                pass

            self.close()

        # --------------------------------------------------------------------
        # Custom connection factory.
        # --------------------------------------------------------------------

        if self.connection_factory is not None:

            self.connection = (
                self.connection_factory()
            )

            if self.connection is None:

                raise RuntimeError(
                    "Oracle connection factory "
                    "returned None."
                )

            return self.connection

        # --------------------------------------------------------------------
        # Validate configuration.
        # --------------------------------------------------------------------

        self._validate_connection_config()

        # --------------------------------------------------------------------
        # python-oracledb / cx_Oracle connection.
        # --------------------------------------------------------------------

        self._driver = load_oracle_driver()
        self.connection = connect_oracle(
            user=self.user, password=self.password, dsn=self.dsn,
            driver=self._driver, encoding=self.encoding, nencoding=self.nencoding,
        )

        return self.connection

    # ========================================================================
    # TEST CONNECTION
    # ========================================================================

    def test_connection(self):
        """
        Test Oracle connectivity.

        Returns True when:

            SELECT 1 FROM dual

        succeeds.
        """

        connection = self.connect()

        cursor = None

        try:

            cursor = connection.cursor()

            cursor.execute(
                """
                SELECT 1
                FROM dual
                """
            )

            row = cursor.fetchone()

            return (
                row is not None
                and row[0] == 1
            )

        finally:

            if cursor is not None:

                try:
                    cursor.close()
                except Exception:
                    pass

    # ========================================================================
    # EXECUTE
    # ========================================================================

    def execute(
        self,
        procedure_name,
        report_date,
    ):
        """
        Execute one Oracle scheduler procedure.

        Expected procedure signature:

            PROCEDURE_NAME(
                p_repdt IN DATE,
                v_cnt   OUT NUMBER
            )

        Returns
        -------
        ExecutionResult
        """

        started = datetime.now()

        started_at = started.isoformat(
            timespec="seconds"
        )

        # --------------------------------------------------------------------
        # Convert report date.
        # --------------------------------------------------------------------

        try:

            normalized_report_date = (
                self._to_date(
                    report_date
                )
            )

        except Exception as exc:

            finished = datetime.now()

            return ExecutionResult(
                success=False,
                procedure_name=str(
                    procedure_name
                    if procedure_name is not None
                    else ""
                ),
                report_date=None,
                count=None,
                error=str(exc),
                error_type=type(exc).__name__,
                started_at=started_at,
                finished_at=finished.isoformat(
                    timespec="seconds"
                ),
                duration_seconds=(
                    finished - started
                ).total_seconds(),
            )

        try:

            # ----------------------------------------------------------------
            # Validate procedure identifier.
            # ----------------------------------------------------------------

            self._validate_procedure_name(
                procedure_name
            )

            # ----------------------------------------------------------------
            # Connect.
            # ----------------------------------------------------------------

            connection = self.connect()

            cursor = None

            try:

                cursor = connection.cursor()

                # ============================================================
                # OUT COUNT
                # ============================================================

                # Injected DB-API connections need no installed Oracle driver.
                output_count = cursor.var(self._driver.NUMBER if self._driver is not None else float)

                # ============================================================
                # CALL PROCEDURE
                # ============================================================

                cursor.callproc(
                    procedure_name,
                    [
                        normalized_report_date,
                        output_count,
                    ],
                )

                # ============================================================
                # GET COUNT
                # ============================================================

                count_value = (
                    output_count.getvalue()
                )

                count_value = (
                    self._normalize_count(
                        count_value
                    )
                )

                # ============================================================
                # COMMIT
                # ============================================================

                connection.commit()

                # ============================================================
                # SUCCESS
                # ============================================================

                finished = datetime.now()

                return ExecutionResult(
                    success=True,
                    procedure_name=procedure_name,
                    report_date=(
                        normalized_report_date
                    ),
                    count=count_value,
                    error=None,
                    error_type=None,
                    started_at=started_at,
                    finished_at=finished.isoformat(
                        timespec="seconds"
                    ),
                    duration_seconds=(
                        finished - started
                    ).total_seconds(),
                )

            except Exception:

                # ------------------------------------------------------------
                # Oracle transaction rollback.
                # ------------------------------------------------------------

                try:
                    connection.rollback()
                except Exception:
                    pass

                raise

            finally:

                if cursor is not None:

                    try:
                        cursor.close()
                    except Exception:
                        pass

        except Exception as exc:

            finished = datetime.now()

            return ExecutionResult(
                success=False,
                procedure_name=str(
                    procedure_name
                    if procedure_name is not None
                    else ""
                ),
                report_date=(
                    normalized_report_date
                ),
                count=None,
                error=str(exc),
                error_type=type(exc).__name__,
                started_at=started_at,
                finished_at=finished.isoformat(
                    timespec="seconds"
                ),
                duration_seconds=(
                    finished - started
                ).total_seconds(),
            )

    # ========================================================================
    # CLOSE
    # ========================================================================

    def close(self):
        """
        Close the Oracle connection.

        Safe to call multiple times.
        """

        if self.connection is None:

            return

        connection = self.connection

        self.connection = None

        try:

            connection.close()

        except Exception:

            pass

    # ========================================================================
    # BUILD DSN
    # ========================================================================

    def _build_dsn(self):
        """
        Build an Oracle DSN.

        Priority:

            ORACLE_SERVICE
                |
                +---- service-name connection

            ORACLE_SID
                |
                +---- SID-based connection

        For the current .env:

            ORACLE_HOST=localhost
            ORACLE_PORT=1521
            ORACLE_SID=ORCL

        this produces:

            a SID-aware ``DESCRIPTION`` connection descriptor.
        """

        if not self.host:

            return None

        port = self.port or "1521"

        # --------------------------------------------------------------------
        # Service name takes precedence when explicitly configured.
        # --------------------------------------------------------------------

        if self.service:

            return (
                f"{self.host}:"
                f"{port}/"
                f"{self.service}"
            )

        # --------------------------------------------------------------------
        # SID fallback.
        # --------------------------------------------------------------------

        if self.sid:
            # Easy Connect treats the final component as a service name on
            # many installations.  Use an explicit SID descriptor instead.
            return (
                "(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)"
                f"(HOST={self.host})(PORT={port}))"
                f"(CONNECT_DATA=(SID={self.sid})))"
            )

        return None

    # ========================================================================
    # VALIDATE CONNECTION CONFIGURATION
    # ========================================================================

    def _validate_connection_config(self):
        """
        Validate required Oracle configuration.
        """

        missing = []

        if not self.user:

            missing.append(
                "ORACLE_USER"
            )

        if not self.password:

            missing.append(
                "ORACLE_PASSWORD"
            )

        if not self.dsn:

            missing.append(
                "ORACLE_DSN or "
                "ORACLE_HOST + ORACLE_PORT + "
                "(ORACLE_SERVICE or ORACLE_SID)"
            )

        if missing:

            raise ValueError(
                "Missing Oracle configuration: "
                + ", ".join(missing)
            )

    # ========================================================================
    # VALIDATE PROCEDURE NAME
    # ========================================================================

    @classmethod
    def _validate_procedure_name(
        cls,
        procedure_name,
    ):
        """
        Validate an Oracle procedure identifier.

        Accepted:

            PROCEDURE

        or:

            PACKAGE.PROCEDURE

        or:

            SCHEMA.PACKAGE.PROCEDURE
        """

        if not procedure_name:

            raise ValueError(
                "Oracle procedure name is required."
            )

        procedure_name = str(
            procedure_name
        ).strip()

        if not cls.PROCEDURE_PATTERN.fullmatch(
            procedure_name
        ):

            raise ValueError(
                "Invalid Oracle procedure name: "
                f"{procedure_name}"
            )

    # ========================================================================
    # DATE CONVERSION
    # ========================================================================

    @staticmethod
    def _to_date(value):
        """
        Convert supported values into datetime.date.

        Supported:

            datetime
            date

            YYYY-MM-DD
            DD-MM-YYYY
            DD/MM/YYYY
            DD-MON-YYYY
            DD-MON-RR
            DD/MMM/YYYY
            DD/MMM/RR
            DD-MONTH-YYYY
            DD/MONTH/YYYY

            ISO datetime
        """

        if value is None:

            return None

        # --------------------------------------------------------------------
        # datetime
        # --------------------------------------------------------------------

        if isinstance(
            value,
            datetime,
        ):

            return value.date()

        # --------------------------------------------------------------------
        # date
        # --------------------------------------------------------------------

        if isinstance(
            value,
            date,
        ):

            return value

        value = str(
            value
        ).strip()

        if not value:

            return None

        # --------------------------------------------------------------------
        # ISO datetime/date.
        # --------------------------------------------------------------------

        try:

            return parse_iso_datetime(
                value
            ).date()

        except ValueError:

            pass

        # --------------------------------------------------------------------
        # Supported formats.
        # --------------------------------------------------------------------

        formats = (
            "%Y-%m-%d",
            "%d-%m-%Y",
            "%d/%m/%Y",
            "%d-%b-%Y",
            "%d-%b-%y",
            "%d/%b/%Y",
            "%d/%b/%y",
            "%d-%B-%Y",
            "%d-%B-%y",
            "%d/%B/%Y",
            "%d/%B/%y",
        )

        for fmt in formats:

            try:

                return datetime.strptime(
                    value,
                    fmt,
                ).date()

            except ValueError:

                continue

        raise ValueError(
            "Unsupported report date format: "
            f"{value}"
        )

    # ========================================================================
    # COUNT NORMALIZATION
    # ========================================================================

    @staticmethod
    def _normalize_count(value):
        """
        Normalize Oracle NUMBER OUT parameter.

        Examples:

            None
                -> None

            10
                -> 10

            Decimal('10')
                -> 10

            Decimal('10.0')
                -> 10

            Decimal('10.5')
                -> 10.5
        """

        if value is None:

            return None

        # --------------------------------------------------------------------
        # Some test doubles/alternate implementations may return sequences.
        # --------------------------------------------------------------------

        if isinstance(
            value,
            (list, tuple),
        ):

            if not value:

                return None

            value = value[0]

        # --------------------------------------------------------------------
        # Convert numeric value.
        # --------------------------------------------------------------------

        try:

            numeric_value = float(
                value
            )

        except (
            TypeError,
            ValueError,
        ):

            return None

        # --------------------------------------------------------------------
        # Preserve integer counts.
        # --------------------------------------------------------------------

        if numeric_value.is_integer():

            return int(
                numeric_value
            )

        return numeric_value


# ============================================================================
# SIMPLE CONNECTION TEST
# ============================================================================


if __name__ == "__main__":

    print(
        "Oracle Executor"
    )

    print(
        "User:",
        os.getenv("ORACLE_USER")
    )

    # ------------------------------------------------------------------------
    # Build the same DSN logic used by OracleExecutor.
    #
    # Password is intentionally never printed.
    # ------------------------------------------------------------------------

    configured_dsn = (
        os.getenv("ORACLE_DSN")
    )

    if not configured_dsn:

        host = os.getenv(
            "ORACLE_HOST"
        )

        port = os.getenv(
            "ORACLE_PORT",
            "1521",
        )

        service = os.getenv(
            "ORACLE_SERVICE"
        )

        sid = os.getenv(
            "ORACLE_SID"
        )

        if host and service:

            configured_dsn = (
                f"{host}:"
                f"{port}/"
                f"{service}"
            )

        elif host and sid:

            configured_dsn = (
                f"{host}:"
                f"{port}/"
                f"{sid}"
            )

    print(
        "DSN:",
        configured_dsn
    )

    executor = None

    try:

        executor = OracleExecutor()

        if executor.test_connection():

            print(
                "Oracle connection: SUCCESS"
            )

        else:

            print(
                "Oracle connection: FAILED"
            )

    except Exception as exc:

        print(
            "Oracle connection: FAILED"
        )

        print(
            f"{type(exc).__name__}: {exc}"
        )

    finally:

        if executor is not None:

            executor.close()
