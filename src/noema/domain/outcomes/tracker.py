"""Measure recovery from subsequent observed activity sessions.

The tracker accepts both raw ActivitySession evidence and canonical
MeaningfulSession records.  The service prefers the latter when available.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Optional

from noema.domain.activity import coerce_timestamp
from noema.application.classification import Classification
from noema.domain.intervention import Intervention
from noema.domain.sessions import ActivitySession


class RecoveryStatus(str, Enum):
    RECOVERED = "RECOVERED"
    NOT_RECOVERED = "NOT_RECOVERED"
    PENDING = "PENDING"


@dataclass(frozen=True)
class InterventionOutcome:
    intervention_id: str
    intervention_time: datetime
    recovery_status: RecoveryStatus
    recovery_time: Optional[datetime] = None
    recovery_session_id: Optional[str] = None
    recovery_duration_seconds: Optional[float] = None
    intervention_type: Optional[str] = None
    meme_id: Optional[str] = None
    outcome_id: Optional[str] = None
    # V3 attribution linkage. All optional: pre-V3 rows and unattributed
    # recoveries leave them empty rather than fabricated.
    detection_id: Optional[str] = None
    response_id: Optional[str] = None
    delivery_state: Optional[str] = None
    user_action: Optional[str] = None
    attribution: Optional[str] = None
    break_context: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "intervention_id", str(self.intervention_id))
        object.__setattr__(self, "intervention_time", coerce_timestamp(self.intervention_time))
        object.__setattr__(self, "recovery_status", RecoveryStatus(self.recovery_status))
        if self.recovery_time is not None:
            object.__setattr__(self, "recovery_time", coerce_timestamp(self.recovery_time))
        if self.recovery_duration_seconds is not None:
            object.__setattr__(self, "recovery_duration_seconds", float(self.recovery_duration_seconds))
        for field_name in ("detection_id", "response_id", "delivery_state",
                           "user_action", "attribution"):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, str(value).strip() if value else None)
        if self.attribution is not None and self.attribution not in {
                "direct", "ambient", "none"}:
            raise ValueError("attribution must be direct, ambient, or none")
        object.__setattr__(self, "break_context", bool(self.break_context))

    @property
    def id(self) -> str:
        return self.outcome_id or hashlib.sha256(self.intervention_id.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "intervention_id": self.intervention_id,
            "intervention_time": self.intervention_time.isoformat().replace("+00:00", "Z"),
            "recovery_status": self.recovery_status.value,
            "recovery_time": self.recovery_time.isoformat().replace("+00:00", "Z") if self.recovery_time else None,
            "recovery_session_id": self.recovery_session_id,
            "recovery_duration_seconds": self.recovery_duration_seconds,
            "intervention_type": self.intervention_type,
            "meme_id": self.meme_id,
            "detection_id": self.detection_id,
            "response_id": self.response_id,
            "delivery_state": self.delivery_state,
            "user_action": self.user_action,
            "attribution": self.attribution,
            "break_context": self.break_context,
        }


class OutcomeTracker:
    """Detect recovery using a configurable minimum productive duration."""

    def __init__(self, minimum_productive_seconds: float = 180.0, pending_window_seconds: float = 3600.0):
        if minimum_productive_seconds < 0 or pending_window_seconds < 0:
            raise ValueError("outcome windows cannot be negative")
        self.minimum_productive_seconds = float(minimum_productive_seconds)
        self.pending_window_seconds = float(pending_window_seconds)

    def measure(
        self,
        intervention: Intervention,
        sessions: Iterable[ActivitySession],
        classifications: Optional[Mapping[str, Classification]] = None,
        now: Optional[datetime] = None,
        meme_id: Optional[str] = None,
        detection_id: Optional[str] = None,
        response_id: Optional[str] = None,
        delivery_state: Optional[str] = None,
        user_action: Optional[str] = None,
        attribution: Optional[str] = None,
        break_context: bool = False,
    ) -> InterventionOutcome:
        classifications = classifications or {}

        def _has_active_time(session: Any) -> bool:
            try:
                return float(getattr(session, "active_duration_seconds", 1.0) or 0.0) > 0
            except (TypeError, ValueError):
                return True

        later = sorted(
            (session for session in sessions if session.start >= intervention.created_at),
            key=lambda session: (session.start, session.id),
        )
        recovered = next(
            (
                session for session in later
                if session.duration >= self.minimum_productive_seconds
                and classifications.get(session.id)
                and classifications[session.id].productivity == "productive"
                # AFK time is never recovery: a session with no active
                # seconds cannot prove the user returned to work.
                and _has_active_time(session)
            ),
            None,
        )
        if recovered:
            status = RecoveryStatus.RECOVERED
            recovery_time = recovered.start
            recovery_session_id = recovered.id
            recovery_duration = (recovered.start - intervention.created_at).total_seconds()
        else:
            current = coerce_timestamp(now) if now is not None else None
            status = (
                RecoveryStatus.NOT_RECOVERED
                if current and current >= intervention.created_at + timedelta(seconds=self.pending_window_seconds)
                else RecoveryStatus.PENDING
            )
            recovery_time = recovery_session_id = recovery_duration = None
        return InterventionOutcome(
            intervention_id=intervention.id,
            intervention_time=intervention.created_at,
            recovery_status=status,
            recovery_time=recovery_time,
            recovery_session_id=recovery_session_id,
            recovery_duration_seconds=recovery_duration,
            intervention_type=intervention.mode.value if intervention.mode else None,
            meme_id=meme_id,
            detection_id=detection_id,
            response_id=response_id,
            delivery_state=delivery_state,
            user_action=user_action,
            attribution=attribution,
            break_context=break_context,
        )
