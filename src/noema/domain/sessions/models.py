"""Models produced by Generation 1 session intelligence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple

from noema.domain.activity import ActivityEvent, coerce_timestamp


@dataclass(frozen=True)
class ActivitySession:
    """A contiguous run of related normalized telemetry events."""

    start: datetime
    end: datetime
    device: str = "unknown"
    app: Optional[str] = None
    title: Optional[str] = None
    domain: Optional[str] = None
    url: Optional[str] = None
    source: str = "activitywatch"
    event_keys: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    browser: Optional[str] = None
    browser_window_id: Optional[str] = None
    browser_tab_id: Optional[str] = None

    def __post_init__(self) -> None:
        start = coerce_timestamp(self.start)
        end = coerce_timestamp(self.end)
        if end < start:
            raise ValueError("session end cannot be before session start")
        try:
            keys = tuple(str(key) for key in self.event_keys)
        except TypeError as exc:
            raise TypeError("event_keys must be iterable") from exc
        metadata = dict(self.metadata or {})
        # Validate serializability early so persistence cannot fail halfway
        # through a session batch.
        json.dumps(metadata, ensure_ascii=False, default=str)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "device", str(self.device or "unknown").strip() or "unknown")
        object.__setattr__(self, "app", str(self.app).strip() if self.app else None)
        object.__setattr__(self, "title", str(self.title).strip() if self.title else None)
        object.__setattr__(self, "domain", str(self.domain).lower().rstrip(".") if self.domain else None)
        object.__setattr__(self, "url", str(self.url).strip() if self.url else None)
        object.__setattr__(self, "source", str(self.source or "activitywatch").strip() or "activitywatch")
        object.__setattr__(self, "event_keys", keys)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "browser", str(self.browser).strip() if self.browser else None)
        object.__setattr__(self, "browser_window_id", str(self.browser_window_id).strip() if self.browser_window_id else None)
        object.__setattr__(self, "browser_tab_id", str(self.browser_tab_id).strip() if self.browser_tab_id else None)

    @classmethod
    def from_events(cls, events: Tuple[ActivityEvent, ...]) -> "ActivitySession":
        if not events:
            raise ValueError("cannot create a session without events")
        start = min(event.timestamp for event in events)
        end = max(event.end_timestamp for event in events)
        first = events[0]
        return cls(
            start=start,
            end=end,
            device=first.device,
            app=first.app,
            title=first.title,
            domain=first.domain,
            url=first.url,
            source=first.source,
            event_keys=tuple(event.event_key for event in events),
            browser=first.browser,
            browser_window_id=first.browser_window_id,
            browser_tab_id=first.browser_tab_id,
            metadata={
                "event_count": len(events),
                "sources": sorted({event.source for event in events}),
            },
        )

    @property
    def duration(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def event_count(self) -> int:
        return len(self.event_keys)

    @property
    def id(self) -> str:
        if self.session_id:
            return self.session_id
        identity = "|".join(
            [
                self.start.isoformat(),
                self.end.isoformat(),
                self.device,
                self.app or "",
                self.domain or "",
                self.browser or "",
                self.browser_window_id or "",
                self.browser_tab_id or "",
                *self.event_keys,
            ]
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "start": self.start.isoformat().replace("+00:00", "Z"),
            "end": self.end.isoformat().replace("+00:00", "Z"),
            "duration": self.duration,
            "device": self.device,
            "app": self.app,
            "title": self.title,
            "domain": self.domain,
            "url": self.url,
            "source": self.source,
            "event_count": self.event_count,
            "event_keys": list(self.event_keys),
            "metadata": dict(self.metadata),
            "browser": self.browser,
            "browser_window_id": self.browser_window_id,
            "browser_tab_id": self.browser_tab_id,
        }
