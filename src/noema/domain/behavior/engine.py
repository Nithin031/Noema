"""Deterministic state transitions for behavioral intelligence."""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional

from noema.application.classification import Classification
from noema.domain.intent import AlignmentResult
from noema.domain.session_context import has_observable_activity
from noema.domain.sessions import ActivitySession

from .models import BehaviorObservation, BehaviorState


class BehaviorEngine:
    """Convert semantic/alignment evidence into inspectable behavior states.

    Models may supply classifications, but this state machine owns the
    transition and actionability rules. No intervention is triggered here.
    """

    def __init__(self, min_actionable_distraction_seconds: float = 300.0):
        if min_actionable_distraction_seconds < 0:
            raise ValueError("minimum actionable distraction cannot be negative")
        self.min_actionable_distraction_seconds = float(min_actionable_distraction_seconds)

    @staticmethod
    def _candidate(
        session: ActivitySession,
        classification: Optional[Classification],
        alignment: Optional[AlignmentResult],
        previous: Optional[BehaviorState],
    ) -> BehaviorState:
        # Only real verdicts are semantic evidence. Pending/failed rows
        # carry a placeholder category internally and must never shape
        # behavior: they are invisible here.
        if classification is not None and getattr(
                classification, "classification_status", None) != "classified":
            classification = None
        category = classification.category if classification else ""
        activity_type = classification.activity_type if classification else ""
        productivity = classification.productivity if classification else ""
        if not has_observable_activity(session):
            return BehaviorState.IDLE
        if category == "break" or activity_type == "break":
            return BehaviorState.BREAK
        if productivity == "distracting":
            return BehaviorState.DISTRACTED
        # DRIFTING requires an *explicit* misaligned verdict. A merely
        # non-aligned result — thin evidence, no goal, or a borderline
        # relationship — carries relation "unknown" and must NOT drift:
        # uncertainty is never treated as distraction. (Legacy results that
        # only set aligned=False derive relation "unknown", so they too stay
        # NORMAL rather than falsely drifting.)
        if alignment is not None and getattr(
                alignment, "relation", None) == "misaligned":
            return BehaviorState.DRIFTING
        if alignment and alignment.aligned and productivity == "productive":
            if previous in {BehaviorState.DISTRACTED, BehaviorState.DRIFTING}:
                return BehaviorState.RECOVERING
            return BehaviorState.FOCUSED
        return BehaviorState.NORMAL

    @staticmethod
    def _score(
        state: BehaviorState,
        classification: Optional[Classification],
        alignment: Optional[AlignmentResult],
    ) -> float:
        if state == BehaviorState.DISTRACTED:
            return 0.9 if classification and classification.confidence >= 0.7 else 0.7
        if state in {BehaviorState.DRIFTING, BehaviorState.BREAK}:
            return 0.55
        if alignment:
            return alignment.confidence
        return classification.confidence if classification else 0.25

    def evaluate(
        self,
        sessions: Iterable[ActivitySession],
        classifications: Optional[Mapping[str, Classification]] = None,
        alignments: Optional[Mapping[str, AlignmentResult]] = None,
    ) -> List[BehaviorObservation]:
        classifications = classifications or {}
        alignments = alignments or {}
        ordered = sorted(sessions, key=lambda item: (item.start, item.id))
        observations: List[BehaviorObservation] = []
        previous: Optional[BehaviorObservation] = None
        for session in ordered:
            classification = classifications.get(session.id)
            if classification is not None and getattr(
                    classification, "classification_status", None) != "classified":
                # Pending/failed rows are not semantic evidence, anywhere
                # in this evaluation (state, score, and confidence alike).
                classification = None
            alignment = alignments.get(session.id)
            previous_state = previous.state if previous else None
            state = self._candidate(session, classification, alignment, previous_state)
            score = self._score(state, classification, alignment)
            if state == BehaviorState.DISTRACTED:
                distraction_score = score
            elif state == BehaviorState.DRIFTING:
                distraction_score = 0.65
            else:
                distraction_score = 0.0
            started_at = session.start
            if previous and previous.state == state:
                started_at = previous.started_at
            confidence = max(score, classification.confidence if classification else 0.0)
            actionable = (
                state == BehaviorState.DISTRACTED
                and session.duration >= self.min_actionable_distraction_seconds
            )
            observation = BehaviorObservation(
                session_id=session.id,
                state=state,
                started_at=started_at,
                confidence=confidence,
                distraction_score=distraction_score,
                actionable=actionable,
                reason=self._reason(state, classification, alignment),
                previous_state=previous_state,
            )
            observations.append(observation)
            previous = observation
        return observations

    @staticmethod
    def _reason(
        state: BehaviorState,
        classification: Optional[Classification],
        alignment: Optional[AlignmentResult],
    ) -> str:
        if state == BehaviorState.IDLE:
            return "no active application or domain"
        if state == BehaviorState.BREAK:
            return "session classified as a break"
        if state == BehaviorState.DISTRACTED:
            return "session classified as distracting"
        if state == BehaviorState.DRIFTING:
            return alignment.reason if alignment else "session is not aligned with the active intent"
        if state == BehaviorState.RECOVERING:
            return "productive aligned session followed a distracted or drifting state"
        if state == BehaviorState.FOCUSED:
            return "productive session aligned with the active intent"
        return "active session without decisive alignment evidence"
