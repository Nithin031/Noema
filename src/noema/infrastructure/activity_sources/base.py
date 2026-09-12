"""Noema-owned telemetry source abstraction.

An :class:`ActivitySource` observes one facet of the local machine
(foreground window, input presence, browser bridge health) and yields
canonical :class:`~noema.domain.activity.ActivityEvent` rows on ``poll``.
Sources never touch the database, the network, or ActivityWatch: the daemon
ingests their events through the same
:meth:`~noema.application.pipeline.NoemaService.ingest_events` path as
every other origin, so privacy filtering, normalization, deduplication,
and sessionization apply uniformly.

Failure contract: a source that cannot observe reports ``unavailable`` or
``error`` and yields no events. Sources must never fabricate activity
(notably never ``ACTIVE`` presence) when the underlying OS API fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


OK = "ok"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"
ERROR = "error"


@dataclass
class SourcePoll:
    """One poll result from an activity source."""

    source: str
    events: List[Any] = field(default_factory=list)
    status: str = OK
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "events": len(self.events),
            "status": self.status,
            "detail": self.detail,
        }


def utcnow() -> datetime:
    """Single clock seam so tests can inject deterministic time."""
    return datetime.now(timezone.utc)


class ActivitySource:
    """Interface for Noema-native telemetry collectors."""

    name: str = "source"

    @property
    def supported(self) -> bool:
        """False when the platform cannot provide this telemetry."""
        return True

    def poll(self, now: Optional[datetime] = None) -> SourcePoll:
        """Observe once; return new/updated events (possibly none)."""
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:
        """Describe this source for diagnostics and daemon health."""
        return {
            "name": self.name,
            "supported": bool(self.supported),
            "status": UNAVAILABLE if not self.supported else OK,
        }

    def close(self) -> None:
        """Release any OS handles. Default: nothing held."""


class CompositeActivitySource(ActivitySource):
    """Fan-in poll across several sources with per-source isolation.

    One crashing or unsupported source degrades only its own result;
    every other source still contributes events.
    """

    name = "composite"

    def __init__(self, sources: Sequence[ActivitySource]):
        self.sources = list(sources)

    @property
    def supported(self) -> bool:
        return any(source.supported for source in self.sources)

    def poll(self, now: Optional[datetime] = None) -> SourcePoll:
        moment = now or utcnow()
        events: List[Any] = []
        parts: List[str] = []
        worst = OK
        order = {OK: 0, DEGRADED: 1, UNAVAILABLE: 2, ERROR: 3}
        for source in self.sources:
            try:
                result = source.poll(moment)
            except Exception as exc:  # never let one source kill the tick
                result = SourcePoll(
                    source=source.name, status=ERROR,
                    detail="{}: {}".format(type(exc).__name__, exc))
            events.extend(result.events)
            parts.append("{}={}".format(result.source, result.status))
            if order.get(result.status, 3) > order[worst]:
                worst = result.status
        return SourcePoll(source=self.name, events=events, status=worst,
                          detail="; ".join(parts))

    def health(self) -> Dict[str, Any]:
        children = []
        for source in self.sources:
            try:
                children.append(source.health())
            except Exception as exc:  # diagnostics must never raise
                children.append({"name": source.name, "supported": False,
                                 "status": ERROR,
                                 "detail": type(exc).__name__})
        return {"name": self.name, "supported": bool(self.supported),
                "sources": children}

    def close(self) -> None:
        for source in self.sources:
            try:
                source.close()
            except Exception:
                pass
