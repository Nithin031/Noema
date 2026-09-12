"""Confidence and evidence calculation for meaningful sessions."""

from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from noema.application.classification import Classification
from noema.domain.intent import AlignmentResult, Intent
from noema.domain.meaningful.models import EvidenceQuality
from noema.domain.sessions import ActivitySession


class ConfidenceCalculator:
    def calculate(
        self,
        sessions: Iterable[ActivitySession],
        intent: Optional[Intent],
        classifications: Mapping[str, Classification],
        alignments: Mapping[str, AlignmentResult],
    ) -> Tuple[float, Tuple[str, ...], float, float, float, int]:
        ordered = list(sessions)
        total = sum(item.duration for item in ordered) or 1.0
        productive = sum(item.duration for item in ordered if classifications.get(item.id) and classifications[item.id].productivity == "productive")
        distracting = sum(item.duration for item in ordered if classifications.get(item.id) and classifications[item.id].productivity == "distracting")
        alignment_scores = [item.score for item in (alignments.get(session.id) for session in ordered) if item]
        alignment = sum(alignment_scores) / len(alignment_scores) if alignment_scores else 0.0
        focus = productive / total
        distraction = distracting / total
        context_switches = sum(
            1 for left, right in zip(ordered, ordered[1:])
            if (left.app, left.domain, left.device) != (right.app, right.domain, right.device)
        )
        evidence: List[str] = []
        if intent:
            evidence.append("explicit intent")
        projects = [item.project for item in classifications.values() if item.project]
        topics = [item.topic for item in classifications.values() if item.topic]
        if projects:
            evidence.append("semantic project evidence")
        if topics:
            evidence.append("semantic topic evidence")
        if alignment_scores:
            evidence.append("intent alignment evidence")
        for session in ordered:
            if session.title:
                evidence.append("observable title: {}".format(session.title))
        base = 0.25 + (0.25 if intent else 0.0) + min(0.2, len(projects) * 0.05) + min(0.15, len(topics) * 0.03) + min(0.15, alignment)
        return min(1.0, base), tuple(dict.fromkeys(evidence)), focus, distraction, alignment, context_switches

    @staticmethod
    def assess_evidence_quality(sessions: Iterable[ActivitySession]) -> EvidenceQuality:
        """Deterministically assess evidence quality from observation signals.

        This is NEVER determined by the model. The classifier must not inflate
        or fabricate evidence quality. Assessment rules:
        - STRONG: title + domain agree (e.g. documentation page on docs domain)
        - MODERATE: title + app agree, but domain/URL missing or generic
        - WEAK: only app name or only title available; domain unknown
        - ABSENT: no meaningful signal beyond process name
        """
        ordered = list(sessions)
        has_domain = any(s.domain for s in ordered)
        has_url = any(s.url for s in ordered)
        has_title = any(s.title for s in ordered)
        has_app = any(s.app for s in ordered)
        # Count how many sessions have browser-type app names
        browser_titles = {
            "mozilla firefox", "google chrome", "microsoft edge",
            "brave", "opera", "vivaldi", "arc",
        }
        is_browser = any(
            (s.app or "").lower() in browser_titles
            or any(marker in (s.title or "").lower() for marker in browser_titles)
            for s in ordered
        )
        if has_domain and has_title:
            return EvidenceQuality.STRONG
        if has_url and has_title:
            return EvidenceQuality.STRONG
        if has_title and has_app and not is_browser:
            # Non-browser app with title is strong enough
            return EvidenceQuality.STRONG
        if is_browser and has_title and not has_domain and not has_url:
            # Browser without domain — title only, weak
            return EvidenceQuality.WEAK
        if has_title and has_app:
            return EvidenceQuality.MODERATE
        if has_title:
            return EvidenceQuality.MODERATE
        if has_app:
            return EvidenceQuality.WEAK
        return EvidenceQuality.ABSENT
