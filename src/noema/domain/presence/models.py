"""Presence data contracts: user interaction state over time.

Presence (was the user interacting?) is deliberately separate from activity
(window/application context) and from semantic classification (productive /
distractive / neutral). AFK is an operational state, never a category.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple

from noema.domain.activity import coerce_timestamp


class PresenceState(str, Enum):
    ACTIVE = "active"
    AFK = "afk"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PresenceInterval:
    """One contiguous span of known user-presence state."""

    start: datetime
    end: datetime
    state: PresenceState
    source: str = "aw-afk"

    def __post_init__(self) -> None:
        start = coerce_timestamp(self.start)
        end = coerce_timestamp(self.end)
        if end < start:
            raise ValueError("presence interval end cannot be before start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "state", PresenceState(self.state))
        object.__setattr__(self, "source", str(self.source or "aw-afk"))

    @property
    def duration(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())

    def overlap(self, start: datetime, end: datetime) -> float:
        """Seconds of this interval inside [start, end)."""
        left = max(self.start, start)
        right = min(self.end, end)
        return max(0.0, (right - left).total_seconds())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "start": self.start.isoformat().replace("+00:00", "Z"),
            "end": self.end.isoformat().replace("+00:00", "Z"),
            "duration_seconds": round(self.duration, 3),
            "state": self.state.value,
            "source": self.source,
        }


@dataclass
class PresenceTimeline:
    """Outcome of presence detection over one bounded window.

    Intervals never overlap. Regions with no evidence keep no interval and
    read as UNKNOWN: the detector never fabricates presence.
    """

    intervals: List[PresenceInterval] = field(default_factory=list)
    last_input_at: Optional[datetime] = None
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None

    def __post_init__(self) -> None:
        ordered = sorted(self.intervals, key=lambda item: (item.start, item.end))
        merged: List[PresenceInterval] = []
        for interval in ordered:
            if interval.duration <= 0:
                continue
            if (
                merged
                and merged[-1].state == interval.state
                and interval.start <= merged[-1].end
            ):
                previous = merged[-1]
                merged[-1] = PresenceInterval(
                    previous.start, max(previous.end, interval.end),
                    previous.state, previous.source,
                )
            else:
                merged.append(interval)
        self.intervals = merged
        if self.last_input_at is not None:
            self.last_input_at = coerce_timestamp(self.last_input_at)
        if self.window_start is not None:
            self.window_start = coerce_timestamp(self.window_start)
        if self.window_end is not None:
            self.window_end = coerce_timestamp(self.window_end)

    def state_at(self, moment: Any) -> PresenceState:
        """Presence at one instant; UNKNOWN outside covered regions."""
        needle = coerce_timestamp(moment)
        for interval in self.intervals:
            if interval.start <= needle < interval.end:
                return interval.state
            if interval.start > needle:
                break
        return PresenceState.UNKNOWN

    def _window_seconds(self, state: PresenceState, start: Any, end: Any) -> float:
        left = coerce_timestamp(start)
        right = coerce_timestamp(end)
        if right <= left:
            return 0.0
        return round(sum(item.overlap(left, right) for item in self.intervals if item.state == state), 3)

    def active_seconds(self, start: Any, end: Any) -> float:
        return self._window_seconds(PresenceState.ACTIVE, start, end)

    def afk_seconds(self, start: Any, end: Any) -> float:
        return self._window_seconds(PresenceState.AFK, start, end)

    def transitions(self) -> List[Tuple[str, str]]:
        """Deduped (iso_timestamp, new_state) changes, chronological."""
        changes: List[Tuple[str, str]] = []
        previous: Optional[str] = None
        for interval in self.intervals:
            current = interval.state.value
            if current != previous:
                changes.append((interval.start.isoformat().replace("+00:00", "Z"), current))
                previous = current
        return changes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intervals": [item.to_dict() for item in self.intervals],
            "last_input_at": self.last_input_at.isoformat().replace("+00:00", "Z") if self.last_input_at else None,
            "window_start": self.window_start.isoformat().replace("+00:00", "Z") if self.window_start else None,
            "window_end": self.window_end.isoformat().replace("+00:00", "Z") if self.window_end else None,
        }
