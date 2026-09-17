"""Evidence-bounded semantics: windows, provenance, freshness, change."""

from .evidence_window import (
    EvidenceWindow,
    observation_digest,
    parse_window,
)
from .change import (
    ChangeAssessment,
    ChangeSignal,
    EvidenceChangeDetector,
)
from .revision import (
    NO_VERDICT,
    ResolvedVerdict,
    RevisionDecision,
    RevisionOutcome,
    build_segments,
    resolve_for_interval,
)
from .segment import SemanticSegment
from .provenance import (
    Freshness,
    Provenance,
    SemanticState,
    assess_freshness,
    is_presentable_as_current,
    next_semantic_state,
)

__all__ = [
    "NO_VERDICT",
    "ChangeAssessment",
    "ChangeSignal",
    "EvidenceChangeDetector",
    "EvidenceWindow",
    "ResolvedVerdict",
    "RevisionDecision",
    "RevisionOutcome",
    "build_segments",
    "resolve_for_interval",
    "Freshness",
    "Provenance",
    "SemanticSegment",
    "SemanticState",
    "assess_freshness",
    "is_presentable_as_current",
    "next_semantic_state",
    "observation_digest",
    "parse_window",
]
