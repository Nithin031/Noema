"""The bounded evidence interval a classification verdict describes.

A classification is never an eternal property of a row. It is a claim about
what happened inside one bounded window of observed evidence. This module
owns that window: its temporal bounds, how much of it was actually observed,
and a digest of the exact observations the classifier saw.

The digest matters as much as the bounds. Two windows can share a start and
end while containing completely different evidence (an episode rebuilt after
new telemetry arrived for the same interval). Comparing digests is how Noema
tells "the same claim" from "a different claim with the same clock reading".
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from noema.domain.activity import coerce_timestamp


#: Fields of one observation that participate in evidence identity. Duration
#: is deliberately excluded: a heartbeat growing from 20s to 25s is the same
#: observation, not new evidence. Identity comes from *what* was observed.
_OBSERVATION_IDENTITY_FIELDS = (
    "application", "app", "title", "window_title", "domain", "url",
)


def observation_digest(observations: Iterable[Mapping[str, Any]]) -> str:
    """Stable digest over the semantic identity of a set of observations.

    Order-independent: the same observations seen in a different order are
    the same evidence. Duration/counts are excluded so that a growing
    heartbeat over unchanged content does not read as new evidence.
    """

    fingerprints = set()
    for item in observations or ():
        if not isinstance(item, Mapping):
            continue
        parts = []
        for field in _OBSERVATION_IDENTITY_FIELDS:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                parts.append("{}={}".format(field, value.strip().casefold()))
        if parts:
            fingerprints.add("|".join(sorted(parts)))
    if not fingerprints:
        return "empty"
    joined = "\n".join(sorted(fingerprints))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class EvidenceWindow:
    """The bounded evidence one verdict is a claim about.

    ``start``/``end`` are the temporal scope. ``observation_count`` and
    ``active_seconds`` describe how much was actually seen inside it, and
    ``digest`` identifies the specific observations. Nothing here is
    model-supplied: the observation layer measures all of it.
    """

    start: datetime
    end: datetime
    observation_count: int = 0
    active_seconds: float = 0.0
    digest: str = "empty"

    def __post_init__(self) -> None:
        start = coerce_timestamp(self.start)
        end = coerce_timestamp(self.end)
        if end < start:
            raise ValueError("evidence window end cannot be before its start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        try:
            count = int(self.observation_count or 0)
        except (TypeError, ValueError):
            count = 0
        object.__setattr__(self, "observation_count", max(0, count))
        try:
            active = float(self.active_seconds or 0.0)
        except (TypeError, ValueError):
            active = 0.0
        object.__setattr__(self, "active_seconds", round(max(0.0, active), 3))
        object.__setattr__(self, "digest", str(self.digest or "empty").strip() or "empty")

    @property
    def duration(self) -> float:
        return (self.end - self.start).total_seconds()

    def covers(self, moment: Any, tolerance_seconds: float = 0.0) -> bool:
        """True when ``moment`` falls inside this window.

        ``tolerance_seconds`` absorbs sub-second boundary jitter between the
        sessionizer and the episode builder. It is never large enough to
        stretch a verdict over evidence it did not see.
        """

        stamp = coerce_timestamp(moment)
        slack = timedelta(seconds=max(0.0, float(tolerance_seconds)))
        return (self.start - slack) <= stamp <= (self.end + slack)

    def covers_interval(
        self,
        start: Any,
        end: Any,
        tolerance_seconds: float = 0.0,
    ) -> bool:
        """True when this window substantively contains ``[start, end]``.

        Endpoint containment alone is not enough. ``tolerance_seconds``
        exists to absorb sub-second jitter between the sessionizer and the
        episode builder — it must never let a verdict reach *content* it
        never observed. Two extra conditions enforce that:

        * A zero-length interval (a point query) is tested half-open,
          ``[start, end)``. The instant an episode ends belongs to whatever
          comes next, so an event starting exactly where a verdict's
          evidence stopped does not inherit it.
        * A real interval must have the majority of its duration inside the
          window. A row that merely brushes the boundary is not described
          by this verdict.
        """

        interval_start = coerce_timestamp(start)
        interval_end = coerce_timestamp(end)
        if not (self.covers(interval_start, tolerance_seconds)
                and self.covers(interval_end, tolerance_seconds)):
            return False
        span = (interval_end - interval_start).total_seconds()
        if span <= 0:
            slack = timedelta(seconds=max(0.0, float(tolerance_seconds)))
            return (self.start - slack) <= interval_start < self.end
        return self.overlap_seconds(interval_start, interval_end) >= (span / 2.0)

    def overlaps(self, start: Any, end: Any) -> bool:
        """True when this window shares any time with ``[start, end]``."""

        other_start = coerce_timestamp(start)
        other_end = coerce_timestamp(end)
        return self.start <= other_end and other_start <= self.end

    def overlap_seconds(self, start: Any, end: Any) -> float:
        """Seconds of overlap between this window and ``[start, end]``."""

        other_start = coerce_timestamp(start)
        other_end = coerce_timestamp(end)
        latest_start = max(self.start, other_start)
        earliest_end = min(self.end, other_end)
        return max(0.0, (earliest_end - latest_start).total_seconds())

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat().replace("+00:00", "Z"),
            "end": self.end.isoformat().replace("+00:00", "Z"),
            "duration_seconds": round(self.duration, 3),
            "observation_count": self.observation_count,
            "active_seconds": self.active_seconds,
            "digest": self.digest,
        }

    @classmethod
    def from_observations(
        cls,
        start: Any,
        end: Any,
        observations: Optional[Sequence[Mapping[str, Any]]] = None,
        active_seconds: Optional[float] = None,
    ) -> "EvidenceWindow":
        """Build a window from the exact observations a classifier will see."""

        items = list(observations or ())
        if active_seconds is None:
            total = 0.0
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                try:
                    total += float(item.get("duration_seconds") or 0.0)
                except (TypeError, ValueError):
                    continue
            active_seconds = total
        return cls(
            start=start,
            end=end,
            observation_count=len(items),
            active_seconds=active_seconds,
            digest=observation_digest(items),
        )

    @classmethod
    def from_episode(
        cls,
        episode: Any,
        observations: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> "EvidenceWindow":
        """Build a window describing an episode's current full extent."""

        start = getattr(episode, "start_time", None) or getattr(episode, "start")
        end = getattr(episode, "end_time", None) or getattr(episode, "end")
        active = getattr(episode, "active_duration_seconds", None)
        if not active:
            try:
                active = float(getattr(episode, "duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                active = 0.0
        items = list(observations or ())
        return cls(
            start=start,
            end=end,
            observation_count=len(items) or int(getattr(episode, "event_count", 0) or 0),
            active_seconds=active,
            digest=observation_digest(items),
        )


def parse_window(
    start: Any,
    end: Any,
    observation_count: Any = 0,
    active_seconds: Any = 0.0,
    digest: Any = "empty",
) -> Optional[EvidenceWindow]:
    """Rebuild a window from stored columns, or None when incomplete.

    Historical rows predate evidence windows. They come back as ``None``
    rather than a fabricated window, so a legacy verdict is correctly
    treated as "scope unknown" instead of "scope = everything".
    """

    if start is None or end is None:
        return None
    try:
        return EvidenceWindow(
            start=start,
            end=end,
            observation_count=observation_count,
            active_seconds=active_seconds,
            digest=digest,
        )
    except (TypeError, ValueError):
        return None
