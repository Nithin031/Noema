"""Deterministic user activity policy (Class A/B/C + games).

This module is pure: episode features in, policy prior out. It never calls
a model and never fabricates evidence. The classifier receives the prior
as one more observed input and still decides from the full evidence.

Decision hierarchy (first match wins):

1. Actual game process / gameplay window -> distractive.
2. Configured inherently distractive app (Instagram/Netflix/Reddit/...) -> distractive.
3. WhatsApp -> inspect, or distractive on >10 continuous minutes.
4. YouTube -> inspect the session title.
5. Browser container -> neutral container, inspect the title.
6. Anything else -> no prior (normal semantic understanding).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

#: Continuous WhatsApp episode duration (seconds) that counts as strong
#: distractive evidence. Measured per continuous episode, never summed
#: across the day.
WHATSAPP_CONTINUOUS_SECONDS = 10 * 60.0

#: Applications that are browsers/containers, never activities by themselves.
BROWSERS = frozenset({
    "firefox.exe", "firefox",
    "chrome.exe", "chrome", "google chrome",
    "msedge.exe", "msedge", "microsoft edge",
    "brave.exe", "brave", "opera.exe", "opera",
    "safari", "chromium",
})

#: Applications with an explicit distractive default under user policy.
DISTRACTIVE_APPS = frozenset({
    "instagram",
    "netflix",
    "reddit",
    "tiktok",
})

#: Substrings identifying WhatsApp (app, web client, or title).
WHATSAPP_MARKERS = frozenset({"whatsapp"})

#: Substrings identifying YouTube (app, domain, or title).
YOUTUBE_MARKERS = frozenset({"youtube", "youtu.be"})

#: Known games (lowercase, punctuation-normalized). Matched as whole terms
#: against the normalized app/title/domain text.
KNOWN_GAMES = frozenset({
    "rocket league", "rocketleague",
    "chess",
    "counter strike", "counterstrike", "counter-strike", "cs2", "csgo", "cs go",
    "valorant",
    "gta", "grand theft auto",
    "minecraft",
    "fortnite",
    "apex", "apex legends", "apexlegends",
    "league of legends", "leagueoflegends",
    "dota",
    "overwatch",
})

#: Title markers indicating technical work ABOUT a game rather than
#: gameplay (docs, code, research). "Rocket League bot development" is
#: work; "Rocket League" alone is gameplay.
TECH_WORK_MARKERS = frozenset({
    "api", "documentation", "docs", "implementation", "implementing",
    "engine", "development", "developer", "developing", "research",
    "paper", "thesis", "codebase", "code", "programming", "github",
    "reinforcement",
})

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize(text: Any) -> str:
    words = _NON_ALNUM_RE.sub(" ", str(text or "").casefold()).split()
    return " ".join(words)


def _padded(text: Any) -> str:
    normalized = _normalize(text)
    return " {} ".format(normalized) if normalized else " "


def _contains_term(haystack: str, term: str) -> bool:
    return " {} ".format(_normalize(term)) in haystack


def _combined_text(app: Any, title: Any, domain: Any, url: Any) -> str:
    return " ".join(
        part for part in (
            _padded(app), _padded(title), _padded(domain), _padded(url),
        ) if part.strip()
    )


@dataclass(frozen=True)
class PolicyPrior:
    """Deterministic policy signal for one episode."""

    decision: str
    reason: str
    matched_term: Optional[str] = None
    continuous_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "matched_term": self.matched_term,
            "continuous_seconds": round(float(self.continuous_seconds), 1),
        }


def _is_technical_game_work(title: Any) -> bool:
    padded = _padded(title)
    return any(" {} ".format(marker) in padded for marker in TECH_WORK_MARKERS)


def assess_policy(
    app: Any = None,
    title: Any = None,
    domain: Any = None,
    url: Any = None,
    active_duration_seconds: float = 0.0,
    observed_apps: Iterable[Any] = (),
) -> PolicyPrior:
    """Compute the policy prior for one episode from observed features only."""
    try:
        continuous = max(0.0, float(active_duration_seconds or 0.0))
    except (TypeError, ValueError):
        continuous = 0.0
    text = _combined_text(app, title, domain, url)
    apps_text = " ".join(_normalize(item) for item in (observed_apps or []))

    # 1. Games: actual gameplay is distractive; technical work about a game
    #    is not gameplay.
    for game in sorted(KNOWN_GAMES, key=len, reverse=True):
        if not _contains_term(text, game):
            continue
        if _is_technical_game_work(title):
            return PolicyPrior(
                decision="inspect",
                reason="game-related technical work, not gameplay",
                matched_term=game,
                continuous_seconds=continuous,
            )
        return PolicyPrior(
            decision="distractive",
            reason="known game gameplay window",
            matched_term=game,
            continuous_seconds=continuous,
        )

    # 2. Configured inherently distractive applications.
    for name in sorted(DISTRACTIVE_APPS):
        if _contains_term(text, name) or _contains_term(apps_text, name):
            return PolicyPrior(
                decision="distractive",
                reason="configured distractive application policy",
                matched_term=name,
                continuous_seconds=continuous,
            )

    # 3. WhatsApp: inspect context; >10 continuous minutes is strong
    #    distractive evidence (per-episode continuity, never a daily sum).
    if any(_contains_term(text, marker) for marker in WHATSAPP_MARKERS):
        if continuous > WHATSAPP_CONTINUOUS_SECONDS:
            return PolicyPrior(
                decision="distractive",
                reason="continuous WhatsApp episode over 10 minutes",
                matched_term="whatsapp",
                continuous_seconds=continuous,
            )
        return PolicyPrior(
            decision="inspect",
            reason="WhatsApp requires title/duration/context inspection",
            matched_term="whatsapp",
            continuous_seconds=continuous,
        )

    # 4. YouTube: inspect the session title for learning vs entertainment.
    if any(_contains_term(text, marker) for marker in YOUTUBE_MARKERS):
        return PolicyPrior(
            decision="inspect",
            reason="YouTube requires session title inspection",
            matched_term="youtube",
            continuous_seconds=continuous,
        )

    # 5. Browser container: neutral by itself; the title carries the activity.
    app_padded = _padded(app)
    if any(" {} ".format(browser) in app_padded for browser in BROWSERS):
        return PolicyPrior(
            decision="container-neutral",
            reason="browser is a container; inspect session title",
            matched_term=str(app or "").strip() or None,
            continuous_seconds=continuous,
        )

    # 6. No prior: normal semantic understanding applies.
    return PolicyPrior(
        decision="none",
        reason="no special application policy applies",
        matched_term=None,
        continuous_seconds=continuous,
    )
