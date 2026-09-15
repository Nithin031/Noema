"""V3 curated-response binding: consider_intervention resolves registry artifacts."""

from datetime import datetime, timezone

from noema.api import NoemaService
from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.domain.sessions import ActivitySession
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore


def _service():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    store.seed_responses()
    return store, service


def _distracted_setup(store, service):
    session = ActivitySession(
        start="2026-09-09T10:00:00Z", end="2026-09-09T10:30:00Z",
        device="laptop", app="Firefox", domain="youtube.com",
        title="cat videos", event_keys=("e1",))
    store.insert_session(session)
    observation = BehaviorObservation(
        session_id=session.id, state=BehaviorState.DISTRACTED,
        started_at=session.start, confidence=0.9, distraction_score=0.9,
        actionable=True, reason="test")
    store.insert_behavior_observations([observation])
    return session


def test_curated_response_bound_to_planned_intervention():
    store, service = _service()
    try:
        session = _distracted_setup(store, service)
        intervention = service.consider_intervention(session.id, execute=False)
        assert intervention.status.value == "PLANNED"
        # Default policy mode is NOTIFICATION → TEXT/NUDGE kinds serve.
        assert intervention.payload.get("response_id")
        assert intervention.payload.get("response_kind") in {"TEXT", "NUDGE"}
        assert "LOCK_IN" in intervention.payload.get("actions", ())
        assert "BREAK_5MIN" in intervention.payload.get("actions", ())
        assert "INTENTIONAL" in intervention.payload.get("actions", ())
        actions = [item["action"] for item in
                   store.query_intervention_actions(intervention.id, limit=100)]
        assert "triggered" in actions
        assert "response_selected" in actions
    finally:
        store.close()


def test_registry_seeds_automatically_on_first_consider():
    store = SQLiteStore()
    try:
        service = NoemaService(ActivityWatchAdapter(), store)
        assert store.query_responses() == []
        session = ActivitySession(
            start="2026-09-09T10:00:00Z", end="2026-09-09T10:30:00Z",
            device="laptop", app="Firefox", domain="youtube.com",
            title="cat videos", event_keys=("e1",))
        store.insert_session(session)
        observation = BehaviorObservation(
            session_id=session.id, state=BehaviorState.DISTRACTED,
            started_at=session.start, confidence=0.9, distraction_score=0.9,
            actionable=True, reason="test")
        store.insert_behavior_observations([observation])
        intervention = service.consider_intervention(session.id, execute=False)
        assert intervention.status.value == "PLANNED"
        # Lazy seeding populated the registry; selection served.
        assert store.query_responses()
        assert intervention.payload.get("response_id")
    finally:
        store.close()


def test_reasoning_class_drives_selection():
    from noema.application.realtime.reasoning import ReasoningResult
    store, service = _service()
    try:
        session = _distracted_setup(store, service)
        reasoning = ReasoningResult(
            state="DISTRACTED", confidence=0.85, severity=2,
            evidence_quality="strong", reason="sustained browsing",
            intervention_worthwhile=True,
            recommended_response_class="MEME", evidence_gaps=(),
            provider="gemini", model="m", latency_ms=10.0, model_calls=1,
            attempts=(("gemini", "m"),), skipped=False)
        intervention = service.consider_intervention(
            session.id, execute=False, reasoning=reasoning)
        # MEME class under the default NOTIFICATION policy mode has no
        # servable kind → heuristic fallback, plans untouched.
        assert intervention.status.value == "PLANNED"
        assert not intervention.payload.get("response_id")
    finally:
        store.close()


def test_response_shown_counter_only_on_delivery():
    store, service = _service()
    try:
        session = _distracted_setup(store, service)
        intervention = service.consider_intervention(session.id, execute=False)
        response_id = intervention.payload.get("response_id")
        assert response_id
        # Planned but not delivered: counter untouched.
        assert store.get_response(response_id).times_shown == 0
    finally:
        store.close()
