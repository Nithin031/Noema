"""Behavior state observations derived from sessions, intent, and semantics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from noema.domain.activity import coerce_timestamp


class BehaviorState(str, Enum):
    FOCUSED = "FOCUSED"
    NORMAL = "NORMAL"
    DRIFTING = "DRIFTING"
    DISTRACTED = "DISTRACTED"
    RECOVERING = "RECOVERING"
    BREAK = "BREAK"
    IDLE = "IDLE"


@dataclass(frozen=True)
class BehaviorObservation:
    session_id: str
    state: BehaviorState
    started_at: datetime
    confidence: float
    distraction_score: float
    actionable: bool
    reason: str
    previous_state: Optional[BehaviorState] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", coerce_timestamp(self.started_at))
        object.__setattr__(self, "session_id", str(self.session_id))
        object.__setattr__(self, "state", BehaviorState(self.state))
        if self.previous_state is not None:
            object.__setattr__(self, "previous_state", BehaviorState(self.previous_state))
        object.__setattr__(self, "confidence", min(1.0, max(0.0, float(self.confidence))))
        object.__setattr__(self, "distraction_score", min(1.0, max(0.0, float(self.distraction_score))))
        object.__setattr__(self, "actionable", bool(self.actionable))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "confidence": self.confidence,
            "distraction_score": self.distraction_score,
            "actionable": self.actionable,
            "reason": self.reason,
            "previous_state": self.previous_state.value if self.previous_state else None,
        }
