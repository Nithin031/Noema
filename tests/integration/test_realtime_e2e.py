"""End-to-end: distraction → detection → intervention → outcome.

Drives the real pipeline (ingest → sessions → meaningful → classify →
behavior → realtime evaluate → outcome) with a fake ActivityWatch and a
fake fast leg. No network, no real providers.
"""

from datetime import datetime, timedelta, timezone


from noema.api import NoemaService
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classifier
from noema.domain.meme import MemeIntelligence
from noema.infrastructure.ollama import OllamaError
from noema.application.realtime import FastModelVerifier

from test_realtime_pipeline import (
    ConfirmLeg,
    CountingClient,
    distracting_payload,
    productive_payload,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class ScriptedAW:
    """Fake ActivityWatch serving scripted heartbeats per bucket poll."""

    def __init__(self, app, title, start, minutes, tag="e2e"):
        self.app = app
        self.title = title
        self.start = start
        self.minutes = minutes
        self.tag = tag

    def list_buckets(self):
        return {"window_laptop": {"hostname": "laptop"}}

    def get_events(self, bucket_id, start=None, end=None):
        return [{
            "id": "{}-{}".format(self.tag, index),
            "timestamp": (self.start + timedelta(seconds=60 * index)).isoformat().replace(
                "+00:00", "Z"),
            "duration": 60,
            "data": {"app": self.app, "title": self.title},
        } for index in range(self.minutes)]


def drive(service, now):
    """Run the production stages in production order."""
    service.ingest_telemetry(
        start=now - timedelta(hours=2), end=now)
    service.sessionize_stored_events(
        start=now - timedelta(hours=2), end=now, limit=100000)
    service.build_meaningful_sessions(
        start=now - timedelta(hours=2), end=now, limit=100000,
        sequence_terminated=False)
    sessions = service.store.query_meaningful_sessions(limit=100000)
    service.classify_meaningful_sessions(sessions, max_retries=5)
    service.evaluate_stored_behavior(limit=100000)
    return service.evaluate_realtime(now=now, execute_intervention=True)


def test_distraction_to_intervention_to_recovery():
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(ScriptedAW("Firefox", "YouTube", NOW - timedelta(minutes=25), 25)),
        store,
        classifier=Classifier(client=CountingClient(payload=distracting_payload())),
    )
    service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())

    result = drive(service, NOW)

    assert result["state"] == "INTERVENTION_COOLDOWN"
    interventions = store.query_interventions(limit=100000)
    assert len(interventions) == 1
    intervention = interventions[0]

    # MANDATORY TEST 10: productive sessions after the intervention →
    # RECOVERED outcome linked to the same intervention.
    service.adapter = ActivityWatchAdapter(
        ScriptedAW("VS Code", "reward.py", NOW, 10, tag="recovery"))
    service.classifier = Classifier(
        client=CountingClient(payload=productive_payload()))
    service.ingest_telemetry(start=NOW, end=NOW + timedelta(minutes=10))
    service.sessionize_stored_events(
        start=NOW, end=NOW + timedelta(minutes=10), limit=100000)
    service.build_meaningful_sessions(
        start=NOW, end=NOW + timedelta(minutes=10), limit=100000,
        sequence_terminated=False)
    fresh = [item for item in service.store.query_meaningful_sessions(limit=100000)
             if item.start_time >= NOW]
    assert fresh
    service.classify_meaningful_sessions(fresh, max_retries=5)
    outcome = service.measure_outcome(
        intervention.id, now=NOW + timedelta(minutes=30))
    assert outcome.recovery_status.value == "RECOVERED"
    assert outcome.recovery_session_id in {item.id for item in fresh}
    assert outcome.intervention_id == intervention.id
    store.close()


def test_continued_distraction_is_not_recovery():
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(ScriptedAW("Firefox", "YouTube", NOW - timedelta(minutes=25), 25)),
        store,
        classifier=Classifier(client=CountingClient(payload=distracting_payload())),
    )
    service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())
    result = drive(service, NOW)
    assert result["state"] == "INTERVENTION_COOLDOWN"
    intervention = store.query_interventions(limit=100000)[0]

    # MANDATORY TEST 11: only more distraction follows, measured after the
    # pending window → NOT_RECOVERED (never PENDING forever, never fake).
    service.adapter = ActivityWatchAdapter(
        ScriptedAW("Firefox", "YouTube", NOW, 10, tag="relapse"))
    service.ingest_telemetry(start=NOW, end=NOW + timedelta(minutes=10))
    service.sessionize_stored_events(
        start=NOW, end=NOW + timedelta(minutes=10), limit=100000)
    service.build_meaningful_sessions(
        start=NOW, end=NOW + timedelta(minutes=10), limit=100000,
        sequence_terminated=False)
    fresh = [item for item in service.store.query_meaningful_sessions(limit=100000)
             if item.start_time >= NOW]
    service.classify_meaningful_sessions(fresh, max_retries=5)
    outcome = service.measure_outcome(
        intervention.id, now=NOW + timedelta(hours=2))
    assert outcome.recovery_status.value == "NOT_RECOVERED"
    store.close()


def test_failing_verifier_never_breaks_evaluation():
    from noema.infrastructure.providers import ProviderError

    class DownLeg(ConfirmLeg):
        def complete_json(self, prompt, max_tokens=300):
            self.calls += 1
            raise ProviderError("connection refused")

    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(ScriptedAW("Firefox", "YouTube", NOW - timedelta(minutes=25), 25)),
        store,
        classifier=Classifier(client=CountingClient(payload=distracting_payload())),
    )
    service.fast_verifier = FastModelVerifier(fast_openrouter=DownLeg())

    result = drive(service, NOW)

    assert result["state"] == "CANDIDATE"
    assert result["verification"]["concerning"] is False
    assert result["intervention"] is None
    assert store.query_interventions(limit=100000) == []
    store.close()


# MANDATORY TEST 12: meme generation failure degrades to text, never blocks.
def test_meme_failure_falls_back_to_text():
    class DeadClient:
        model = "dead"

        def generate_json(self, prompt):
            raise OllamaError("ollama is down")

    from noema.domain.behavior import BehaviorObservation, BehaviorState
    from noema.domain.sessions import ActivitySession

    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(), store, meme_intelligence=MemeIntelligence(client=DeadClient()))
    session = ActivitySession(
        start="2026-09-09T10:00:00Z", end="2026-09-09T10:10:00Z",
        device="laptop", app="Firefox", domain="youtube.com",
        event_keys=("e1",))
    store.insert_session(session)
    observation = BehaviorObservation(
        session_id=session.id, state=BehaviorState.DISTRACTED,
        started_at=session.start, confidence=0.9, distraction_score=0.9,
        actionable=True, reason="test")
    store.insert_behavior_observations([observation])

    meme = service.generate_meme(session.id)

    assert meme.provider == "heuristic"
    assert meme.top and meme.bottom
    assert store.query_memes(session_id=session.id)
    store.close()


def test_tracker_state_survives_service_restart():
    store = SQLiteStore()
    first = NoemaService(
        ActivityWatchAdapter(ScriptedAW("Firefox", "YouTube", NOW - timedelta(minutes=25), 25)),
        store,
        classifier=Classifier(client=CountingClient(payload=distracting_payload())),
    )
    first.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())
    drive(first, NOW)

    restarted = NoemaService(ActivityWatchAdapter(), store)
    status = restarted.realtime_status()
    assert status["tracker"]["state"] == "INTERVENTION_COOLDOWN"
    assert status["last_detection"] is not None
    store.close()
