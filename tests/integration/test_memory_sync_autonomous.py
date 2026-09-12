from datetime import datetime, timezone

from noema.application.autonomous import AutonomousAgent
from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.application.classification import Classification
from noema.domain.intervention import Intervention, InterventionMode, InterventionStatus
from noema.domain.memory import MemoryEngine, Personalizer
from noema.domain.outcomes import InterventionOutcome, RecoveryStatus
from noema.domain.sessions import ActivitySession
from noema.infrastructure.sync import SyncEnvelope, UnifiedTimeline


def session(session_id, start, domain):
    return ActivitySession(
        start=start,
        end="2026-09-04T10:10:00Z" if start.endswith("10:00:00Z") else "2026-09-04T11:10:00Z",
        device="phone" if domain == "instagram.com" else "laptop",
        app="Chrome",
        domain=domain,
        event_keys=(session_id,),
    )


def test_memory_validation_and_personalization():
    first = session("one", "2026-09-04T10:00:00Z", "reddit.com")
    second = session("two", "2026-09-04T11:00:00Z", "reddit.com")
    classifications = {
        first.id: Classification(first.id, "entertainment", topic="reddit", productivity="distracting"),
        second.id: Classification(second.id, "entertainment", topic="reddit", productivity="distracting"),
    }
    observations = {
        first.id: BehaviorObservation(first.id, BehaviorState.DISTRACTED, first.start, .9, .9, True, "distracted"),
        second.id: BehaviorObservation(second.id, BehaviorState.DISTRACTED, second.start, .9, .9, True, "distracted"),
    }
    memories = MemoryEngine().derive_distraction_patterns([first, second], classifications, observations)
    assert len(memories) == 1
    assert memories[0].evidence_count == 2

    outcomes = [
        InterventionOutcome("a", "2026-09-04T09:00:00Z", RecoveryStatus.RECOVERED, intervention_type="NOTIFICATION"),
        InterventionOutcome("b", "2026-09-04T09:00:00Z", RecoveryStatus.PENDING, intervention_type="MEME"),
    ]
    profile = Personalizer().build_profile(outcomes)
    assert profile.preferred_intervention == "NOTIFICATION"
    assert profile.recovered_rate_by_intervention["NOTIFICATION"] == 1.0


def test_cross_device_sync_round_trip_and_timeline_deduplicates():
    from noema.domain.activity import ActivityEvent
    one = ActivityEvent("2026-09-04T10:00:00Z", 60, device="laptop", app="Code", bucket_id="b", source_event_id="1")
    two = ActivityEvent("2026-09-04T10:01:00Z", 60, device="phone", app="Instagram", domain="instagram.com", bucket_id="p", source_event_id="2")
    envelope = SyncEnvelope("mixed", (one, two))
    restored = SyncEnvelope.from_json(envelope.to_json())
    timeline = UnifiedTimeline.from_envelopes([envelope, restored])
    assert len(timeline.events) == 2
    assert [event.device for event in timeline.events] == ["laptop", "phone"]
    assert len(timeline.query(device="phone")) == 1


def test_autonomous_agent_is_recommendation_first():
    distracted = BehaviorObservation(
        "session", BehaviorState.DISTRACTED, "2026-09-04T10:00:00Z", .9, .9, True, "distracted"
    )
    focused = BehaviorObservation(
        "session", BehaviorState.FOCUSED, "2026-09-04T10:00:00Z", .9, 0, False, "focused"
    )
    agent = AutonomousAgent()
    assert agent.recommend(distracted).action == "intervene"
    assert agent.recommend(distracted).requires_confirmation is True
    assert agent.recommend(focused).action == "continue"
    assert agent.recommend(focused).requires_confirmation is False
