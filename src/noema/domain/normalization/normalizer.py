"""Small, deterministic normalization stage for adapter output."""

from __future__ import annotations

from dataclasses import replace
import re
from typing import Optional
from urllib.parse import urlsplit

from noema.domain.activity import ActivityEvent


def _domain_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    if not hostname:
        return None
    hostname = hostname.lower().rstrip(".")
    return hostname[4:] if hostname.startswith("www.") else hostname


def application_identity(app: Optional[str], title: Optional[str] = None) -> tuple[str, str]:
    """Return a stable application id and a human-readable display name."""

    raw_app = str(app or "").strip()
    raw_title = str(title or "").strip()
    evidence = "{} {}".format(raw_app, raw_title).casefold()
    if "rocketleague" in evidence or "rocket league" in evidence:
        return "rocket_league", "Rocket League"
    if "visual studio code" in evidence or raw_app.casefold() in {"code", "code.exe"}:
        return "visual_studio_code", "Visual Studio Code"
    if "firefox" in evidence:
        return "firefox", "Firefox"
    if "chrome" in evidence:
        return "chrome", "Chrome"
    cleaned = re.sub(r"\.exe$", "", raw_app, flags=re.IGNORECASE)
    application_id = re.sub(r"[^a-z0-9]+", "_", cleaned.casefold()).strip("_") or "other"
    return application_id, cleaned or "Other"


class EventNormalizer:
    """Keep common fields stable while retaining watcher metadata.

    The adapter handles source-specific field mapping. This second stage is
    intentionally source-agnostic so future phone/tablet adapters can use the
    same privacy and storage boundary.
    """

    def normalize(self, event: ActivityEvent) -> ActivityEvent:
        if not isinstance(event, ActivityEvent):
            raise TypeError("EventNormalizer expects a ActivityEvent")
        domain = event.domain or _domain_from_url(event.url)
        application_id, display_name = application_identity(event.app, event.title)
        metadata = dict(event.metadata or {})
        metadata.setdefault("application_id", application_id)
        metadata.setdefault("application_display_name", display_name)
        return replace(
            event,
            domain=domain,
            device=event.device.strip() or "unknown",
            app=event.app.strip() if event.app else None,
            title=event.title.strip() if event.title else None,
            metadata=metadata,
        )

    def __call__(self, event: ActivityEvent) -> ActivityEvent:
        return self.normalize(event)
