"""Derive durable observations from evidence without copying raw timelines."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional

from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.application.classification import Classification
from noema.domain.outcomes import InterventionOutcome, RecoveryStatus
from noema.domain.session_context import session_label
from noema.domain.sessions import ActivitySession


class MemoryKind(str, Enum):
    BEHAVIORAL_OBSERVATION = "behavioral_observation"
    INTERVENTION_PREFERENCE = "intervention_preference"


@dataclass(frozen=True)
class Memory:
    text: str
    kind: MemoryKind = MemoryKind.BEHAVIORAL_OBSERVATION
    confidence: float = 0.0
    evidence_count: int = 0
    evidence_refs: tuple = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    memory_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not str(self.text or "").strip():
            raise ValueError("memory text cannot be empty")
        object.__setattr__(self, "text", str(self.text).strip())
        object.__setattr__(self, "kind", MemoryKind(self.kind))
        object.__setattr__(self, "confidence", min(1.0, max(0.0, float(self.confidence))))
        object.__setattr__(self, "evidence_count", max(0, int(self.evidence_count)))
        object.__setattr__(self, "evidence_refs", tuple(str(ref) for ref in self.evidence_refs))
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "created_at", created_at.astimezone(timezone.utc))

    @property
    def id(self) -> str:
        return self.memory_id or hashlib.sha256((self.kind.value + "|" + self.text).encode()).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "text": self.text, "kind": self.kind.value,
            "confidence": self.confidence, "evidence_count": self.evidence_count,
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True)
class PersonalizationProfile:
    preferred_intervention: Optional[str]
    recovered_rate_by_intervention: Mapping[str, float]
    sample_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "preferred_intervention": self.preferred_intervention,
            "recovered_rate_by_intervention": dict(self.recovered_rate_by_intervention),
            "sample_count": self.sample_count,
        }


class MemoryEngine:
    """Create candidates only from aggregated evidence, then validate them."""

    def __init__(self, minimum_evidence: int = 2, minimum_confidence: float = 0.5):
        self.minimum_evidence = minimum_evidence
        self.minimum_confidence = minimum_confidence

    def derive_distraction_patterns(
        self,
        sessions: Iterable[ActivitySession],
        classifications: Mapping[str, Classification],
        observations: Optional[Mapping[str, BehaviorObservation]] = None,
    ) -> List[Memory]:
        grouped: Dict[str, List[str]] = {}
        for session in sessions:
            classification = classifications.get(session.id)
            observation = observations.get(session.id) if observations else None
            if not classification or classification.productivity != "distracting":
                continue
            if observation and observation.state != BehaviorState.DISTRACTED:
                continue
            label = classification.topic or classification.category or session_label(session, "")
            if label:
                grouped.setdefault(label, []).append(session.id)
        memories = []
        for label, refs in grouped.items():
            if len(refs) < self.minimum_evidence:
                continue
            memories.append(Memory(
                text="{} activity is associated with distraction.".format(label),
                confidence=min(0.95, 0.45 + 0.1 * len(refs)),
                evidence_count=len(refs),
                evidence_refs=tuple(refs),
            ))
        return [memory for memory in memories if self.validate(memory)]

    def validate(self, memory: Memory) -> bool:
        return memory.evidence_count >= self.minimum_evidence and memory.confidence >= self.minimum_confidence


class Personalizer:
    """Turn measured intervention outcomes into an inspectable preference."""

    def build_profile(self, outcomes: Iterable[InterventionOutcome]) -> PersonalizationProfile:
        totals: Dict[str, int] = {}
        recovered: Dict[str, int] = {}
        for outcome in outcomes:
            mode = outcome.intervention_type or "unknown"
            totals[mode] = totals.get(mode, 0) + 1
            if outcome.recovery_status == RecoveryStatus.RECOVERED:
                recovered[mode] = recovered.get(mode, 0) + 1
        rates = {mode: recovered.get(mode, 0) / count for mode, count in totals.items()}
        preferred = max(rates, key=rates.get) if rates else None
        return PersonalizationProfile(preferred, rates, sum(totals.values()))
