"""Configurable privacy filtering between telemetry and AI storage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from noema.domain.activity import ActivityEvent


DEFAULT_BLOCKED_APPS = frozenset(
    {
        "1password",
        "bitwarden",
        "dashlane",
        "keepass",
        "lastpass",
        "proton pass",
    }
)
PRIVATE_TITLE_RE = re.compile(r"\b(private|incognito|inprivate)\b", re.IGNORECASE)
PRIVATE_VALUES = frozenset({"private", "incognito", "inprivate", "true", "1", "yes"})


def _url_domain(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    if not hostname:
        return None
    hostname = hostname.casefold().rstrip(".")
    return hostname[4:] if hostname.startswith("www.") else hostname


def _normalized_set(values: Iterable[str]) -> frozenset:
    return frozenset(str(value).strip().casefold() for value in values if str(value).strip())


@dataclass(frozen=True)
class PrivacyPolicy:
    """Local privacy policy applied before AI processing or persistence."""

    blocked_apps: frozenset = field(default_factory=lambda: DEFAULT_BLOCKED_APPS)
    blocked_domains: frozenset = field(default_factory=frozenset)
    blocked_url_patterns: Tuple[str, ...] = ()
    discard_private_windows: bool = True
    strip_url_query: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocked_apps", _normalized_set(self.blocked_apps))
        object.__setattr__(self, "blocked_domains", _normalized_set(self.blocked_domains))


@dataclass(frozen=True)
class PrivacyDecision:
    allowed: bool
    reason: Optional[str] = None
    event: Optional[ActivityEvent] = None


class PrivacyFilter:
    """Apply a fail-closed privacy policy to normalized events."""

    def __init__(self, policy: Optional[PrivacyPolicy] = None):
        self.policy = policy or PrivacyPolicy()
        self._blocked_patterns = tuple(
            re.compile(pattern, re.IGNORECASE) for pattern in self.policy.blocked_url_patterns
        )

    @staticmethod
    def _app_is_blocked(app: Optional[str], blocked_apps: frozenset) -> bool:
        if not app:
            return False
        candidate = app.casefold().strip()
        return any(candidate == blocked or blocked in candidate for blocked in blocked_apps)

    @staticmethod
    def _domain_is_blocked(domain: Optional[str], blocked_domains: frozenset) -> bool:
        if not domain:
            return False
        candidate = domain.casefold().strip().rstrip(".")
        return any(candidate == blocked or candidate.endswith("." + blocked) for blocked in blocked_domains)

    @staticmethod
    def _private_window(event: ActivityEvent) -> bool:
        for key, value in event.metadata.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if normalized_key in {
                "private",
                "private_window",
                "incognito",
                "is_private",
                "is_incognito",
            }:
                if isinstance(value, bool) and value:
                    return True
                if str(value).casefold().strip() in PRIVATE_VALUES:
                    return True
            if normalized_key == "window_type" and str(value).casefold().strip() in PRIVATE_VALUES:
                return True
        return bool(event.title and PRIVATE_TITLE_RE.search(event.title))

    def evaluate(self, event: ActivityEvent) -> PrivacyDecision:
        if not isinstance(event, ActivityEvent):
            raise TypeError("PrivacyFilter expects a ActivityEvent")

        if self._app_is_blocked(event.app, self.policy.blocked_apps):
            return PrivacyDecision(False, "blocked_app")
        if self._domain_is_blocked(
            event.domain or _url_domain(event.url), self.policy.blocked_domains
        ):
            return PrivacyDecision(False, "blocked_domain")
        if self.policy.discard_private_windows and self._private_window(event):
            return PrivacyDecision(False, "private_window")
        if event.url and any(pattern.search(event.url) for pattern in self._blocked_patterns):
            return PrivacyDecision(False, "blocked_url_pattern")

        filtered = event
        if event.url and self.policy.strip_url_query:
            try:
                parts = urlsplit(event.url)
                filtered = replace(
                    event,
                    url=urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                )
            except ValueError:
                return PrivacyDecision(False, "malformed_url")
        return PrivacyDecision(True, event=filtered)

    def filter(self, event: ActivityEvent) -> Optional[ActivityEvent]:
        """Return a sanitized event, or ``None`` when it must be discarded."""

        return self.evaluate(event).event

    def apply(self, event: ActivityEvent) -> Optional[ActivityEvent]:
        """Explicit alias for callers that prefer verb-style APIs."""

        return self.filter(event)
