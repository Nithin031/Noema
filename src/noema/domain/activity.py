"""Domain models at the collector/Noema boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional


UTC = timezone.utc


def coerce_timestamp(value: Any) -> datetime:
    """Convert common collector timestamp forms to an aware UTC datetime."""

    if isinstance(value, datetime):
        timestamp = value
    elif isinstance(value, (int, float)):
        timestamp = datetime.fromtimestamp(float(value), tz=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp cannot be empty")
        try:
            timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601 or Unix seconds") from exc
    else:
        raise TypeError("timestamp must be a datetime, number, or ISO-8601 string")

    if timestamp.tzinfo is None:
        # ActivityWatch timestamps are UTC. Treat a naive value as UTC rather
        # than silently applying the host timezone.
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_safe(value: Any) -> Any:
    """Return a JSON-compatible copy of arbitrary watcher metadata."""

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, Mapping):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(item) for item in value]
        return str(value)


@dataclass(frozen=True)
class ActivityEvent:
    """Stable, privacy-filterable representation of observed activity."""

    timestamp: datetime
    duration: float
    device: str = "unknown"
    app: Optional[str] = None
    title: Optional[str] = None
    domain: Optional[str] = None
    url: Optional[str] = None
    source: str = "activitywatch"
    bucket_id: Optional[str] = None
    source_event_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Browser identity is optional because most ActivityWatch events are not
    # browser events.  When present it lets interventions target the exact tab
    # that produced the evidence.
    browser: Optional[str] = None
    browser_window_id: Optional[str] = None
    browser_tab_id: Optional[str] = None

    def __post_init__(self) -> None:
        timestamp = coerce_timestamp(self.timestamp)
        try:
            duration = float(self.duration)
        except (TypeError, ValueError) as exc:
            raise ValueError("duration must be a number") from exc
        if duration < 0:
            raise ValueError("duration cannot be negative")

        object.__setattr__(self, "timestamp", timestamp)
        object.__setattr__(self, "duration", duration)
        object.__setattr__(self, "device", _clean_text(self.device) or "unknown")
        object.__setattr__(self, "app", _clean_text(self.app))
        object.__setattr__(self, "title", _clean_text(self.title))
        domain = _clean_text(self.domain)
        object.__setattr__(self, "domain", domain.lower().rstrip(".") if domain else None)
        object.__setattr__(self, "url", _clean_text(self.url))
        object.__setattr__(self, "source", _clean_text(self.source) or "activitywatch")
        object.__setattr__(self, "bucket_id", _clean_text(self.bucket_id))
        object.__setattr__(self, "source_event_id", _clean_text(self.source_event_id))
        object.__setattr__(self, "metadata", _json_safe(dict(self.metadata or {})))
        object.__setattr__(self, "browser", _clean_text(self.browser))
        object.__setattr__(self, "browser_window_id", _clean_text(self.browser_window_id))
        object.__setattr__(self, "browser_tab_id", _clean_text(self.browser_tab_id))

    @property
    def timestamp_iso(self) -> str:
        """Canonical UTC timestamp used by the application database/API."""

        return self.timestamp.isoformat().replace("+00:00", "Z")

    @property
    def end_timestamp(self) -> datetime:
        from datetime import timedelta

        return self.timestamp + timedelta(seconds=self.duration)

    @property
    def event_key(self) -> str:
        """Stable key used to make repeated ingestion idempotent."""

        if self.bucket_id and self.source_event_id:
            identity = "source-id|{}|{}|{}".format(
                self.source, self.bucket_id, self.source_event_id
            )
        else:
            identity = "content|{}|{}|{}|{}|{}|{}|{}|{}".format(
                self.source,
                self.bucket_id or "",
                self.timestamp_iso,
                self.duration,
                self.device,
                self.app or "",
                self.title or "",
                self.domain or "",
                self.url or "",
            )
            browser_identity = (self.browser, self.browser_window_id, self.browser_tab_id)
            if any(browser_identity):
                identity += "|{}|{}|{}".format(*(value or "" for value in browser_identity))
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp_iso,
            "duration": self.duration,
            "device": self.device,
            "app": self.app,
            "title": self.title,
            "domain": self.domain,
            "url": self.url,
            "source": self.source,
            "bucket_id": self.bucket_id,
            "source_event_id": self.source_event_id,
            "metadata": dict(self.metadata),
            "browser": self.browser,
            "browser_window_id": self.browser_window_id,
            "browser_tab_id": self.browser_tab_id,
        }


@dataclass(frozen=True)
class StoredActivityEvent:
    """An event plus its local application-database identifier."""

    id: int
    event: ActivityEvent

    def to_dict(self) -> Dict[str, Any]:
        payload = self.event.to_dict()
        payload["id"] = self.id
        return payload
