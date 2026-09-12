"""Noema: local-first Personal Behavioral Intelligence.

The package reads compatible local telemetry through a read-only adapter and
stores its own privacy-filtered derived representation.
"""

from .infrastructure.activity_sources.activitywatch import ActivityWatchAdapter, ActivityWatchClient
from .domain.activity import ActivityEvent
from .domain.behavior import BehaviorObservation, BehaviorState, BehaviorEngine
from .infrastructure.browser import BrowserBridge, BrowserEvent, BrowserRegistration, DesktopNotificationHandler
from .infrastructure.database import SQLiteStore, StoredActivityEvent
from .domain.normalization import EventNormalizer
from .infrastructure.providers import (
    GeminiProvider,
    ModelProvider,
    OllamaProvider,
    ProviderError,
)
from .application.classification import (
    Classification,
    Classifier,
)
from .domain.intent import AlignmentResult, GoalAligner, Intent, IntentEngine
from .domain.intervention import Intervention, InterventionActionPayload, InterventionEngine, InterventionMode, InterventionPolicy, InterventionStatus
from .domain.meme import MemeIntelligence, MemePayload, MemeRenderer
from .domain.outcomes import InterventionOutcome, OutcomeTracker, RecoveryStatus
from .infrastructure.sync import SyncEnvelope, UnifiedTimeline
from .application.autonomous import AgentRecommendation, AutonomousAgent, AutonomousCycle
from .domain.meaningful import (
    ContinuityScore, ContinuityScorer, MeaningfulSession, MeaningfulSessionEngine,
    MeaningfulSessionGrouper, MeaningfulSessionOutcome, MeaningfulSessionPersistence,
    MeaningfulSessionStatus, MeaningfulSessionSummarizer, SessionPhase,
)
from .domain.memory import Memory, MemoryEngine, MemoryKind, PersonalizationProfile, Personalizer
from .infrastructure.ollama import OllamaClient, OllamaError
from .domain.privacy import PrivacyDecision, PrivacyFilter, PrivacyPolicy
from .domain.sessions import ActivitySession, Sessionizer
from .runtime import NoemaDaemon, DaemonConfig

__all__ = [
    "ActivityWatchAdapter",
    "ActivityWatchClient",
    "ActivitySession",
    "AlignmentResult",
    "BehaviorObservation",
    "BehaviorState",
    "BehaviorEngine",
    "BrowserBridge",
    "BrowserEvent",
    "BrowserRegistration",
    "DesktopNotificationHandler",
    "EventNormalizer",
    "GoalAligner",
    "Intent",
    "IntentEngine",
    "Intervention",
    "InterventionActionPayload",
    "InterventionEngine",
    "InterventionMode",
    "InterventionPolicy",
    "InterventionStatus",
    "MemeIntelligence",
    "MemePayload",
    "MemeRenderer",
    "InterventionOutcome",
    "OutcomeTracker",
    "RecoveryStatus",
    "Memory",
    "MemoryEngine",
    "MemoryKind",
    "PersonalizationProfile",
    "Personalizer",
    "SyncEnvelope",
    "UnifiedTimeline",
    "AgentRecommendation",
    "AutonomousAgent",
    "AutonomousCycle",
    "ContinuityScore",
    "ContinuityScorer",
    "MeaningfulSession",
    "MeaningfulSessionEngine",
    "MeaningfulSessionGrouper",
    "MeaningfulSessionOutcome",
    "MeaningfulSessionPersistence",
    "MeaningfulSessionStatus",
    "MeaningfulSessionSummarizer",
    "SessionPhase",
    "OllamaClient",
    "OllamaError",
    "OllamaProvider",
    "GeminiProvider",
    "ModelProvider",
    "ProviderError",
    "ActivityEvent",
    "PrivacyDecision",
    "PrivacyFilter",
    "PrivacyPolicy",
    "SQLiteStore",
    "Sessionizer",
    "Classification",
    "Classifier",
    "StoredActivityEvent",
    "NoemaDaemon",
    "DaemonConfig",
]

__version__ = "0.14.0"
