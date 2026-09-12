"""Browser telemetry source status for the native collector set.

Browser URL/tab semantics stay push-based through the Noema Firefox
extension (``web/extension/firefox`` → ``POST /api/browser/event`` →
``NoemaService.report_browser_event``), which already persists
``source="firefox_extension"`` activity rows. There is nothing to poll:
this source therefore yields no events and instead reports whether the
extension bridge is currently delivering, so daemon health and the
``noema telemetry`` diagnostic show an explicit browser state instead of
silence.

OS-level browser foreground activity (browser app + window title while a
browser is focused) is covered by
:class:`~noema.infrastructure.activity_sources.native_windows.NativeWindowsActivitySource`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from .base import (
    DEGRADED,
    OK,
    UNAVAILABLE,
    ActivitySource,
    SourcePoll,
    utcnow,
)

SOURCE_NAME = "noema-browser-bridge"
STALE_AFTER_SECONDS = 120.0


class BrowserActivitySource(ActivitySource):
    """Push-based browser telemetry: health only, no polled events."""

    name = "noema-browser-bridge"

    def __init__(
        self,
        bridge_snapshot_fn: Optional[Callable[[], Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        stale_after_seconds: float = STALE_AFTER_SECONDS,
    ):
        self._bridge_snapshot_fn = bridge_snapshot_fn
        self._clock = clock or utcnow
        self.stale_after_seconds = float(stale_after_seconds)
        self._polls = 0

    @property
    def supported(self) -> bool:
        return self._bridge_snapshot_fn is not None

    def _snapshot(self) -> Dict[str, Any]:
        if self._bridge_snapshot_fn is None:
            return {}
        try:
            snapshot = self._bridge_snapshot_fn()
        except Exception:
            return {}
        if isinstance(snapshot, dict):
            return snapshot
        to_dict = getattr(snapshot, "to_dict", None)
        if callable(to_dict):
            try:
                value = to_dict()
                return value if isinstance(value, dict) else {}
            except Exception:
                return {}
        return {}

    def poll(self, now: Optional[datetime] = None) -> SourcePoll:
        moment = now or self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._polls += 1
        if not self.supported:
            return SourcePoll(
                source=self.name, status=UNAVAILABLE,
                detail="no browser bridge attached (extension reports via POST /api/browser/event)")
        snapshot = self._snapshot()
        registrations = snapshot.get("registrations", [])
        if not registrations:
            return SourcePoll(
                source=self.name, status=DEGRADED,
                detail="bridge attached but no extension registered")
        return SourcePoll(source=self.name, status=OK,
                          detail="{} extension(s) reporting".format(len(registrations)))

    def health(self) -> Dict[str, Any]:
        snapshot = self._snapshot() if self.supported else {}
        registrations = snapshot.get("registrations", []) if snapshot else []
        if not self.supported:
            status = UNAVAILABLE
        elif not registrations:
            status = DEGRADED
        else:
            status = OK
        return {
            "name": self.name,
            "supported": bool(self.supported),
            "status": status,
            "registrations": len(registrations),
            "polls": self._polls,
        }
