"""Build reproducible semantic sessions from stored ActivitySessions."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional

from noema.application.classification import Classification
from noema.domain.intent import AlignmentResult, Intent
from noema.domain.sessions import ActivitySession

from .confidence import ConfidenceCalculator
from .continuity import ContinuityScorer
from .models import MeaningfulSession, MeaningfulSessionStatus
from .phase_detector import PhaseDetector


class MeaningfulSessionEngine:
    """Group adjacent technical sessions using semantic continuity signals."""

    def __init__(self, continuity_threshold: float = 0.47):
        if not 0 <= continuity_threshold <= 1:
            raise ValueError("continuity threshold must be between 0 and 1")
        self.continuity_threshold = continuity_threshold
        self.scorer = ContinuityScorer()
        self.phase_detector = PhaseDetector()
        self.confidence_calculator = ConfidenceCalculator()

    @staticmethod
    def _conflicting_projects(
        left: ActivitySession,
        right: ActivitySession,
        classifications: Mapping[str, Classification],
        alignments: Mapping[str, AlignmentResult],
    ) -> bool:
        left_class = classifications.get(left.id)
        right_class = classifications.get(right.id)
        left_project = left_class.project if left_class else None
        right_project = right_class.project if right_class else None
        if not left_project or not right_project or left_project.casefold() == right_project.casefold():
            return False
        # Strong alignment to the same explicit intent can bridge noisy model
        # project labels; conflicting inferred projects alone cannot.
        left_alignment = alignments.get(left.id)
        right_alignment = alignments.get(right.id)
        return not (left_alignment and right_alignment and left_alignment.aligned and right_alignment.aligned)

    @staticmethod
    def _afk_between(
        left: ActivitySession,
        right: ActivitySession,
        presence: Any = None,
    ) -> bool:
        """True when proven AFK time separates two sessions (PART 44).

        Return-from-AFK always starts a new meaningful session, no matter
        how similar the surrounding work looks. Without presence data the
        check is skipped (legacy behavior).
        """
        if presence is None:
            return False
        try:
            seconds = presence.afk_seconds(left.end, right.start)
        except (AttributeError, TypeError, ValueError):
            return False
        return float(seconds or 0.0) > 0.0

    def _can_merge(
        self,
        left: ActivitySession,
        right: ActivitySession,
        classifications: Mapping[str, Classification],
        alignments: Mapping[str, AlignmentResult],
        intent: Optional[Intent],
        presence: Any = None,
        left_evidence_quality: Any = None,
        right_evidence_quality: Any = None,
    ) -> bool:
        if self._afk_between(left, right, presence):
            return False
        if self._conflicting_projects(left, right, classifications, alignments):
            return False
        score = self.scorer.score(
            left,
            right,
            classifications.get(left.id),
            classifications.get(right.id),
            alignments.get(left.id),
            alignments.get(right.id),
            intent,
            left_evidence_quality=left_evidence_quality,
            right_evidence_quality=right_evidence_quality,
        )
        return score.total >= self.continuity_threshold

    @staticmethod
    def _majority(
        sessions: List[ActivitySession],
        classifications: Mapping[str, Classification],
        field_name: str,
    ) -> Optional[str]:
        values = []
        for session in sessions:
            classification = classifications.get(session.id)
            value = getattr(classification, field_name, None) if classification else None
            if value:
                values.extend([value] * max(1, round(session.duration)))
        return Counter(values).most_common(1)[0][0] if values else None

    def _build(
        self,
        sessions: List[ActivitySession],
        classifications: Mapping[str, Classification],
        alignments: Mapping[str, AlignmentResult],
        intent: Optional[Intent],
        sequence_terminated: bool,
        presence: Any = None,
    ) -> MeaningfulSession:
        confidence, evidence, focus, distraction, alignment, switches = self.confidence_calculator.calculate(
            sessions, intent, classifications, alignments
        )
        span_start = min(session.start for session in sessions)
        span_end = max(session.end for session in sessions)
        active_duration, afk_duration = self._presence_durations(
            sessions, span_start, span_end, presence)
        phases = self.phase_detector.detect(sessions, dict(classifications))
        activities = tuple(phase.phase_type for phase in phases)
        project = intent.project if intent and intent.project else self._majority(sessions, classifications, "project")
        topic = intent.topic if intent and intent.topic else self._majority(sessions, classifications, "topic")
        task = intent.goal if intent else topic or (sessions[0].title if sessions and sessions[0].title else None)
        evidence_quality = self.confidence_calculator.assess_evidence_quality(sessions)
        return MeaningfulSession(
            start_time=span_start,
            end_time=span_end,
            device_set=tuple(sorted({session.device for session in sessions})),
            activity_session_ids=tuple(session.id for session in sessions),
            primary_project=project,
            primary_task=task,
            primary_topic=topic,
            activities=activities,
            dominant_category=self._majority(sessions, classifications, "category"),
            dominant_activity_type=self._majority(sessions, classifications, "activity_type"),
            intent_id=intent.id if intent else None,
            alignment_score=alignment,
            focus_score=focus,
            distraction_score=distraction,
            context_switch_count=switches,
            active_duration_seconds=active_duration,
            afk_duration_seconds=afk_duration,
            status=MeaningfulSessionStatus.CLOSED if sequence_terminated else MeaningfulSessionStatus.ACTIVE,
            confidence=confidence,
            evidence=evidence,
            evidence_quality=evidence_quality,
            phases=tuple(phases),
            browser=sessions[-1].browser if sessions else None,
            browser_window_id=sessions[-1].browser_window_id if sessions else None,
            browser_tab_id=sessions[-1].browser_tab_id if sessions else None,
        )

    @staticmethod
    def _presence_durations(
        sessions: List[ActivitySession],
        span_start: Any,
        span_end: Any,
        presence: Any = None,
    ) -> tuple:
        """Split a session span into active vs AFK seconds (PART 20).

        Classification applies to the active portion. Without presence data
        the whole span counts as active (legacy behavior preserved).
        """
        span_seconds = max(0.0, (span_end - span_start).total_seconds())
        intervals = getattr(presence, "intervals", None) if presence is not None else None
        if presence is None or not intervals:
            # No presence evidence at all: legacy behavior counts the whole
            # span as active rather than stranding the session unclassifiable.
            return round(span_seconds, 3), 0.0
        try:
            active = float(presence.active_seconds(span_start, span_end) or 0.0)
            afk = float(presence.afk_seconds(span_start, span_end) or 0.0)
        except (AttributeError, TypeError, ValueError):
            return round(span_seconds, 3), 0.0
        active = round(max(0.0, active), 3)
        afk = round(max(0.0, afk), 3)
        if active + afk > span_seconds:
            scale = span_seconds / (active + afk) if (active + afk) else 0.0
            active, afk = round(active * scale, 3), round(afk * scale, 3)
        return active, afk

    def build(
        self,
        sessions: Iterable[ActivitySession],
        classifications: Optional[Mapping[str, Classification]] = None,
        alignments: Optional[Mapping[str, AlignmentResult]] = None,
        intent: Optional[Intent] = None,
        sequence_terminated: bool = True,
        presence: Any = None,
    ) -> List[MeaningfulSession]:
        classifications = classifications or {}
        alignments = alignments or {}
        ordered = sorted(sessions, key=lambda item: (item.start, item.id))
        if not ordered:
            return []
        groups: List[List[ActivitySession]] = []
        current = [ordered[0]]
        for session in ordered[1:]:
            left_eq = self.confidence_calculator.assess_evidence_quality([current[-1]])
            right_eq = self.confidence_calculator.assess_evidence_quality([session])
            if self._can_merge(current[-1], session, classifications, alignments, intent, presence, left_eq, right_eq):
                current.append(session)
            else:
                groups.append(current)
                current = [session]
        groups.append(current)
        return [self._build(group, classifications, alignments, intent, sequence_terminated, presence) for group in groups]
