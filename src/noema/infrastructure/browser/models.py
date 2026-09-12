"""Contracts exchanged between the backend and browser extensions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from noema.domain.activity import coerce_timestamp


@dataclass
class BrowserRegistration:
    browser: str
    device_id: str
    extension_instance_id: str
    current_window_id: Optional[str] = None
    current_tab_id: Optional[str] = None
    private: bool = False
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        self.browser = str(self.browser or "").strip().casefold()
        self.device_id = str(self.device_id or "").strip()
        self.extension_instance_id = str(self.extension_instance_id or "").strip()
        if not self.browser or not self.device_id or not self.extension_instance_id:
            raise ValueError("browser, device_id, and extension_instance_id are required")
        self.current_window_id = str(self.current_window_id) if self.current_window_id is not None else None
        self.current_tab_id = str(self.current_tab_id) if self.current_tab_id is not None else None
        self.private = bool(self.private)
        self.registered_at = coerce_timestamp(self.registered_at)
        self.last_seen_at = coerce_timestamp(self.last_seen_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "browser": self.browser,
            "device_id": self.device_id,
            "extension_instance_id": self.extension_instance_id,
            "current_window_id": self.current_window_id,
            "current_tab_id": self.current_tab_id,
            "private": self.private,
            "registered_at": self.registered_at.isoformat().replace("+00:00", "Z"),
            "last_seen_at": self.last_seen_at.isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True)
class BrowserEvent:
    event_type: str
    window_id: Optional[str] = None
    tab_id: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    private: Optional[bool] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", str(self.event_type or "unknown").strip().lower())
        object.__setattr__(self, "window_id", str(self.window_id) if self.window_id is not None else None)
        object.__setattr__(self, "tab_id", str(self.tab_id) if self.tab_id is not None else None)
        object.__setattr__(self, "url", str(self.url) if self.url else None)
        object.__setattr__(self, "title", str(self.title) if self.title else None)
        object.__setattr__(self, "private", bool(self.private) if self.private is not None else None)
        object.__setattr__(self, "timestamp", coerce_timestamp(self.timestamp))
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BrowserEvent":
        return cls(
            event_type=payload.get("event_type") or payload.get("type") or "unknown",
            window_id=payload.get("window_id"),
            tab_id=payload.get("tab_id"),
            url=payload.get("url"),
            title=payload.get("title"),
            private=payload.get("private"),
            timestamp=payload.get("timestamp") or datetime.now(timezone.utc),
            metadata=payload.get("metadata") or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "window_id": self.window_id,
            "tab_id": self.tab_id,
            "url": self.url,
            "title": self.title,
            "private": self.private,
            "timestamp": self.timestamp.isoformat().replace("+00:00", "Z"),
            "metadata": dict(self.metadata),
        }
