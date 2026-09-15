"""V3 outcome attribution: direct vs ambient vs none, response linkage."""

from datetime import datetime, timedelta, timezone

from noema.api import NoemaService
from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.domain.sessions import ActivitySession
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
INT_NOW = datetime(2026, 9, 9, 10, 35, tzinfo=timezone.utc)


def _distracted_setup(store):
    session = ActivitySession(
        start="2026-09-09T10:00:00Z", end="2026-09-09T10:30:00Z",
        device="laptop", app="Firefox", domain="youtube.com",
        title="cat videos", event_keys=("e1",))
    store.insert_session(session)
    store.insert_behavior_observations([BehaviorObservation(
        session_id=session.id, state=BehaviorState.DISTRACTED,
        started_at=session.start, confidence=0.9, distraction_score=0.9,
        actionable=True, reason="test")])
    return session


def _recovery_session(store, tag):
    session = ActivitySession(
        start="2026-09-09T11:00:00Z", end="2026-09-09T11:05:00Z",
        device="laptop", app="Code", title="work.py",
        event_keys=(tag,))
    store.insert_session(session)
    return session


def _productive_classification(store, session):
    from noema.application.classification import Classification
    store.insert_classification(Classification(
        session_id=session.id, category="productive", productivity="productive",
        confidence=0.9, classification_status="classified", provider="test",
        model="test", source="test", evidence_quality="strong"))


def test_lockin_then_recovery_is_direct_and_counts():
    store = SQLiteStore()
    try:
        service = NoemaService(ActivityWatchAdapter(), store)
        session = _distracted_setup(store)
        intervention = service.consider_intervention(session.id, execute=False, now=INT_NOW)
        response_id = intervention.payload.get("response_id")
        assert response_id
        service.record_intervention_action(
            intervention.id, "DISPLAYED", state="DISPLAYED")
        service.record_intervention_action(
            intervention.id, "LOCK_IN", state="INTERACTED")
        recovery = _recovery_session(store, "rec-1")
        _productive_classification(store, recovery)
        outcome = service.measure_outcome(
            intervention.id, now=NOW + timedelta(minutes=30))
        assert outcome.recovery_status.value == "RECOVERED"
        assert outcome.attribution == "direct"
        assert outcome.user_action == "lock_in"
        assert outcome.response_id == response_id
        assert outcome.delivery_state is None  # never delivered in this fixture
        assert store.get_response(response_id).recovery_count == 1
    finally:
        store.close()


def test_recovery_without_interaction_is_ambient():
    store = SQLiteStore()
    try:
        service = NoemaService(ActivityWatchAdapter(), store)
        session = _distracted_setup(store)
        intervention = service.consider_intervention(session.id, execute=False, now=INT_NOW)
        recovery = _recovery_session(store, "rec-2")
        _productive_classification(store, recovery)
        outcome = service.measure_outcome(
            intervention.id, now=NOW + timedelta(minutes=30))
        assert outcome.recovery_status.value == "RECOVERED"
        assert outcome.attribution == "ambient"
        assert outcome.user_action is None
    finally:
        store.close()


def test_dismiss_then_recovery_is_not_claimed():
    store = SQLiteStore()
    try:
        service = NoemaService(ActivityWatchAdapter(), store)
        session = _distracted_setup(store)
        intervention = service.consider_intervention(session.id, execute=False, now=INT_NOW)
        response_id = intervention.payload.get("response_id")
        service.record_intervention_action(
            intervention.id, "DISMISS", state="INTERACTED")
        recovery = _recovery_session(store, "rec-3")
        _productive_classification(store, recovery)
        outcome = service.measure_outcome(
            intervention.id, now=NOW + timedelta(minutes=30))
        assert outcome.recovery_status.value == "RECOVERED"
        assert outcome.attribution == "none"
        if response_id:
            assert store.get_response(response_id).recovery_count == 0
    finally:
        store.close()
