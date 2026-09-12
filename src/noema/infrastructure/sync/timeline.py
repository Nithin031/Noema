"""Portable event envelopes and deterministic cross-device merging."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from noema.domain.activity import ActivityEvent


@dataclass(frozen=True)
class SyncEnvelope:
    """JSON-safe batch exchanged between ActivityWatch-compatible devices."""

    device: str
    events: tuple
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "device": self.device,
            "events": [event.to_dict() for event in self.events],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> "SyncEnvelope":
        data = json.loads(payload)
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise ValueError("invalid sync envelope")
        events = tuple(ActivityEvent(**item) for item in data["events"])
        return cls(str(data.get("device") or "unknown"), events, int(data.get("schema_version", 1)))


class UnifiedTimeline:
    """Merge event streams from laptop, phone, and tablet by UTC time."""

    def __init__(self, events: Iterable[ActivityEvent] = ()):
        unique = {}
        for event in events:
            if not isinstance(event, ActivityEvent):
                raise TypeError("UnifiedTimeline expects ActivityEvent instances")
            unique[event.event_key] = event
        self._events = tuple(sorted(unique.values(), key=lambda item: (item.timestamp, item.device, item.event_key)))

    @property
    def events(self) -> tuple:
        return self._events

    def query(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        device: Optional[str] = None,
    ) -> List[ActivityEvent]:
        from noema.domain.activity import coerce_timestamp

        start_time = coerce_timestamp(start) if start is not None else None
        end_time = coerce_timestamp(end) if end is not None else None
        return [
            event for event in self._events
            if (start_time is None or event.end_timestamp > start_time)
            and (end_time is None or event.timestamp < end_time)
            and (device is None or event.device == device)
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {"events": [event.to_dict() for event in self._events], "count": len(self._events)}

    @classmethod
    def from_envelopes(cls, envelopes: Iterable[SyncEnvelope]) -> "UnifiedTimeline":
        return cls(event for envelope in envelopes for event in envelope.events)
