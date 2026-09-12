"""Process-wide single-instance guard for the scheduler worker.

The scheduler persists queue state in SQLite and can execute Oracle procedures.
Two workers evaluating the same master at the same time would therefore be a
production incident, even if individual repositories have duplicate
protection. A named Windows mutex gives Task Scheduler and the long-running
service one shared, crash-safe ownership boundary. A small ``flock`` fallback
keeps local development and CI behaviour equivalent on non-Windows hosts.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class SchedulerInstanceLock:
    """Acquire one cross-process scheduler-worker lease.

    ``acquire`` returns ``False`` when another worker already owns the lease;
    it never waits. This is intentional for Windows Task Scheduler: a later
    trigger should be skipped instead of queueing a second worker behind a
    slow Oracle run.
    """

    _WINDOWS_ALREADY_EXISTS = 183

    def __init__(self, scope: str | Path, name: str | None = None):
        scope_text = str(Path(scope).resolve()).lower()
        digest = hashlib.sha256(scope_text.encode("utf-8")).hexdigest()[:20]
        self.name = name or f"Local\\SBI_Oracle_Scheduler_{digest}"
        self._handle = None
        self._file_handle = None
        self._scope = scope_text
        self.acquired = False

    def acquire(self) -> bool:
        """Attempt to acquire the lease without blocking."""
        if self.acquired:
            return True

        if os.name == "nt":
            # ``CreateMutexW`` is released automatically by Windows when a
            # crashed process exits. Passing initial_owner=False means merely
            # holding the handle is the ownership lifetime we need.
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
            create_mutex.restype = ctypes.c_void_p
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_bool
            handle = create_mutex(None, False, self.name)
            error = ctypes.get_last_error()
            if not handle:
                raise OSError(error, "CreateMutexW failed")
            if error == self._WINDOWS_ALREADY_EXISTS:
                close_handle(handle)
                return False
            self._handle = handle
            self.acquired = True
            return True

        # POSIX is used only for developer/test environments. The lock file
        # itself is harmless and is not treated as a scheduler state file.
        import fcntl

        lock_file = Path(self._scope).with_suffix(".worker.lock")
        self._file_handle = lock_file.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._file_handle.close()
            self._file_handle = None
            return False
        self.acquired = True
        return True

    def release(self) -> None:
        """Release the lease. Safe to call more than once."""
        if self._handle is not None:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (ctypes.c_void_p,)
            close_handle.restype = ctypes.c_bool
            close_handle(self._handle)
            self._handle = None
        if self._file_handle is not None:
            import fcntl

            fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_UN)
            self._file_handle.close()
            self._file_handle = None
        self.acquired = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("A scheduler worker is already running.")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()

