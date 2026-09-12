"""Data contracts for semantic sessions and their internal phases."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from noema.domain.activity import coerce_timestamp


class MeaningfulSessionStatus(str, Enum):
    CANDIDATE = "CANDIDATE"
    FORMING = "FORMING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"
    SUMMARIZED = "SUMMARIZED"


class EvidenceQuality(str, Enum):
    """Quality level of evidence available for a meaningful session classification.

    Determined by the observation layer, not the model. The model must never
    inflate or fabricate evidence quality.
    """
    STRONG = "strong"        # title + domain agree; or title + app + explicit metadata
    MODERATE = "moderate"    # title + app agree, but domain/URL missing or generic
    WEAK = "weak"            # only app name or only title available; domain unknown
    ABSENT = "absent"        # no meaningful signal beyond process name


class MeaningfulSessionOutcome(str, Enum):
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    INTERRUPTED = "interrupted"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SessionPhase:
    """A semantic phase inside a larger meaningful session."""

    phase_type: str
    start_time: datetime
    end_time: datetime
    activity_session_ids: Tuple[str, ...] = ()
    confidence: float = 0.0

    def __post_init__(self) -> None:
        start = coerce_timestamp(self.start_time)
        end = coerce_timestamp(self.end_time)
        if end < start:
            raise ValueError("phase end cannot be before phase start")
        object.__setattr__(self, "phase_type", str(self.phase_type or "unknown").strip().lower())
        object.__setattr__(self, "start_time", start)
        object.__setattr__(self, "end_time", end)
        object.__setattr__(self, "activity_session_ids", tuple(str(item) for item in self.activity_session_ids))
        object.__setattr__(self, "confidence", min(1.0, max(0.0, float(self.confidence))))

    @property
    def duration(self) -> float:
        return (self.end_time - self.start_time).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase_type": self.phase_type,
            "start_time": self.start_time.isoformat().replace("+00:00", "Z"),
            "end_time": self.end_time.isoformat().replace("+00:00", "Z"),
            "duration": self.duration,
            "activity_session_ids": list(self.activity_session_ids),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class MeaningfulSession:
    """A semantic task unit composed of one or more ActivitySessions."""

    start_time: datetime
    end_time: datetime
    device_set: Tuple[str, ...] = ()
    activity_session_ids: Tuple[str, ...] = ()
    primary_project: Optional[str] = None
    primary_task: Optional[str] = None
    primary_topic: Optional[str] = None
    activities: Tuple[str, ...] = ()
    dominant_category: Optional[str] = None
    dominant_activity_type: Optional[str] = None
    intent_id: Optional[str] = None
    alignment_score: float = 0.0
    focus_score: float = 0.0
    distraction_score: float = 0.0
    context_switch_count: int = 0
    active_duration_seconds: float = 0.0
    afk_duration_seconds: float = 0.0
    status: MeaningfulSessionStatus = MeaningfulSessionStatus.CLOSED
    confidence: float = 0.0
    summary: Any = None
    outcome: MeaningfulSessionOutcome = MeaningfulSessionOutcome.UNKNOWN
    evidence: Tuple[str, ...] = ()
    evidence_quality: EvidenceQuality = EvidenceQuality.ABSENT
    phases: Tuple[SessionPhase, ...] = ()
    session_id: Optional[str] = None
    browser: Optional[str] = None
    browser_window_id: Optional[str] = None
    browser_tab_id: Optional[str] = None

    def __post_init__(self) -> None:
        start = coerce_timestamp(self.start_time)
        end = coerce_timestamp(self.end_time)
        if end < start:
            raise ValueError("meaningful session end cannot be before start")
        object.__setattr__(self, "start_time", start)
        object.__setattr__(self, "end_time", end)
        object.__setattr__(self, "device_set", tuple(dict.fromkeys(str(item) for item in self.device_set)))
        object.__setattr__(self, "activity_session_ids", tuple(dict.fromkeys(str(item) for item in self.activity_session_ids)))
        object.__setattr__(self, "activities", tuple(dict.fromkeys(str(item).strip().lower() for item in self.activities if str(item).strip())))
        for field_name in ("primary_project", "primary_task", "primary_topic", "dominant_category", "dominant_activity_type", "intent_id", "browser", "browser_window_id", "browser_tab_id"):
            value = getattr(self, field_name)
            object.__setattr__(self, field_name, str(value).strip() if value else None)
        object.__setattr__(self, "alignment_score", min(1.0, max(0.0, float(self.alignment_score))))
        object.__setattr__(self, "focus_score", min(1.0, max(0.0, float(self.focus_score))))
        object.__setattr__(self, "distraction_score", min(1.0, max(0.0, float(self.distraction_score))))
        object.__setattr__(self, "context_switch_count", max(0, int(self.context_switch_count)))
        try:
            active_duration = float(self.active_duration_seconds)
        except (TypeError, ValueError):
            active_duration = 0.0
        try:
            afk_duration = float(self.afk_duration_seconds)
        except (TypeError, ValueError):
            afk_duration = 0.0
        object.__setattr__(self, "active_duration_seconds", round(max(0.0, active_duration), 3))
        object.__setattr__(self, "afk_duration_seconds", round(max(0.0, afk_duration), 3))
        object.__setattr__(self, "status", MeaningfulSessionStatus(self.status))
        object.__setattr__(self, "outcome", MeaningfulSessionOutcome(self.outcome))
        object.__setattr__(self, "confidence", min(1.0, max(0.0, float(self.confidence))))
        object.__setattr__(self, "evidence", tuple(dict.fromkeys(str(item) for item in self.evidence)))
        object.__setattr__(self, "evidence_quality", EvidenceQuality(self.evidence_quality))
        object.__setattr__(self, "phases", tuple(self.phases))
        if self.summary is not None and not isinstance(self.summary, (str, Mapping)):
            object.__setattr__(self, "summary", str(self.summary))
        if isinstance(self.summary, Mapping):
            object.__setattr__(self, "summary", dict(self.summary))
        # Fail early if a caller supplied non-JSON metadata.
        json.dumps(self.summary, default=str)

    @property
    def duration(self) -> float:
        return (self.end_time - self.start_time).total_seconds()

    @property
    def start(self) -> datetime:
        return self.start_time

    @property
    def end(self) -> datetime:
        return self.end_time

    @property
    def id(self) -> str:
        if self.session_id:
            return self.session_id
        identity = "|".join(
            [self.start_time.isoformat(), self.end_time.isoformat(), *self.activity_session_ids]
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "start_time": self.start_time.isoformat().replace("+00:00", "Z"),
            "end_time": self.end_time.isoformat().replace("+00:00", "Z"),
            "duration": self.duration,
            "device_set": list(self.device_set),
            "activity_session_ids": list(self.activity_session_ids),
            "primary_project": self.primary_project,
            "primary_task": self.primary_task,
            "primary_topic": self.primary_topic,
            "activities": list(self.activities),
            "dominant_category": self.dominant_category,
            "dominant_activity_type": self.dominant_activity_type,
            "intent_id": self.intent_id,
            "alignment_score": self.alignment_score,
            "focus_score": self.focus_score,
            "distraction_score": self.distraction_score,
            "context_switch_count": self.context_switch_count,
            "active_duration_seconds": self.active_duration_seconds,
            "afk_duration_seconds": self.afk_duration_seconds,
            "status": self.status.value,
            "confidence": self.confidence,
            "summary": self.summary,
            "outcome": self.outcome.value,
            "evidence": list(self.evidence),
            "evidence_quality": self.evidence_quality.value,
            "phases": [phase.to_dict() for phase in self.phases],
            "browser": self.browser,
            "browser_window_id": self.browser_window_id,
            "browser_tab_id": self.browser_tab_id,
        }
