"""Deterministic response selection (V3).

The selector resolves a reasoning response class + policy context into one
curated registry entry. It never calls a model, never invents copy, and
never blocks an otherwise-justified intervention: when nothing is
eligible it reports a fallback so the caller can use the legacy text
payload instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .models import MODE_RESPONSE_CLASSES, Response


@dataclass(frozen=True)
class Selection:
    """Selector outcome: a response, or an explicit fallback."""

    response: Optional[Response]
    fallback: bool
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "response_id": self.response.id if self.response else None,
            "fallback": self.fallback,
            "reason": self.reason,
        }


def _parse_moment(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def select_response(
    response_class: Optional[str] = None,
    mode: Optional[str] = None,
    severity: int = 2,
    context_tags: Iterable[str] = (),
    registry: Iterable[Response] = (),
    now: Optional[datetime] = None,
    preferred_tone: Optional[str] = None,
) -> Selection:
    """Pick one curated response deterministically.

    Eligibility: enabled, mode-compatible class, severity within bounds,
    per-response cooldown since last shown. Preference: exact class match,
    then preferred tone (V4 intervention guidance), then least-shown
    (exploration floor), then gentlest severity fit, then stable id order.
    Fully deterministic given the same inputs.
    """
    candidates = [item for item in list(registry or []) if isinstance(item, Response)]
    if not candidates:
        return Selection(response=None, fallback=True, reason="response registry is empty")
    mode_key = str(mode or "").strip().upper()
    allowed = MODE_RESPONSE_CLASSES.get(mode_key)
    if allowed is None:
        return Selection(response=None, fallback=True,
                         reason="mode {} serves no response class".format(mode_key or "?"))
    wanted = str(response_class or "NONE").strip().upper()
    try:
        severity_value = int(severity)
    except (TypeError, ValueError):
        severity_value = 2
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    wanted_tags = {str(tag).strip().lower() for tag in (context_tags or ()) if str(tag).strip()}
    tone_wanted = str(preferred_tone or "").strip().lower() or None

    eligible: List[Tuple[bool, bool, Response]] = []
    mode_kinds = {
        "MEME": {"MEME", "CHARACTER", "STICKER"},
        "NOTIFICATION": {"TEXT", "NUDGE", "CHALLENGE", "ENCOURAGEMENT"},
    }.get(mode_key, set())
    for item in candidates:
        if not item.enabled:
            continue
        if wanted == "NONE":
            continue
        if wanted not in allowed:
            # The mode cannot serve the requested class at all.
            continue
        if item.kind not in mode_kinds:
            continue
        # Class-level match: the kind must belong to the wanted class
        # family. Kind==class for MEME/CHARACTER/STICKER/CHALLENGE/
        # ENCOURAGEMENT; GENTLE maps to TEXT/NUDGE/ENCOURAGEMENT;
        # SARCASTIC to MEME.
        family = {
            "MEME": {"MEME"},
            "CHARACTER": {"CHARACTER"},
            "STICKER": {"STICKER"},
            "CHALLENGE": {"CHALLENGE"},
            "ENCOURAGEMENT": {"ENCOURAGEMENT"},
            "GENTLE": {"TEXT", "NUDGE", "ENCOURAGEMENT"},
            "SARCASTIC": {"MEME"},
            "NONE": set(),
        }.get(wanted, set())
        if item.kind not in family:
            continue
        if not (item.severity_min <= severity_value <= item.severity_max):
            continue
        if wanted_tags and item.context_tags and wanted_tags.isdisjoint(set(item.context_tags)):
            continue
        shown_at = _parse_moment(item.last_shown_at)
        if shown_at is not None:
            elapsed = (moment - shown_at).total_seconds()
            if elapsed < float(item.cooldown_seconds):
                continue
        exact = item.kind == wanted
        tone_hit = tone_wanted is not None and str(item.tone or "").strip().lower() == tone_wanted
        eligible.append((exact, tone_hit, item))
    if not eligible:
        return Selection(response=None, fallback=True,
                         reason="no eligible response for class {} severity {}".format(wanted, severity_value))
    eligible.sort(key=lambda triple: (
        not triple[0], not triple[1], triple[2].times_shown, triple[2].severity_min, triple[2].id))
    chosen = eligible[0][2]
    return Selection(response=chosen, fallback=False,
                     reason="selected {} for class {}".format(chosen.id, wanted))
