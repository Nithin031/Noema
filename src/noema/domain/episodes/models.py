"""Deterministic evidence-quality measurement for activity episodes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from enum import Enum


class EvidenceQuality(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Domain / URL deliberately NOT available.
_REASONS = {
    "LOW_BROWSER_CONTEXT": "no browser domain/URL evidence",
    "SHORT_DURATION": "session shorter than 15 seconds",
    "SINGLE_EVENT": "one event only",
    "GENERIC_TITLE": "generic browser title without page context",
    "RICH_EPISODE_CONTEXT": "sustained multi-fragment episode",
    "STRONG_BROWSER_CONTEXT": "browser domain/URL evidence present",
}


@dataclass(frozen=True)
class EvidenceScore:
    """Picture of what Noema actually measured (never model-dependent)."""

    quality: EvidenceQuality
    reason: str
    duration_seconds: float
    fragment_count: int
    has_browser_context: bool

    def to_dict(self) -> dict:
        return {
            "quality": self.quality.value,
            "reason": self.reason,
            "duration_seconds": self.duration_seconds,
            "fragment_count": self.fragment_count,
            "has_browser_context": self.has_browser_context,
        }


def assess_evidence(session: Any) -> EvidenceScore:
    """Grade raw evidence strength from session shape; never fabricates.

    Ordered, deterministic, and reads only observed facts. No scores are
    invented to match confidence — a high-confidence model verdict on weak
    evidence remains HIGH/LOW correctly independent of it.
    """
    duration = float(getattr(session, "duration", 0.0) or 0.0)
    fragments = int(getattr(session, "event_count", 0) or 0)
    # Browser context (domain/URL) may come from the extension for browser
    # rows, or be absent on window-only tiers. Native collectors emit empty
    # strings for both on OS-only rows.
    domain = getattr(session, "domain", None)
    url = getattr(session, "url", None)
    has_browser_context = bool(str(domain or "").strip()) or bool(
        str(url or "").strip())
    title = str(getattr(session, "title", None) or getattr(
        session, "primary_title", None) or "").strip().casefold()
    generic_titles = {"mozilla firefox", "google chrome", "new tab",
                      "new tab - mozilla firefox", "untitled", ""}
    generic = title in generic_titles
    if not has_browser_context and duration < 15.0 and fragments <= 1:
        score = EvidenceScore(EvidenceQuality.LOW, "LOW_BROWSER_CONTEXT",
                              duration, fragments, False)
    elif not has_browser_context and duration < 30.0:
        score = EvidenceScore(EvidenceQuality.LOW, "SHORT_DURATION",
                              duration, fragments, False)
    elif not has_browser_context:
        score = EvidenceScore(EvidenceQuality.MEDIUM,
                              "LOW_BROWSER_CONTEXT", duration, fragments, False)
    elif generic:
        score = EvidenceScore(EvidenceQuality.MEDIUM, "GENERIC_TITLE",
                              duration, fragments, True)
    elif fragments >= 3 or duration >= 120.0:
        score = EvidenceScore(EvidenceQuality.HIGH, "RICH_EPISODE_CONTEXT",
                              duration, fragments, True)
    else:
        score = EvidenceScore(EvidenceQuality.MEDIUM, "STRONG_BROWSER_CONTEXT",
                              duration, fragments, True)
    return score
