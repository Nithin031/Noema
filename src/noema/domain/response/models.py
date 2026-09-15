"""Curated response library for V3 interventions.

A Response is a controlled, reviewable artifact — never free model text.
Gemini's reasoning names a *response class*; the selector in
:mod:`selector` resolves the actual registry entry deterministically.
Counters (shown/clicked/recovered) are maintained by the outcome worker,
never by UI code, so effectiveness analysis reads recovery — not clicks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple


# Response kinds. Kept aligned with the reasoning response classes where
# they overlap; TEXT/NUDGE cover the NOTIFICATION mode surface.
KIND_MEME = "MEME"
KIND_STICKER = "STICKER"
KIND_CHARACTER = "CHARACTER"
KIND_TEXT = "TEXT"
KIND_NUDGE = "NUDGE"
KIND_CHALLENGE = "CHALLENGE"
KIND_ENCOURAGEMENT = "ENCOURAGEMENT"
RESPONSE_KINDS = frozenset({
    KIND_MEME, KIND_STICKER, KIND_CHARACTER, KIND_TEXT, KIND_NUDGE,
    KIND_CHALLENGE, KIND_ENCOURAGEMENT,
})

# Tones a response may carry. Free-form tone strings are normalized to
# these at construction so analysis can group by tone.
TONE_GENTLE = "gentle"
TONE_SARCASTIC = "sarcastic"
TONE_SUPPORTIVE = "supportive"
TONE_PLAYFUL = "playful"
TONE_FIRM = "firm"
RESPONSE_TONES = frozenset({
    TONE_GENTLE, TONE_SARCASTIC, TONE_SUPPORTIVE, TONE_PLAYFUL, TONE_FIRM,
})

# Which reasoning response classes each mode may serve. HOLDOUT serves
# nothing by engine design (recorded, never delivered).
MODE_RESPONSE_CLASSES = {
    "MEME": frozenset({"MEME", "CHARACTER", "STICKER", "SARCASTIC"}),
    "NOTIFICATION": frozenset(
        {"GENTLE", "ENCOURAGEMENT", "CHALLENGE", "NONE"}),
}

# Copy slots the renderer may fill, from stored episode/goal strings only.
# Anything else in a template is rendered literally — never formatted —
# so a bad template cannot raise at delivery time.
COPY_SLOTS = ("goal", "minutes_away", "distraction")


@dataclass(frozen=True)
class Response:
    """One curated intervention response."""

    id: str
    kind: str
    tone: str = TONE_GENTLE
    title: str = "Noema"
    body_template: str = "You may be drifting from your current goal."
    asset_id: Optional[str] = None
    severity_min: int = 1
    severity_max: int = 5
    context_tags: Tuple[str, ...] = ()
    cooldown_seconds: float = 3600.0
    enabled: bool = True
    times_shown: int = 0
    times_clicked: int = 0
    recovery_count: int = 0
    last_shown_at: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id or "").strip())
        if not self.id:
            raise ValueError("response id cannot be empty")
        kind = str(self.kind or "").strip().upper()
        if kind not in RESPONSE_KINDS:
            raise ValueError("unknown response kind: {}".format(self.kind))
        object.__setattr__(self, "kind", kind)
        tone = str(self.tone or TONE_GENTLE).strip().lower()
        if tone not in RESPONSE_TONES:
            raise ValueError("unknown response tone: {}".format(self.tone))
        object.__setattr__(self, "tone", tone)
        object.__setattr__(self, "title", str(self.title or "Noema").strip() or "Noema")
        object.__setattr__(self, "body_template", str(self.body_template or "").strip())
        if self.asset_id is not None:
            object.__setattr__(self, "asset_id", str(self.asset_id).strip() or None)
        try:
            severity_min = int(self.severity_min)
        except (TypeError, ValueError):
            raise ValueError("severity_min must be an integer")
        try:
            severity_max = int(self.severity_max)
        except (TypeError, ValueError):
            raise ValueError("severity_max must be an integer")
        if not 1 <= severity_min <= 5 or not 1 <= severity_max <= 5:
            raise ValueError("severity bounds must be within 1-5")
        if severity_min > severity_max:
            raise ValueError("severity_min cannot exceed severity_max")
        object.__setattr__(self, "severity_min", severity_min)
        object.__setattr__(self, "severity_max", severity_max)
        object.__setattr__(
            self, "context_tags",
            tuple(dict.fromkeys(str(tag).strip().lower()
                                for tag in (self.context_tags or ())
                                if str(tag).strip())))
        try:
            cooldown = float(self.cooldown_seconds)
        except (TypeError, ValueError):
            raise ValueError("cooldown_seconds must be numeric")
        if cooldown < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        object.__setattr__(self, "cooldown_seconds", cooldown)
        object.__setattr__(self, "enabled", bool(self.enabled))
        for counter in ("times_shown", "times_clicked", "recovery_count"):
            try:
                value = int(getattr(self, counter) or 0)
            except (TypeError, ValueError):
                value = 0
            object.__setattr__(self, counter, max(0, value))

    def render(self, goal: Optional[str] = None,
               minutes_away: Optional[float] = None,
               distraction: Optional[str] = None) -> Dict[str, str]:
        """Render copy slots from stored strings only. Never raises."""
        values = {
            "goal": (str(goal).strip() if goal else "your goal"),
            "minutes_away": (
                str(int(minutes_away)) if minutes_away is not None else "?"),
            "distraction": (str(distraction).strip() if distraction else "this"),
        }
        try:
            body = self.body_template.format(
                **{key: values[key] for key in COPY_SLOTS if key in values})
        except (KeyError, IndexError, ValueError):
            body = self.body_template
        return {"title": self.title, "body": body}

    @property
    def recovery_rate(self) -> Optional[float]:
        if self.times_shown <= 0:
            return None
        return round(self.recovery_count / self.times_shown, 3)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "tone": self.tone,
            "title": self.title,
            "body_template": self.body_template,
            "asset_id": self.asset_id,
            "severity_min": self.severity_min,
            "severity_max": self.severity_max,
            "context_tags": list(self.context_tags),
            "cooldown_seconds": self.cooldown_seconds,
            "enabled": self.enabled,
            "times_shown": self.times_shown,
            "times_clicked": self.times_clicked,
            "recovery_count": self.recovery_count,
            "recovery_rate": self.recovery_rate,
            "last_shown_at": self.last_shown_at,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Response":
        import json as _json

        try:
            tags = _json.loads(row["context_tags_json"] or "[]")
        except (TypeError, ValueError, KeyError):
            tags = []
        return cls(
            id=str(row["id"]),
            kind=str(row["kind"]),
            tone=str(row.get("tone") or TONE_GENTLE),
            title=str(row.get("title") or "Noema"),
            body_template=str(row.get("body_template") or ""),
            asset_id=row.get("asset_id"),
            severity_min=int(row.get("severity_min") or 1),
            severity_max=int(row.get("severity_max") or 5),
            context_tags=tuple(tags) if isinstance(tags, list) else (),
            cooldown_seconds=float(row.get("cooldown_seconds") or 0.0),
            enabled=bool(row.get("enabled", True)),
            times_shown=int(row.get("times_shown") or 0),
            times_clicked=int(row.get("times_clicked") or 0),
            recovery_count=int(row.get("recovery_count") or 0),
            last_shown_at=row.get("last_shown_at"),
        )
