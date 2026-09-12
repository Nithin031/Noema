"""User-presence detection from genuine interaction signals.

Architecture mirrors ActivityWatch's aw-watcher-afk: the detector consumes
*input timing* (keyboard/mouse activity as observed by the OS watcher, plus
explicit browser interaction events) and derives ACTIVE/AFK intervals. Window
or application telemetry is NEVER treated as presence (PART 61): a focused
window with no input still goes AFK after the timeout.

Genuine signals used, in priority order:
1. ``aw-watcher-afk`` status rows (``status: afk | not-afk``). These already
   encode OS-level keyboard/mouse timing, including system lock (reported
   as afk). This is the primary source, historical and live.
2. Explicit browser interaction events (``tab_activated``). Switching tabs
   requires user input, so each activation is input evidence. Heartbeats,
   closed tabs, and stored heartbeat rows are NOT input evidence.
3. System sleep has no rows at all: intervals simply end at the last
   evidence. Nothing is fabricated beyond observed spans.

Conflict rule: AW afk rows observe the full device and always win. A browser
activation timestamp covered by an AFK interval is ignored.

Media exception: no browser producer currently emits playback state, so
``media_exception`` only honors explicit ``audible``/``media_playing`` /
``is_playing`` metadata when enabled, and is inert otherwise. No audio
detector is built and no media signal is invented.

Privacy: only the existence and timestamp of interaction is used. Key codes,
typed text, passwords, and mouse coordinates are never read or stored.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from noema.domain.activity import coerce_timestamp

from .models import PresenceInterval, PresenceState, PresenceTimeline

AFK_BUCKET_TYPES = frozenset({"afkstatus", "afk", "afkwatcher"})
AFK_STATUS_VALUES = frozenset({"afk", "not-afk"})
INTERACTION_EVENT_TYPES = frozenset({"tab_activated"})
MEDIA_METADATA_KEYS = ("audible", "media_playing", "is_playing")


def is_presence_event(event: Any) -> bool:
    """True when a stored event is presence telemetry, not activity.

    Presence rows must never be sessionized as user activity. The adapter
    stamps ``event_kind=presence``; historical rows are recognized from
    generic AFK status metadata so existing databases keep working.
    """
    metadata = getattr(event, "metadata", None)
    if isinstance(metadata, Mapping):
        kind = str(metadata.get("event_kind", "") or "").casefold()
        if kind == "presence":
            return True
        bucket_type = str(metadata.get("activitywatch_bucket_type", "") or "").casefold()
        if bucket_type in AFK_BUCKET_TYPES:
            return True
        status = str(metadata.get("status", "") or "").casefold()
        if status in AFK_STATUS_VALUES and not getattr(event, "app", None) and not getattr(event, "title", None):
            return True
    bucket_id = str(getattr(event, "bucket_id", "") or "")
    if bucket_id.startswith("aw-watcher-afk"):
        return True
    return False


def afk_status(event: Any) -> Optional[str]:
    """Return 'afk' | 'not-afk' for AFK-watcher rows, else None."""
    if not is_presence_event(event):
        return None
    metadata = getattr(event, "metadata", None)
    if isinstance(metadata, Mapping):
        status = str(metadata.get("status", "") or "").casefold()
        if status in AFK_STATUS_VALUES:
            return status
    return None


class PresenceDetector:
    """Build ACTIVE/AFK timelines from interaction signals."""

    def __init__(
        self,
        timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 5.0,
        media_exception: bool = False,
    ):
        if timeout_seconds <= 0:
            raise ValueError("afk timeout must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("afk poll interval must be positive")
        self.timeout_seconds = float(timeout_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.media_exception = bool(media_exception)

    def _activation_at(self, event: Any) -> Optional[datetime]:
        """Input timestamp for explicit browser interactions, else None."""
        event_type = str(getattr(event, "event_type", "") or "").strip().casefold()
        if event_type not in INTERACTION_EVENT_TYPES:
            return None
        timestamp = getattr(event, "timestamp", None)
        try:
            return coerce_timestamp(timestamp)
        except (AttributeError, TypeError, ValueError):
            return None

    def _media_active(self, event: Any) -> bool:
        if not self.media_exception:
            return False
        metadata = getattr(event, "metadata", None)
        if not isinstance(metadata, Mapping):
            return False
        return any(bool(metadata.get(key)) for key in MEDIA_METADATA_KEYS)

    def extract_afk_intervals(
        self,
        events: Iterable[Any],
        start: Optional[Any] = None,
        end: Optional[Any] = None,
    ) -> List[PresenceInterval]:
        """Turn stored afk-status rows into ACTIVE/AFK intervals."""
        window_start = coerce_timestamp(start) if start is not None else None
        window_end = coerce_timestamp(end) if end is not None else None
        intervals: List[PresenceInterval] = []
        for event in events or []:
            status = afk_status(event)
            if status is None:
                continue
            event_start = coerce_timestamp(event.timestamp)
            event_end = coerce_timestamp(event.end_timestamp)
            if window_start is not None:
                event_start = max(event_start, window_start)
            if window_end is not None:
                event_end = min(event_end, window_end)
            if event_end <= event_start:
                continue
            intervals.append(PresenceInterval(
                event_start, event_end,
                PresenceState.ACTIVE if status == "not-afk" else PresenceState.AFK,
                "aw-afk",
            ))
        intervals.sort(key=lambda item: (item.start, item.end))
        return intervals

    def extract_input_times(
        self,
        browser_events: Optional[Iterable[Any]] = None,
    ) -> List[datetime]:
        """Timestamps of genuine user interactions (no keystrokes stored)."""
        moments: List[datetime] = []
        for event in browser_events or []:
            moment = self._activation_at(event)
            if moment is not None:
                moments.append(moment)
                continue
            if self._media_active(event):
                try:
                    moments.append(coerce_timestamp(event.timestamp))
                except (AttributeError, TypeError, ValueError):
                    continue
        return sorted(set(moments))

    def _afk_covering(self, intervals: List[PresenceInterval], moment: datetime) -> bool:
        for interval in intervals:
            if interval.state == PresenceState.AFK and interval.start <= moment < interval.end:
                return True
            if interval.start > moment:
                break
        return False

    def build_timeline(
        self,
        afk_events: Optional[Iterable[Any]] = None,
        browser_events: Optional[Iterable[Any]] = None,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        now: Optional[Any] = None,
    ) -> PresenceTimeline:
        """Combine afk rows and interaction timestamps into one timeline.

        - afk rows define ACTIVE/AFK spans (device-wide truth).
        - Each browser activation extends ACTIVE for up to ``timeout_seconds``
          unless an afk interval contradicts it.
        - Regions without any evidence stay UNKNOWN; the detector never
          invents presence. System sleep appears as a data gap: intervals
          end at the last evidence and nothing is claimed beyond it.
        """
        current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
        window_start = coerce_timestamp(start) if start is not None else None
        window_end = coerce_timestamp(end) if end is not None else current
        base = self.extract_afk_intervals(afk_events, window_start, window_end)
        inputs = self.extract_input_times(browser_events)
        if window_start is not None:
            inputs = [moment for moment in inputs if moment >= window_start]
        inputs = [moment for moment in inputs if moment <= window_end]

        spans: List[PresenceInterval] = list(base)
        for moment in inputs:
            if self._afk_covering(base, moment):
                continue  # Device truth wins over a lone browser signal.
            horizon = min(moment.timestamp() + self.timeout_seconds, window_end.timestamp())
            horizon_dt = datetime.fromtimestamp(horizon, tz=timezone.utc)
            if horizon_dt > moment:
                spans.append(PresenceInterval(moment, horizon_dt, PresenceState.ACTIVE, "browser-input"))

        last_input: Optional[datetime] = None
        candidates: List[datetime] = list(inputs)
        for interval in base:
            if interval.state == PresenceState.AFK:
                # An afk onset at T means the device saw input last around
                # T - timeout: the standard watcher derivation, used only
                # for the last-input readout, never to invent ACTIVE spans.
                derived = interval.start.timestamp() - self.timeout_seconds
                candidates.append(datetime.fromtimestamp(derived, tz=timezone.utc))
        for candidate in candidates:
            if candidate <= current and (last_input is None or candidate > last_input):
                last_input = candidate
        return PresenceTimeline(
            intervals=spans,
            last_input_at=last_input,
            window_start=window_start,
            window_end=window_end,
        )

    def current_state(
        self,
        afk_events: Optional[Iterable[Any]] = None,
        browser_events: Optional[Iterable[Any]] = None,
        bridge_last_seen_at: Optional[Any] = None,
        now: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Presence snapshot for dashboards and health: state + idle age."""
        current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
        lookback = current.timestamp() - 2 * self.timeout_seconds
        timeline = self.build_timeline(
            afk_events, browser_events,
            start=datetime.fromtimestamp(max(0.0, lookback), tz=timezone.utc),
            end=current, now=current,
        )
        state = timeline.state_at(current)
        bridge_seen: Optional[datetime] = None
        if bridge_last_seen_at is not None:
            try:
                bridge_seen = coerce_timestamp(bridge_last_seen_at)
            except (AttributeError, TypeError, ValueError):
                bridge_seen = None
        last_input = timeline.last_input_at
        if bridge_seen is not None and bridge_seen <= current and (
                last_input is None or bridge_seen > last_input):
            # A live extension report is recent interaction evidence. It can
            # only ever report ACTIVE-ish recency, never AFK.
            last_input = bridge_seen
            if state == PresenceState.UNKNOWN:
                state = PresenceState.ACTIVE
        idle_seconds: Optional[float] = None
        if last_input is not None:
            idle_seconds = round(max(0.0, (current - last_input).total_seconds()), 1)
        stale = state == PresenceState.UNKNOWN
        return {
            "state": state.value,
            "last_input_at": last_input.isoformat().replace("+00:00", "Z") if last_input else None,
            "idle_seconds": idle_seconds,
            "as_of": current.isoformat().replace("+00:00", "Z"),
            "stale": stale,
        }
