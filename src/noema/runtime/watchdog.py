"""Small process supervisor used by the Windows startup task."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from typing import Iterable, Optional

from .logging_utils import configure_logging, log_event


def run_watchdog(
    daemon_arguments: Iterable[str],
    *,
    restart_delay_seconds: float = 5.0,
    max_restart_delay_seconds: float = 60.0,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Keep one daemon child running until the supervisor is stopped.

    The child owns the single-instance lock and all application state. A
    child crash therefore goes through the normal lock stale-PID recovery on
    restart, while a deliberate supervisor shutdown remains a clean exit.
    """

    if restart_delay_seconds < 0 or max_restart_delay_seconds < restart_delay_seconds:
        raise ValueError("invalid watchdog restart delay")

    logger = logger or configure_logging(os.environ.get(
        "NOEMA_LOG_LEVEL", os.environ.get("AI_ACTIVITY_OS_LOG_LEVEL", "INFO")))
    stop_event = threading.Event()
    child: Optional[subprocess.Popen] = None

    def _stop(signum: int, frame: object) -> None:
        log_event(logger, logging.INFO, "watchdog_stop_signal", signal=signum)
        stop_event.set()
        if child is not None and child.poll() is None:
            try:
                child.terminate()
            except OSError:
                pass

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, name, None)
            if signum is not None:
                try:
                    signal.signal(signum, _stop)
                except (ValueError, OSError):
                    pass

    delay = float(restart_delay_seconds)
    arguments = [str(argument) for argument in daemon_arguments]
    while not stop_event.is_set():
        try:
            child = subprocess.Popen([sys.executable, *arguments])
        except OSError:
            log_event(logger, logging.ERROR, "watchdog_spawn_failed", exc_info=True)
            if stop_event.wait(delay):
                return 0
            delay = min(max_restart_delay_seconds, max(delay * 2.0, restart_delay_seconds))
            continue

        delay = float(restart_delay_seconds)
        log_event(logger, logging.INFO, "watchdog_child_started", pid=child.pid)
        while child.poll() is None and not stop_event.wait(1.0):
            pass

        if stop_event.is_set():
            if child.poll() is None:
                try:
                    child.terminate()
                except OSError:
                    pass
            try:
                child.wait(timeout=10.0)
            except (subprocess.TimeoutExpired, OSError):
                pass
            return 0

        return_code = child.returncode
        log_event(logger, logging.ERROR, "watchdog_child_exited", return_code=return_code)
        if stop_event.wait(delay):
            return 0
        delay = min(max_restart_delay_seconds, max(delay * 2.0, restart_delay_seconds))

    return 0
