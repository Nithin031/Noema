"""Explainable continuity features between adjacent ActivitySessions."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Set

from noema.application.classification import Classification
from noema.domain.intent import AlignmentResult, Intent
from noema.domain.meaningful.models import EvidenceQuality
from noema.domain.sessions import ActivitySession


def _tokens(value: Optional[str]) -> Set[str]:
    return set(re.findall(r"[a-z0-9]+", (value or "").casefold()))


@dataclass(frozen=True)
class ContinuityScore:
    total: float
    project_similarity: float
    topic_similarity: float
    intent_alignment: float
    temporal_proximity: float
    activity_transition_compatibility: float
    title_similarity: float
    semantic_similarity: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "total": self.total,
            "project_similarity": self.project_similarity,
            "topic_similarity": self.topic_similarity,
            "intent_alignment": self.intent_alignment,
            "temporal_proximity": self.temporal_proximity,
            "activity_transition_compatibility": self.activity_transition_compatibility,
            "title_similarity": self.title_similarity,
            "semantic_similarity": self.semantic_similarity,
        }


class ContinuityScorer:
    """Score adjacent sessions without treating apps as task identity."""

    def __init__(self, temporal_tau_seconds: float = 180.0):
        if temporal_tau_seconds <= 0:
            raise ValueError("temporal_tau_seconds must be positive")
        self.temporal_tau_seconds = float(temporal_tau_seconds)

    @staticmethod
    def _similarity(left: Optional[str], right: Optional[str]) -> float:
        if left and right and left.casefold() == right.casefold():
            return 1.0
        if not left or not right:
            return 0.25
        a, b = _tokens(left), _tokens(right)
        return len(a & b) / max(1, len(a | b))

    @staticmethod
    def _transition(left: Optional[Classification], right: Optional[Classification]) -> float:
        if not left or not right:
            return 0.45
        if left.category == right.category or left.activity_type == right.activity_type:
            return 1.0
        pairs = {
            frozenset(("research", "development")),
            frozenset(("research", "coding")),
            frozenset(("development", "communication")),
            frozenset(("research", "documentation")),
        }
        if frozenset((left.category, right.category)) in pairs:
            return 0.8
        if "distracting" in {left.productivity, right.productivity}:
            return 0.1
        return 0.35

    @staticmethod
    def _evidence_quality_bonus(
        left: EvidenceQuality,
        right: EvidenceQuality,
    ) -> float:
        """Return a continuity bonus when both sessions have weak/absent evidence.

        When browser domain is unavailable, many short Firefox sessions fragment
        into many raw sessions with only app + title. These are genuinely part
        of the same browsing activity and should merge more aggressively. A
        bonus to temporal proximity weight compensates for the low title/domain
        similarity that otherwise breaks them apart.
        """
        weak = {EvidenceQuality.WEAK, EvidenceQuality.ABSENT}
        if left in weak and right in weak:
            return 0.15
        if left in weak or right in weak:
            return 0.05
        return 0.0

    def score(
        self,
        left: ActivitySession,
        right: ActivitySession,
        left_classification: Optional[Classification] = None,
        right_classification: Optional[Classification] = None,
        left_alignment: Optional[AlignmentResult] = None,
        right_alignment: Optional[AlignmentResult] = None,
        intent: Optional[Intent] = None,
        left_evidence_quality: EvidenceQuality = EvidenceQuality.ABSENT,
        right_evidence_quality: EvidenceQuality = EvidenceQuality.ABSENT,
    ) -> ContinuityScore:
        gap = max(0.0, (right.start - left.end).total_seconds())
        temporal = math.exp(-gap / self.temporal_tau_seconds)
        project_left = left_classification.project if left_classification else None
        project_right = right_classification.project if right_classification else None
        if intent and intent.project:
            project_left = project_left or intent.project if left_alignment and left_alignment.aligned else project_left
            project_right = project_right or intent.project if right_alignment and right_alignment.aligned else project_right
        topic_left = left_classification.topic if left_classification else left.domain or left.title
        topic_right = right_classification.topic if right_classification else right.domain or right.title
        project = self._similarity(project_left, project_right)
        topic = self._similarity(topic_left, topic_right)
        alignments = [item.score for item in (left_alignment, right_alignment) if item]
        intent_alignment = sum(alignments) / len(alignments) if alignments else 0.35
        title = self._similarity(left.title, right.title)
        semantic_values = []
        if left_classification and right_classification:
            semantic_values.extend([
                1.0 if left_classification.category == right_classification.category else 0.0,
                self._similarity(left_classification.topic, right_classification.topic),
                self._similarity(left_classification.project, right_classification.project),
            ])
        semantic = sum(semantic_values) / len(semantic_values) if semantic_values else 0.35
        transition = self._transition(left_classification, right_classification)
        # Evidence quality bonus: when both sessions have weak evidence
        # (e.g. browser without domain), boost temporal proximity to reduce
        # excessive fragmentation.
        eq_bonus = self._evidence_quality_bonus(left_evidence_quality, right_evidence_quality)
        # Intent and semantic continuity carry more weight than application
        # identity. Temporal proximity is a signal, never the sole boundary.
        total = min(1.0, max(0.0, (
            project * 0.20 + topic * 0.15 + intent_alignment * 0.25
            + (temporal + eq_bonus) * 0.15 + transition * 0.10
            + title * 0.05 + semantic * 0.10
        )))
        return ContinuityScore(total, project, topic, intent_alignment, temporal, transition, title, semantic)
