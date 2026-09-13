"""Generation 3 intent capture and goal alignment."""

from .engine import Intent, IntentEngine
from .alignment import (
    AlignmentResult,
    GoalAligner,
    RELATION_ALIGNED,
    RELATION_MISALIGNED,
    RELATION_UNKNOWN,
    RELEVANCE_HIGH,
    RELEVANCE_LOW,
    RELEVANCE_MEDIUM,
    RELEVANCE_NONE,
    RELEVANCE_UNKNOWN,
)

__all__ = [
    "AlignmentResult",
    "GoalAligner",
    "Intent",
    "IntentEngine",
    "RELATION_ALIGNED",
    "RELATION_MISALIGNED",
    "RELATION_UNKNOWN",
    "RELEVANCE_HIGH",
    "RELEVANCE_MEDIUM",
    "RELEVANCE_LOW",
    "RELEVANCE_NONE",
    "RELEVANCE_UNKNOWN",
]
