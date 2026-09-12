"""Lazy, shared Oracle driver selection for repositories and execution.

Only an absent optional module triggers AUTO fallback. An installed driver's
load, client initialization, authentication or connection failure is final.
"""
from __future__ import annotations

import importlib
import os
import threading
from pathlib import Path


ENGINE_ROOT = Path(__file__).resolve().parent.parent
_CLIENT_LOCK = threading.Lock()
_INITIALIZED_CLIENTS = {}
_DRIVER_MODULES = {"oracledb": "oracledb", "cx_oracle": "cx_Oracle"}


class OracleDriverError(RuntimeError):
    """Safe configuration/installation failure without connection secrets."""


def configured_driver_name():
    value = os.getenv("ORACLE_DRIVER", "auto").strip().lower() or "auto"
    if value not in {*_DRIVER_MODULES, "auto"}:
        raise OracleDriverError("ORACLE_DRIVER must be oracledb, cx_oracle, or auto.")
    return value


def _client_library_directory():
    value = os.getenv("ORACLE_CLIENT_LIB_DIR", "").strip()
    if not value:
        return None
    directory = Path(os.path.expandvars(os.path.expanduser(value)))
    if not directory.is_absolute():
        directory = ENGINE_ROOT / directory
    directory = directory.resolve()
    if not directory.is_dir():
        raise OracleDriverError("ORACLE_CLIENT_LIB_DIR must identify an existing Oracle Client directory.")
    return str(directory)


def _initialize_client(driver):
    library_directory = _client_library_directory()
    if library_directory is None:
        # python-oracledb defaults to Thin mode. cx_Oracle discovers an
        # installed Oracle Client using its ordinary operating-system search.
        return
    with _CLIENT_LOCK:
        previous = _INITIALIZED_CLIENTS.get(driver)
        if previous is not None:
            if previous != library_directory:
                raise OracleDriverError("Oracle Client configuration changed; restart the scheduler to use the new directory.")
            return
        initializer = getattr(driver, "init_oracle_client", None)
        if not callable(initializer):
            raise OracleDriverError("The selected Oracle driver cannot initialize an explicit Oracle Client directory.")
        try:
            initializer(lib_dir=library_directory)
        except Exception:
            raise OracleDriverError(
                "Oracle Client initialization failed. Check the client installation, Python/client architecture, "
                "and ORACLE_CLIENT_LIB_DIR; restart after changing the driver or client mode."
            ) from None
        _INITIALIZED_CLIENTS[driver] = library_directory


def load_oracle_driver():
    """Read current environment only when an Oracle capability is requested."""
    selection = configured_driver_name()
    candidates = ("oracledb", "cx_oracle") if selection == "auto" else (selection,)
    for candidate in candidates:
        module_name = _DRIVER_MODULES[candidate]
        try:
            driver = importlib.import_module(module_name)
        except ModuleNotFoundError as error:
            if error.name != module_name:
                raise OracleDriverError(
                    f"The selected {module_name} installation has a missing dependency; repair that driver installation."
                ) from None
            if selection != "auto":
                raise OracleDriverError(
                    f"ORACLE_DRIVER selects {module_name}, but it is not installed in this Python environment."
                ) from None
            continue
        except Exception:
            raise OracleDriverError(
                f"The installed {module_name} driver could not load; check its installation and Python architecture."
            ) from None
        _initialize_client(driver)
        return driver
    raise OracleDriverError("No Oracle driver is installed. Install the oracledb or cx_Oracle requirements profile.")


def connect_oracle(*, user, password, dsn, driver=None, encoding=None, nencoding=None):
    """Connect once with the selected driver; never fall back on an error."""
    driver = load_oracle_driver() if driver is None else driver
    kwargs = {"user": user, "password": password, "dsn": dsn}
    if getattr(driver, "__name__", "").lower() == "cx_oracle":
        kwargs.update(
            encoding=encoding or os.getenv("ORACLE_ENCODING", "").strip() or "UTF-8",
            nencoding=nencoding or os.getenv("ORACLE_NENCODING", "").strip() or "UTF-8",
            threaded=True,
        )
    return driver.connect(**kwargs)


def connection_is_healthy(connection):
    """Reuse python-oracledb and legacy cx_Oracle connections safely."""
    try:
        checker = getattr(connection, "is_healthy", None)
        if callable(checker):
            return bool(checker())
        ping = getattr(connection, "ping", None)
        if callable(ping):
            ping()
            return True
    except Exception:
        return False
    return False
