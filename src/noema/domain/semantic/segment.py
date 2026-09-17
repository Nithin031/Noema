"""Semantic subsegments: meaning changing inside an episode that continues.

The brief's constraint is precise: a change in meaning must not destroy the
temporal episode. An episode is a continuity claim ("this was one stretch of
activity"); a verdict is a meaning claim ("this stretch was work"). They can
disagree, and when they do the honest representation is one episode carrying
several bounded segments:

    EPISODE 79bc95ca  21:16-21:36
      SEG A 21:16-21:20  PRODUCTIVE 0.78   direct
      SEG B 21:20-21:27  UNCERTAIN         reclassified (mixed evidence)
      SEG C 21:27-21:36  DISTRACTIVE 0.91  reclassified

Each segment owns its own evidence window, so a row at 21:34 resolves against
segment C and can never pick up segment A's verdict.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from noema.domain.activity import coerce_timestamp

from .evidence_window import EvidenceWindow, parse_window
from .provenance import Provenance, SemanticState


@dataclass(frozen=True)
class SemanticSegment:
    """One bounded stretch of coherent meaning inside an episode."""

    episode_id: str
    sequence: int
    start_time: datetime
    end_time: datetime
    category: str = "uncertain"
    productivity: str = "neutral"
    confidence: float = 0.0
    activity: Optional[str] = None
    signal: Optional[str] = None
    topic: Optional[str] = None
    activity_type: Optional[str] = None
    evidence_quality: Optional[str] = None
    evidence_start: Optional[datetime] = None
    evidence_end: Optional[datetime] = None
    evidence_digest: Optional[str] = None
    evidence_observation_count: int = 0
    evidence_active_seconds: float = 0.0
    provenance: str = Provenance.DIRECT.value
    semantic_state: str = SemanticState.UNKNOWN.value
    classification_status: str = "pending"
    activity_session_ids: Tuple[str, ...] = ()
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    classifier_version: Optional[str] = None
    segment_id: Optional[str] = None

    def __post_init__(self) -> None:
        start = coerce_timestamp(self.start_time)
        end = coerce_timestamp(self.end_time)
        if end < start:
            raise ValueError("segment end cannot be before segment start")
        object.__setattr__(self, "start_time", start)
        object.__setattr__(self, "end_time", end)
        object.__setattr__(self, "episode_id", str(self.episode_id))
        object.__setattr__(self, "sequence", max(0, int(self.sequence)))
        object.__setattr__(self, "confidence", min(1.0, max(0.0, float(self.confidence or 0.0))))
        object.__setattr__(self, "activity_session_ids",
                           tuple(dict.fromkeys(str(item) for item in self.activity_session_ids)))
        for stamp in ("evidence_start", "evidence_end"):
            value = getattr(self, stamp)
            if value is not None:
                object.__setattr__(self, stamp, coerce_timestamp(value))
        try:
            count = int(self.evidence_observation_count or 0)
        except (TypeError, ValueError):
            count = 0
        object.__setattr__(self, "evidence_observation_count", max(0, count))
        try:
            active = float(self.evidence_active_seconds or 0.0)
        except (TypeError, ValueError):
            active = 0.0
        object.__setattr__(self, "evidence_active_seconds", round(max(0.0, active), 3))
        provenance = str(self.provenance or Provenance.DIRECT.value).strip().lower()
        if provenance not in {item.value for item in Provenance}:
            provenance = Provenance.DIRECT.value
        object.__setattr__(self, "provenance", provenance)
        state = str(self.semantic_state or SemanticState.UNKNOWN.value).strip().lower()
        if state not in {item.value for item in SemanticState}:
            state = SemanticState.UNKNOWN.value
        object.__setattr__(self, "semantic_state", state)
        status = str(self.classification_status or "pending").strip().lower()
        if status not in {"pending", "classified", "classification_failed"}:
            status = "pending"
        object.__setattr__(self, "classification_status", status)
        object.__setattr__(self, "category", str(self.category or "uncertain").strip().lower())

    @property
    def id(self) -> str:
        if self.segment_id:
            return self.segment_id
        identity = "{}|{}|{}|{}".format(
            self.episode_id, self.sequence,
            self.start_time.isoformat(), self.end_time.isoformat())
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    @property
    def duration(self) -> float:
        return (self.end_time - self.start_time).total_seconds()

    @property
    def evidence_window(self) -> Optional[EvidenceWindow]:
        return parse_window(
            self.evidence_start, self.evidence_end,
            self.evidence_observation_count, self.evidence_active_seconds,
            self.evidence_digest or "empty")

    @property
    def is_verdict(self) -> bool:
        return self.classification_status == "classified"

    def covers(self, start: Any, end: Any = None, tolerance_seconds: float = 2.0) -> bool:
        """Whether this segment's evidence window contains the interval."""

        window = self.evidence_window
        if window is None:
            return False
        return window.covers_interval(start, end if end is not None else start,
                                      tolerance_seconds=tolerance_seconds)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "episode_id": self.episode_id,
            "sequence": self.sequence,
            "start_time": self.start_time.isoformat().replace("+00:00", "Z"),
            "end_time": self.end_time.isoformat().replace("+00:00", "Z"),
            "duration_seconds": round(self.duration, 3),
            "category": self.category,
            "productivity": self.productivity,
            "confidence": self.confidence,
            "activity": self.activity,
            "signal": self.signal,
            "topic": self.topic,
            "activity_type": self.activity_type,
            "evidence_quality": self.evidence_quality,
            "evidence_window": self.evidence_window.to_dict() if self.evidence_window else None,
            "provenance": self.provenance,
            "semantic_state": self.semantic_state,
            "classification_status": self.classification_status,
            "activity_session_ids": list(self.activity_session_ids),
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "classifier_version": self.classifier_version,
        }
