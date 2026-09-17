"""Positive-continuity and contradiction signals for verdict extension.

Vocabulary policy (deliberate and restrictive):

* Positive signals must be evidence FOR continuation — shared anchors
  between the new slice and the verdict's own evidence. A single shared
  application, one keyword, an allowlist hit, or the mere absence of
  entertainment tokens can never license extension alone.
* Contradiction signals reuse the existing deterministic policy module
  (:mod:`noema.domain.policy.activity_policy`) — no new keyword lists are
  invented here. Only an explicit policy ``distractive`` decision with
  substantive duration vetoes a productive/neutral verdict.
* Absence of contradiction is recorded but never counted as positive
  evidence. Ambiguity (thin or unknown evidence) fails closed to UNKNOWN.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Set

from noema.domain.policy.activity_policy import assess_policy

#: Minimum slice duration (seconds) for a slice to carry a verdict at all.
#: Excludes zero/negative glitches; thin-but-identified slices are still
#: assessable because continuity (not duration) does the licensing work.
MIN_SLICE_SECONDS = 1.0

#: Shared title tokens required for the title-overlap signal. Two, so that
#: one shared keyword (e.g. a repo name, or browser chrome like "firefox")
#: can never license extension by itself.
TITLE_OVERLAP_MIN_TOKENS = 2

#: Generic browser/vendor/chrome tokens excluded from overlap matching.
#: Without these, any two browser tabs would "overlap" on "mozilla firefox"
#: and unrelated tabs would falsely corroborate each other.
STOP_TOKENS = frozenset({
    "mozilla", "firefox", "chrome", "chromium", "brave", "edge", "safari",
    "opera", "microsoft", "windows", "google", "newtab", "untitled",
    "blank", "homepage", "startpage", "tab",
})

#: Policy-distractive veto needs substantive duration: a sustained
#: contradictory interval, not a transient window-focus flash.
CONTRADICTION_MIN_SECONDS = 5.0

_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def tokens(text: Any) -> Set[str]:
    """Content tokens: lowercase alnum, length >= 3, stopwords removed."""
    words = {
        word for word in _TOKEN_RE.sub(" ", str(text or "").casefold()).split()
        if len(word) >= 3 and word not in STOP_TOKENS
    }
    return words


def verdict_text(verdict: Mapping[str, Any], episode: Mapping[str, Any]) -> Set[str]:
    """Token pool describing what the verdict was decided on.

    Only evidence-bound strings: the verdict's own topic/activity and the
    episode's declared task/topic. Never the episode's grown member list —
    that would let later, unseen evidence corroborate itself.
    """
    pool: Set[str] = set()
    for value in (
        verdict.get("topic"),
        verdict.get("activity"),
        episode.get("primary_task"),
        episode.get("primary_topic"),
    ):
        pool |= tokens(value)
    return pool


def positive_signals(
    evidence: Mapping[str, Any],
    verdict: Mapping[str, Any],
    episode: Mapping[str, Any],
    member_apps: Set[str],
    member_domains: Set[str],
) -> Dict[str, bool]:
    """Evaluate each positive-continuity signal independently.

    Every signal is evidence FOR continuation. Missing data yields False
    (absent), never a default True.
    """
    app = str(evidence.get("app") or "").strip().casefold()
    domain = str(evidence.get("domain") or "").strip().casefold()
    title = str(evidence.get("title") or "")

    normalized_apps = {str(item).strip().casefold() for item in member_apps if str(item).strip()}
    normalized_domains = {str(item).strip().casefold() for item in member_domains if str(item).strip()}

    overlap = tokens(title) & verdict_text(verdict, episode)

    tab_match = False
    window_id = evidence.get("window_id")
    tab_id = evidence.get("tab_id")
    if window_id is not None and tab_id is not None:
        tab_match = (
            str(window_id) == str(episode.get("browser_window_id"))
            and str(tab_id) == str(episode.get("browser_tab_id"))
            and str(episode.get("browser_window_id") or "") != ""
        )

    return {
        # Same application family as the episode under review.
        "same_app": bool(app) and app in normalized_apps,
        # Same web origin as episode evidence.
        "shared_domain": bool(domain) and domain in normalized_domains,
        # Independent topical corroboration (>=2 shared content tokens).
        "title_overlap": len(overlap) >= TITLE_OVERLAP_MIN_TOKENS,
        # Same exact browser tab continued.
        "same_browser_tab": tab_match,
    }


def material_contradiction(
    evidence: Mapping[str, Any],
    verdict_productivity: Optional[str],
) -> Optional[str]:
    """Return a contradiction reason, or None when nothing contradicts.

    Only an explicit deterministic-policy ``distractive`` decision with
    substantive duration vetoes a productive/neutral verdict. "Inspect"
    outcomes (YouTube, short WhatsApp, browser containers) and the absence
    of any policy hit are NOT contradictions — and symmetrically, they are
    never positive evidence either. There is no deterministic productive
    policy, so distractive verdicts have no contradiction veto by
    construction; they are protected solely by the continuity gate.
    """
    if (verdict_productivity or "").strip().lower() not in {"productive", "neutral"}:
        return None
    try:
        seconds = max(0.0, float(evidence.get("seconds") or 0.0))
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds < CONTRADICTION_MIN_SECONDS:
        return None
    prior = assess_policy(
        evidence.get("app"),
        evidence.get("title"),
        evidence.get("domain"),
        evidence.get("url"),
        seconds,
    )
    if prior.decision == "distractive":
        return "policy '{}': {}".format(prior.matched_term, prior.reason)
    return None


def evidence_identity(evidence: Mapping[str, Any]) -> List[str]:
    """Human-readable list of which identifying fields a slice has."""
    present = [
        key for key in ("app", "title", "domain")
        if str(evidence.get(key) or "").strip()
    ]
    return present
