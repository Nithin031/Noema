"""Noema-native input-presence collector (Windows-first).

Independent implementation using only the Python standard library
(``ctypes`` against user32/kernel32). Behavioral reference: ActivityWatch
``aw-watcher-afk`` (MPL-2.0, upstream only — no code copied; see
``docs/licensing/activitywatch.md``): idle seconds from the OS last-input
clock, ``timeout``/``poll`` cadence, and ``{status: afk | not-afk}``
presence rows.

Emitted rows satisfy :func:`~noema.domain.presence.detector.is_presence_event`
via ``metadata.event_kind == "presence"`` (plus a native AFK bucket-type
marker), so Noema's AFK veto, session splitting, and classification
exclusion apply unchanged.

Failure contract: an unreadable input clock yields ``error`` with no
events. The collector never reports ACTIVE it did not observe.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from noema.domain.activity import ActivityEvent

from .base import (
    ERROR,
    OK,
    UNAVAILABLE,
    ActivitySource,
    SourcePoll,
    utcnow,
)

SOURCE_NAME = "noema-native-presence"
NATIVE_SOURCE = "noema_native"
NATIVE_BUCKET = "noema-afk"

ACTIVE = "not-afk"
AFK = "afk"


def _windows_idle_seconds() -> float:
    """Seconds since last keyboard/mouse input, via the OS input clock."""
    import ctypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise OSError("input clock unavailable") from exc

    class LastInputInfo(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]

    info = LastInputInfo()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise OSError("GetLastInputInfo failed")
    # GetTickCount64 avoids the 49.7-day DWORD wrap; reconcile the 32-bit
    # stamp against it the same way upstream does.
    now_ms = kernel32.GetTickCount64()
    last_ms = (now_ms & ~0xFFFFFFFF) | info.dwTime
    if last_ms > now_ms:
        last_ms -= 0x100000000
    return max(0.0, (now_ms - last_ms) / 1000.0)


def _device_name() -> str:
    try:
        return socket.gethostname().strip() or "unknown"
    except OSError:
        return "unknown"


class NativeAFKSource(ActivitySource):
    """Input-idle presence collector with transition + heartbeat emission.

    ``idle_seconds_fn`` and ``clock`` are injectable so tests drive the
    collector deterministically; production uses the ctypes input clock.
    """

    name = "noema-native-presence"

    def __init__(
        self,
        timeout_seconds: float = 180.0,
        heartbeat_seconds: float = 5.0,
        idle_seconds_fn: Optional[Callable[[], float]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        device: Optional[str] = None,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self._idle_seconds_fn = idle_seconds_fn
        self._clock = clock or utcnow
        self._device = device or _device_name()
        self._state: Optional[str] = None
        self._slice_start: Optional[datetime] = None
        self._last_emit_at: Optional[datetime] = None
        self._polls = 0
        self._errors = 0
        self._last_error: Optional[str] = None

    @property
    def supported(self) -> bool:
        if self._idle_seconds_fn is not None:
            return True
        if os.name != "nt":
            return False
        try:
            import ctypes

            ctypes.WinDLL("user32", use_last_error=True)
            return True
        except (AttributeError, OSError):
            return False

    def _presence_event(self, status: str, start: datetime,
                        end: datetime) -> Optional[ActivityEvent]:
        """One presence slice, or None when it would be zero-length.

        The store treats presence rows as immutable and the timeline
        skips zero-length intervals, so empty slices are dropped at
        emission instead of stored as dead rows.
        """
        duration = (end - start).total_seconds()
        if duration <= 0:
            return None
        return ActivityEvent(
            timestamp=start,
            duration=duration,
            device=self._device,
            source=NATIVE_SOURCE,
            bucket_id=NATIVE_BUCKET,
            source_event_id="noema-afk-{}".format(
                end.isoformat().replace("+00:00", "Z")),
            metadata={
                "event_kind": "presence",
                "status": status,
                "noema_native_bucket_type": "afkstatus",
                "noema_native_source": "windows-input",
            },
        )

    def _maybe_emit(self, events: list, status: str,
                    start: datetime, end: datetime) -> None:
        event = self._presence_event(status, start, end)
        if event is not None:
            events.append(event)

    def poll(self, now: Optional[datetime] = None) -> SourcePoll:
        moment = now or self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if not self.supported:
            return SourcePoll(source=self.name, status=UNAVAILABLE,
                              detail="input clock not supported here")
        self._polls += 1
        try:
            probe = self._idle_seconds_fn or _windows_idle_seconds
            idle_seconds = max(0.0, float(probe()))
        except Exception as exc:
            # Unknown input state: say so, emit nothing, keep prior state
            # so a transient blip does not fabricate an AFK span.
            self._errors += 1
            self._last_error = "{}: {}".format(type(exc).__name__, exc)
            return SourcePoll(source=self.name, status=ERROR,
                              detail=self._last_error)
        last_input_at = moment - timedelta(seconds=idle_seconds)
        state = AFK if idle_seconds >= self.timeout_seconds else ACTIVE
        self._last_error = None
        if self._state is None:
            # First observation. An already-idle machine yields an honest
            # [last-input, now] AFK slice straight away; a single active
            # instant proves nothing yet, so it waits for the heartbeat.
            self._state = state
            self._slice_start = last_input_at if state == AFK else moment
            self._last_emit_at = moment
            events: list = []
            if state == AFK:
                self._maybe_emit(events, AFK, last_input_at, moment)
                self._slice_start = moment
            return SourcePoll(source=self.name, events=events)
        if state != self._state:
            # Transition: close the prior slice at the last-input boundary
            # (the honest transition instant) and open the new state there,
            # so both sides are represented even if the user flips back
            # before the next heartbeat.
            events = []
            assert self._slice_start is not None
            self._maybe_emit(events, self._state, self._slice_start,
                             last_input_at)
            self._maybe_emit(events, state, last_input_at, moment)
            self._state = state
            self._slice_start = moment
            self._last_emit_at = moment
            return SourcePoll(source=self.name, events=events)
        # Steady state: heartbeat only when due.
        if (self._last_emit_at is not None
                and (moment - self._last_emit_at).total_seconds() < self.heartbeat_seconds):
            return SourcePoll(source=self.name)
        assert self._slice_start is not None
        events = []
        self._maybe_emit(events, self._state, self._slice_start, moment)
        self._slice_start = moment
        self._last_emit_at = moment
        return SourcePoll(source=self.name, events=events)

    def health(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "supported": bool(self.supported),
            "status": OK if self.supported else UNAVAILABLE,
            "timeout_seconds": self.timeout_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "polls": self._polls,
            "errors": self._errors,
            "last_error": self._last_error,
            "state": self._state,
            "slice_start": (
                self._slice_start.isoformat().replace("+00:00", "Z")
                if self._slice_start is not None else None
            ),
        }
