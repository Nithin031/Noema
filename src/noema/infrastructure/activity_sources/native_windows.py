"""Noema-native foreground-window collector (Windows-first).

Independent implementation using only the Python standard library
(``ctypes`` against user32/kernel32). Behavioral reference for the Win32
approach and heartbeat semantics: ActivityWatch ``aw-watcher-window``
(MPL-2.0, upstream only — no code copied; see
``docs/licensing/activitywatch.md``).

Heartbeat semantics: the collector keeps the current foreground state in
memory. A poll emits an event only when the state changed (closing the
previous span) or when ``heartbeat_seconds`` elapsed since the last
emission for an unchanged state (extending the span with the same stable
``source_event_id`` so the store treats it as a duration update, not a
duplicate). Unchanged states between heartbeats emit nothing, so an idle
desktop costs ~one row per heartbeat interval, not one per poll.

A poll that cannot observe (lock screen, UAC secure desktop, API failure)
closes the open span and yields ``unavailable``/``error`` — it never
invents a foreground window.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from noema.domain.activity import ActivityEvent

from .base import (
    DEGRADED,
    ERROR,
    OK,
    UNAVAILABLE,
    ActivitySource,
    SourcePoll,
    utcnow,
)

SOURCE_NAME = "noema-native-window"
NATIVE_SOURCE = "noema_native"
NATIVE_BUCKET = "noema-window"

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_TITLE_CHARS = 512


def _windows_foreground_state() -> Optional[Tuple[str, str]]:
    """Return ``(app, title)`` for the foreground window, or None.

    None means "cannot observe" (no foreground window, secure desktop,
    or API failure) — never "no activity".
    """
    import ctypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):
        return None
    try:
        hwnd = user32.GetForegroundWindow()
    except (AttributeError, OSError):
        return None
    if not hwnd:
        return None
    try:
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        title = buffer.value or "unknown"
    except (AttributeError, OSError):
        title = "unknown"
    app = _windows_process_name(user32, kernel32, hwnd)
    if app is None:
        return None
    return app, title


def _windows_process_name(user32: Any, kernel32: Any, hwnd: int) -> Optional[str]:
    """Executable basename owning ``hwnd``; None when unresolvable."""
    import ctypes

    try:
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return None
        try:
            size = ctypes.c_ulong(260)
            buffer = ctypes.create_unicode_buffer(size.value)
            ok = kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size))
            if ok:
                path = buffer.value
            else:
                path = _module_filename_fallback(pid.value)
            if not path:
                return None
            name = path.replace("/", "\\").rsplit("\\", 1)[-1].strip()
            return name or None
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return None


def _module_filename_fallback(pid: int) -> Optional[str]:
    """PSAPI fallback when the query-limited path fails."""
    import ctypes

    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(260)
            if psapi.GetModuleFileNameExW(handle, None, buffer, len(buffer)):
                return buffer.value or None
            return None
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return None


def _device_name() -> str:
    try:
        return socket.gethostname().strip() or "unknown"
    except OSError:
        return "unknown"


class NativeWindowsActivitySource(ActivitySource):
    """Poll-based foreground-window collector with heartbeat emission.

    ``foreground_fn`` and ``clock`` are injectable so tests drive the
    collector deterministically; production uses the ctypes probe above.
    """

    name = "noema-native-window"

    def __init__(
        self,
        heartbeat_seconds: float = 5.0,
        foreground_fn: Optional[Callable[[], Optional[Tuple[str, str]]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        device: Optional[str] = None,
    ):
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self.heartbeat_seconds = float(heartbeat_seconds)
        self._foreground_fn = foreground_fn
        self._clock = clock or utcnow
        self._device = device or _device_name()
        self._span_app: Optional[str] = None
        self._span_title: Optional[str] = None
        self._span_start: Optional[datetime] = None
        self._last_emit_at: Optional[datetime] = None
        self._polls = 0
        self._errors = 0
        self._last_error: Optional[str] = None

    @property
    def supported(self) -> bool:
        if self._foreground_fn is not None:
            return True
        if os.name != "nt":
            return False
        try:
            import ctypes

            ctypes.WinDLL("user32", use_last_error=True)
            return True
        except (AttributeError, OSError):
            return False

    def _observe(self) -> Optional[Tuple[str, str]]:
        probe = self._foreground_fn or _windows_foreground_state
        return probe()

    def _span_event(self, app: str, title: str, start: datetime,
                    end: datetime) -> ActivityEvent:
        duration = max(0.0, (end - start).total_seconds())
        span_id = "noema-window-{}".format(
            start.isoformat().replace("+00:00", "Z"))
        return ActivityEvent(
            timestamp=start,
            duration=duration,
            device=self._device,
            app=app,
            title=title,
            source=NATIVE_SOURCE,
            bucket_id=NATIVE_BUCKET,
            source_event_id=span_id,
            metadata={
                "event_kind": "window",
                "noema_native_bucket_type": "window",
                "noema_native_source": "windows-foreground",
            },
        )

    def poll(self, now: Optional[datetime] = None) -> SourcePoll:
        moment = now or self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if not self.supported:
            return SourcePoll(source=self.name, status=UNAVAILABLE,
                              detail="foreground collection not supported here")
        self._polls += 1
        try:
            observed = self._observe()
        except Exception as exc:
            self._errors += 1
            self._last_error = "{}: {}".format(type(exc).__name__, exc)
            return SourcePoll(source=self.name, status=ERROR,
                              detail=self._last_error)
        if observed is None:
            # Cannot observe (lock screen, secure desktop): close the open
            # span honestly and report degraded, never invent a window.
            events = self._close_span(moment)
            self._last_error = None
            return SourcePoll(source=self.name, events=events,
                              status=DEGRADED if events else OK,
                              detail="no foreground window observable" if not events else "")
        app, title = observed
        if (app, title) != (self._span_app, self._span_title):
            events = self._close_span(moment)
            self._span_app, self._span_title = app, title
            self._span_start = moment
            self._last_emit_at = moment
            events.append(self._span_event(app, title, moment, moment))
            return SourcePoll(source=self.name, events=events)
        # Unchanged state: heartbeat only when due, so a static desktop
        # costs one row per heartbeat interval instead of one per poll.
        if (self._last_emit_at is not None
                and (moment - self._last_emit_at).total_seconds() < self.heartbeat_seconds):
            return SourcePoll(source=self.name)
        assert self._span_start is not None  # span open whenever app/title set
        self._last_emit_at = moment
        return SourcePoll(
            source=self.name,
            events=[self._span_event(app, title, self._span_start, moment)])

    def _close_span(self, moment: datetime) -> List[ActivityEvent]:
        if self._span_app is None or self._span_start is None:
            return []
        event = self._span_event(
            self._span_app, self._span_title or "unknown",
            self._span_start, moment)
        self._span_app = None
        self._span_title = None
        self._span_start = None
        self._last_emit_at = None
        return [event]

    def health(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "supported": bool(self.supported),
            "status": OK if self.supported else UNAVAILABLE,
            "heartbeat_seconds": self.heartbeat_seconds,
            "polls": self._polls,
            "errors": self._errors,
            "last_error": self._last_error,
            "open_span": (
                {"app": self._span_app, "title": self._span_title,
                 "since": self._span_start.isoformat().replace("+00:00", "Z")}
                if self._span_app is not None and self._span_start is not None
                else None
            ),
        }
