"""Inspectably deterministic alignment between a session and active intent."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

from noema.application.classification import Classification
from noema.domain.session_context import is_meaningful_session, search_values
from noema.domain.sessions import ActivitySession

from .engine import Intent, _tokens


VERB_COMPATIBILITY = {
    "implement": {"coding", "development"},
    "code": {"coding", "development"},
    "build": {"coding", "development"},
    "debug": {"debugging", "coding", "development"},
    "read": {"reading", "research"},
    "research": {"reading", "research"},
    "write": {"writing", "coding"},
    "study": {"reading", "research"},
}


@dataclass(frozen=True)
class AlignmentResult:
    session_id: str
    intent_id: str
    aligned: bool
    score: float
    confidence: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "intent_id": self.intent_id,
            "aligned": self.aligned,
            "score": self.score,
            "confidence": self.confidence,
            "reason": self.reason,
        }


class GoalAligner:
    """Compute goal alignment without allowing a model to trigger actions."""

    def __init__(self, threshold: float = 0.35):
        if not 0 <= threshold <= 1:
            raise ValueError("alignment threshold must be between 0 and 1")
        self.threshold = threshold

    @staticmethod
    def _session_tokens(session: ActivitySession, classification: Optional[Classification]) -> Set[str]:
        values = search_values(session)
        if classification:
            values.extend(
                [
                    classification.category,
                    classification.topic,
                    classification.project,
                    classification.activity_type,
                ]
            )
        return set(token for value in values for token in _tokens(value))

    def align(
        self,
        session: ActivitySession,
        intent: Intent,
        classification: Optional[Classification] = None,
    ) -> AlignmentResult:
        if (not isinstance(session, ActivitySession) and not is_meaningful_session(session)) or not isinstance(intent, Intent):
            raise TypeError("GoalAligner expects an ActivitySession or MeaningfulSession and an Intent")
        intent_tokens = intent.search_tokens
        session_tokens = self._session_tokens(session, classification)
        overlap = intent_tokens & session_tokens
        overlap_score = len(overlap) / max(1, len(intent_tokens))
        productivity = classification.productivity if classification else "unknown"
        productivity_bonus = 0.15 if productivity == "productive" else 0.0
        compatibility_bonus = 0.0
        for verb, compatible in VERB_COMPATIBILITY.items():
            if verb in intent_tokens and (
                (classification and classification.activity_type in compatible)
                or (classification and classification.category in compatible)
            ):
                compatibility_bonus = 0.25
                break
        score = min(1.0, overlap_score * 0.60 + productivity_bonus + compatibility_bonus)
        aligned = score >= self.threshold
        reason_parts = []
        if overlap:
            reason_parts.append("shared tokens: " + ", ".join(sorted(overlap)))
        if compatibility_bonus:
            reason_parts.append("activity type matches goal verb")
        if productivity_bonus:
            reason_parts.append("classified productive")
        reason = "; ".join(reason_parts) if reason_parts else "no meaningful goal evidence"
        base_confidence = classification.confidence if classification else 0.0
        confidence = min(1.0, max(base_confidence, score))
        return AlignmentResult(session.id, intent.id, aligned, score, confidence, reason)
