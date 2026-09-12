"""Detect coarse task phases inside a meaningful session."""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from noema.application.classification import Classification
from noema.domain.sessions import ActivitySession

from .models import SessionPhase


class PhaseDetector:
    """Group adjacent raw sessions into research/implementation/debug phases."""

    def _phase_type(self, session: ActivitySession, classification: Optional[Classification]) -> str:
        if classification:
            value = (classification.activity_type or classification.category).casefold()
            if any(token in value for token in ("research", "reading", "paper")):
                return "research"
            if any(token in value for token in ("debug", "troubleshoot")):
                return "debugging"
            if any(token in value for token in ("test", "validation")):
                return "validation"
            if any(token in value for token in ("document", "writing")):
                return "documentation"
            if any(token in value for token in ("code", "develop", "implement")):
                return "implementation"
        text = " ".join(value.casefold() for value in (session.app, session.title) if value)
        if any(token in text for token in ("test", "pytest", "validation")):
            return "validation"
        if any(token in text for token in ("debug", "traceback", "error")):
            return "debugging"
        if any(token in text for token in ("paper", "arxiv", "docs", "stackoverflow")):
            return "research"
        if any(token in text for token in ("code", "vscode", "pycharm", "terminal")):
            return "implementation"
        return "other"

    def detect(
        self,
        sessions: Iterable[ActivitySession],
        classifications: Optional[Dict[str, Classification]] = None,
    ) -> List[SessionPhase]:
        classifications = classifications or {}
        ordered = sorted(sessions, key=lambda item: (item.start, item.id))
        phases = []
        current = []
        current_type = None
        for session in ordered:
            phase_type = self._phase_type(session, classifications.get(session.id))
            if current and phase_type != current_type:
                phases.append(self._build(current_type, current, classifications))
                current = []
            current_type = phase_type
            current.append(session)
        if current:
            phases.append(self._build(current_type, current, classifications))
        return phases

    @staticmethod
    def _build(phase_type: str, sessions: List[ActivitySession], classifications: Dict[str, Classification]) -> SessionPhase:
        confidences = [classifications[item.id].confidence for item in sessions if item.id in classifications]
        return SessionPhase(
            phase_type=phase_type,
            start_time=min(item.start for item in sessions),
            end_time=max(item.end for item in sessions),
            activity_session_ids=tuple(item.id for item in sessions),
            confidence=sum(confidences) / len(confidences) if confidences else 0.3,
        )
