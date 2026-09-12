"""Predictive recommendations and a safe end-to-end activity cycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.domain.intent import Intent
from noema.domain.memory import PersonalizationProfile
from noema.domain.outcomes import InterventionOutcome


@dataclass(frozen=True)
class AgentRecommendation:
    action: str
    confidence: float
    reason: str
    intervention_mode: Optional[str] = None
    requires_confirmation: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "created_at", created_at.astimezone(timezone.utc))
        object.__setattr__(self, "confidence", min(1.0, max(0.0, float(self.confidence))))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "confidence": self.confidence,
            "reason": self.reason,
            "intervention_mode": self.intervention_mode,
            "requires_confirmation": self.requires_confirmation,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
        }


@dataclass(frozen=True)
class AutonomousCycle:
    ingested: Any
    sessions: tuple
    classifications: tuple
    observations: tuple
    recommendation: AgentRecommendation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ingested": self.ingested.to_dict(),
            "sessions": [item.to_dict() for item in self.sessions],
            "classifications": [item.to_dict() for item in self.classifications],
            "observations": [item.to_dict() for item in self.observations],
            "recommendation": self.recommendation.to_dict(),
        }


class AutonomousAgent:
    """Choose the next safe recommendation from current state and history."""

    def recommend(
        self,
        observation: BehaviorObservation,
        intent: Optional[Intent] = None,
        profile: Optional[PersonalizationProfile] = None,
        memories: Iterable[Any] = (),
        outcomes: Iterable[InterventionOutcome] = (),
    ) -> AgentRecommendation:
        if observation.state == BehaviorState.DISTRACTED and observation.actionable:
            mode = profile.preferred_intervention if profile else "NOTIFICATION"
            return AgentRecommendation(
                action="intervene",
                intervention_mode=mode,
                confidence=observation.confidence,
                reason="actionable distracted state detected; confirmation required",
            )
        if observation.state == BehaviorState.DRIFTING:
            return AgentRecommendation(
                action="monitor",
                confidence=observation.confidence,
                reason="drift detected but intervention threshold has not been met",
            )
        if observation.state in {BehaviorState.FOCUSED, BehaviorState.RECOVERING}:
            return AgentRecommendation(
                action="continue",
                confidence=observation.confidence,
                reason="productive aligned activity is underway",
                requires_confirmation=False,
            )
        if observation.state in {BehaviorState.BREAK, BehaviorState.IDLE}:
            return AgentRecommendation(
                action="respect_state",
                confidence=observation.confidence,
                reason="break or idle state should not be interrupted",
                requires_confirmation=False,
            )
        return AgentRecommendation(
            action="observe",
            confidence=observation.confidence,
            reason="insufficient evidence for autonomous action",
        )
