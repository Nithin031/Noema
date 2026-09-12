"""Crash-recoverable single-instance lock for the local daemon."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class AlreadyRunningError(RuntimeError):
    """Raised when another live daemon owns the lock file."""


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except ImportError:
        # psutil is available in the repository's development dependencies,
        # but the runtime keeps a stdlib/Win32 fallback for lean installs.
        pass
    if os.name == "nt":
        # os.kill(pid, 0) is not a reliable liveness probe on Windows and
        # can block while the CRT resolves the process handle. Ask kernel32
        # directly instead, with the narrowest query permission required.
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)
            ]
            kernel32.GetExitCodeProcess.restype = ctypes.c_int
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            process = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not process:
                error = ctypes.get_last_error()
                return error == 5  # access denied means the process is live
            exit_code = ctypes.c_ulong()
            try:
                if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(process)
        except (AttributeError, OSError, ValueError):
            # If the platform API is unavailable, fail safe and keep the
            # existing lock rather than risk two daemons writing together.
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A process that cannot be inspected is safer to treat as live.
        return True
    except OSError:
        return False
    return True


class SingleInstanceLock:
    """Own one exact lock path using atomic file creation.

    The PID makes a lock left behind by a crashed process recoverable.  The
    lock is deliberately a plain file so it works on Windows and Unix without
    third-party packages.
    """

    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self._owned = False

    @property
    def owned(self) -> bool:
        return self._owned

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(str(os.getpid()))
                    handle.write("\n")
                self._owned = True
                return
            except FileExistsError:
                owner = self._read_owner()
                if owner is not None and _pid_is_running(owner):
                    raise AlreadyRunningError(
                        "Noema daemon is already running (pid {})".format(owner)
                    )
                if attempt == 0:
                    # Only remove the exact configured lock file, and only
                    # after its owner is confirmed stale or unreadable.
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise AlreadyRunningError("could not recover daemon lock: {}".format(self.path))

    def _read_owner(self) -> Optional[int]:
        try:
            value = self.path.read_text(encoding="utf-8").strip().splitlines()[0]
            return int(value)
        except (FileNotFoundError, IndexError, ValueError, OSError):
            return None

    def release(self) -> None:
        if not self._owned:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self._owned = False

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
