"""Real-time behavioral detection for Noema.

This package owns the near-real-time time scale, kept strictly separate
from the 20-minute semantic classification pipeline:

raw/session state
→ rolling behavioral features (:mod:`features`)
→ deterministic local candidate detector (:mod:`detector`)
→ fast model verification (compact feature summary only)
→ intervention policy
→ outcome observation

Nothing here classifies raw ActivityWatch rows, and nothing here writes
semantic classifications. AFK is always an operational state, never a
productivity verdict.
"""

from .detector import (
    CandidateDecision,
    DetectionState,
    DetectionTracker,
    DetectorConfig,
    DistractionCandidateDetector,
)
from .features import BehaviorWindow, BehaviorWindowConfig, build_behavior_windows
from .records import DetectionRecord, detection_id
from .verifier import FastModelVerifier, VerificationConfig, VerificationResult

__all__ = [
    "BehaviorWindow",
    "BehaviorWindowConfig",
    "CandidateDecision",
    "DetectionRecord",
    "DetectionState",
    "DetectionTracker",
    "DetectorConfig",
    "DistractionCandidateDetector",
    "FastModelVerifier",
    "VerificationConfig",
    "VerificationResult",
    "build_behavior_windows",
    "detection_id",
]
