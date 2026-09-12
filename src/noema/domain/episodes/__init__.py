"""Episode semantics: evidence quality independent of model confidence.

An episode (a meaningful session plus its raw sessions) represents the
semantic unit the classifier describes. EvidenceQuality is orthogonal to
Classification.confidence: it measures how much usable evidence the system
actually had, not how sure the model felt. It is deterministic from already
known facts (duration, fragment count, browser-context availability).
"""

from .models import EvidenceQuality, EvidenceScore, assess_evidence

__all__ = ["EvidenceQuality", "EvidenceScore", "assess_evidence"]
