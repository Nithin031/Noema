"""Inspectably deterministic alignment between a session and active intent.

Semantic V2 splits two ideas that were previously collapsed into one boolean:

* **Goal relevance** — how strongly the observed semantic activity relates to
  the active goal (``high`` / ``medium`` / ``low`` / ``none`` / ``unknown``).
* **Alignment** — the resulting relationship (``aligned`` / ``misaligned`` /
  ``unknown``).

The single most important rule here is that *uncertainty must never become
misalignment*. A session that has an active goal but only thin evidence is
``unknown``, not ``misaligned`` — so it can never masquerade as a distraction
downstream. Declaring an activity ``misaligned`` (clearly unrelated) requires a
confident, well-evidenced classification; positive relevance, by contrast, is
directly observable from token overlap and needs no such gate. ``productive``
never automatically means ``aligned`` and ``distractive`` never automatically
means ``misaligned``; both are judged only against the goal.
"""

from __future__ import annotations

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

# Three-valued alignment relation.
RELATION_ALIGNED = "aligned"
RELATION_MISALIGNED = "misaligned"
RELATION_UNKNOWN = "unknown"
_RELATIONS = {RELATION_ALIGNED, RELATION_MISALIGNED, RELATION_UNKNOWN}

# Goal-relevance grades (separate from the raw semantic category).
RELEVANCE_HIGH = "high"
RELEVANCE_MEDIUM = "medium"
RELEVANCE_LOW = "low"
RELEVANCE_NONE = "none"
RELEVANCE_UNKNOWN = "unknown"
_RELEVANCES = {
    RELEVANCE_HIGH, RELEVANCE_MEDIUM, RELEVANCE_LOW,
    RELEVANCE_NONE, RELEVANCE_UNKNOWN,
}

# A "clearly unrelated" (misaligned) verdict is only permitted when the
# classification is a confident, well-evidenced real verdict. Below these
# gates the relation stays ``unknown`` — never ``misaligned``.
_MISALIGN_MIN_CONFIDENCE = 0.5
_MISALIGN_GOOD_QUALITY = {"strong", "moderate"}

# Stop words carry no goal meaning. Left in, a shared "for"/"the" would fake a
# relationship between an activity and a goal and wrongly pull a genuinely
# unrelated session out of the misaligned/aligned decision into a borderline
# "unknown". They are removed from both sides before the overlap is computed.
_STOPWORDS = frozenset({
    "a", "an", "and", "the", "of", "for", "to", "in", "on", "at", "by",
    "with", "from", "into", "or", "as", "is", "are", "be", "my", "our",
    "your", "this", "that", "it", "i", "we", "you",
})


@dataclass(frozen=True)
class AlignmentResult:
    session_id: str
    intent_id: str
    aligned: bool
    score: float
    confidence: float
    reason: str
    # Semantic V2 dimensions. Defaulted so the historical six-positional
    # constructor (and old persisted rows) keep working; when omitted they
    # are derived conservatively in ``__post_init__``.
    relation: Optional[str] = None
    goal_relevance: Optional[str] = None
    alignment_confidence: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", str(self.session_id))
        object.__setattr__(self, "intent_id", str(self.intent_id))
        object.__setattr__(self, "aligned", bool(self.aligned))
        try:
            score = float(self.score)
        except (TypeError, ValueError):
            score = 0.0
        object.__setattr__(self, "score", min(1.0, max(0.0, score)))
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        object.__setattr__(self, "confidence", min(1.0, max(0.0, confidence)))
        # Relation: validate, or derive conservatively from the aligned flag.
        # Crucially, a bare ``aligned=False`` becomes ``unknown`` — never
        # ``misaligned`` — so legacy rows and hand-built results can never
        # invent a distraction. Only an explicit ``misaligned`` (produced by
        # GoalAligner under its evidence gate) can drive drift downstream.
        relation = str(self.relation).strip().lower() if self.relation else None
        if relation not in _RELATIONS:
            relation = RELATION_ALIGNED if self.aligned else RELATION_UNKNOWN
        object.__setattr__(self, "relation", relation)
        relevance = str(self.goal_relevance).strip().lower() if self.goal_relevance else None
        if relevance not in _RELEVANCES:
            if relation == RELATION_ALIGNED:
                relevance = RELEVANCE_HIGH if self.score >= 0.6 else RELEVANCE_MEDIUM
            elif relation == RELATION_MISALIGNED:
                relevance = RELEVANCE_NONE
            else:
                relevance = RELEVANCE_UNKNOWN
        object.__setattr__(self, "goal_relevance", relevance)
        if self.alignment_confidence is None:
            object.__setattr__(self, "alignment_confidence", self.confidence)
        else:
            try:
                ac = float(self.alignment_confidence)
            except (TypeError, ValueError):
                ac = self.confidence
            object.__setattr__(self, "alignment_confidence", min(1.0, max(0.0, ac)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "intent_id": self.intent_id,
            "aligned": self.aligned,
            "score": self.score,
            "confidence": self.confidence,
            "reason": self.reason,
            "relation": self.relation,
            "goal_relevance": self.goal_relevance,
            "alignment_confidence": self.alignment_confidence,
        }

    @classmethod
    def unknown(
        cls,
        session_id: str,
        intent_id: str = "",
        reason: str = "insufficient evidence to relate activity to the goal",
    ) -> "AlignmentResult":
        """An explicit 'we cannot tell' result — never a misalignment.

        Used when there is no active goal, no usable classification, or the
        goal-selection is ambiguous. Callers that simply omit alignment get
        the same downstream treatment, but this makes the intent explicit and
        persistable.
        """
        return cls(
            session_id=session_id,
            intent_id=intent_id,
            aligned=False,
            score=0.0,
            confidence=0.0,
            reason=reason,
            relation=RELATION_UNKNOWN,
            goal_relevance=RELEVANCE_UNKNOWN,
            alignment_confidence=0.0,
        )


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

    @staticmethod
    def _evidence_quality(
        session: ActivitySession, classification: Optional[Classification]
    ) -> str:
        quality = ""
        if classification is not None:
            quality = str(classification.evidence_quality or "").strip().lower()
        if not quality:
            session_quality = getattr(session, "evidence_quality", None)
            quality = str(getattr(session_quality, "value", session_quality) or "").strip().lower()
        return quality

    def align(
        self,
        session: ActivitySession,
        intent: Intent,
        classification: Optional[Classification] = None,
    ) -> AlignmentResult:
        if (not isinstance(session, ActivitySession) and not is_meaningful_session(session)) or not isinstance(intent, Intent):
            raise TypeError("GoalAligner expects an ActivitySession or MeaningfulSession and an Intent")

        intent_tokens = intent.search_tokens - _STOPWORDS
        session_tokens = self._session_tokens(session, classification) - _STOPWORDS
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

        # Evidence gate for declaring an activity clearly unrelated. Positive
        # relevance (aligned) only needs observable token overlap; a
        # *misaligned* verdict needs a confident, well-evidenced classification
        # so uncertainty is never converted into distraction.
        quality = self._evidence_quality(session, classification)
        try:
            classification_confidence = float(classification.confidence) if classification else 0.0
        except (TypeError, ValueError):
            classification_confidence = 0.0
        # Declaring an activity clearly unrelated demands a real, confident,
        # well-evidenced verdict. A pending or failed classification (never
        # "classified") can never clear this gate, so failed evidence cannot
        # be converted into a misalignment — only into "unknown".
        classified = (
            classification is not None
            and getattr(classification, "classification_status", None) == "classified"
        )
        strong_enough_to_reject = (
            classified
            and classification_confidence >= _MISALIGN_MIN_CONFIDENCE
            and quality in _MISALIGN_GOOD_QUALITY
        )

        if aligned:
            relation = RELATION_ALIGNED
            relevance = RELEVANCE_HIGH if score >= 0.6 else RELEVANCE_MEDIUM
        elif overlap_score > 0:
            # Some relationship, but below the alignment bar: genuinely
            # borderline. Not confident enough to call it either way.
            relation = RELATION_UNKNOWN
            relevance = RELEVANCE_LOW
        elif strong_enough_to_reject:
            # No overlap AND a confident, well-evidenced verdict: the activity
            # is understood and is not about the goal.
            relation = RELATION_MISALIGNED
            relevance = RELEVANCE_NONE
        else:
            # No overlap but thin evidence: we simply cannot tell.
            relation = RELATION_UNKNOWN
            relevance = RELEVANCE_UNKNOWN

        reason_parts = []
        if overlap:
            reason_parts.append("shared tokens: " + ", ".join(sorted(overlap)))
        if compatibility_bonus:
            reason_parts.append("activity type matches goal verb")
        if productivity_bonus:
            reason_parts.append("classified productive")
        if relation == RELATION_MISALIGNED:
            reason_parts.append("no goal overlap with a confident well-evidenced verdict")
        elif relation == RELATION_UNKNOWN and not overlap:
            reason_parts.append("insufficient evidence to relate activity to the goal")
        reason = "; ".join(reason_parts) if reason_parts else "no meaningful goal evidence"

        base_confidence = classification.confidence if classification else 0.0
        # Retained legacy ``confidence`` (how sure we are of the score).
        confidence = min(1.0, max(base_confidence, score))
        # Confidence in the alignment *decision* specifically.
        if relation == RELATION_ALIGNED:
            alignment_confidence = confidence
        elif relation == RELATION_MISALIGNED:
            alignment_confidence = classification_confidence
        else:
            alignment_confidence = min(0.5, base_confidence)

        return AlignmentResult(
            session_id=session.id,
            intent_id=intent.id,
            aligned=aligned,
            score=score,
            confidence=confidence,
            reason=reason,
            relation=relation,
            goal_relevance=relevance,
            alignment_confidence=alignment_confidence,
        )
