"""V3 break mode + typed user actions: suppression, expiry, semantics."""

from datetime import datetime, timedelta, timezone

from noema.api import NoemaService
from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.domain.sessions import ActivitySession
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _service():
    store = SQLiteStore()
    return store, NoemaService(ActivityWatchAdapter(), store)


def _distracted(store):
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


def test_break_lifecycle_start_status_expiry():
    store, service = _service()
    try:
        assert service.break_status(now=NOW)["active"] is False
        started = service.start_break(minutes=5, now=NOW)
        assert started["active"] is True
        assert started["remaining_seconds"] <= 300.0
        assert service.break_status(now=NOW + timedelta(minutes=4))["active"] is True
        ended = service.break_status(now=NOW + timedelta(minutes=6))
        assert ended["active"] is False
        assert ended.get("just_ended") is True
        # Lazily cleared: keys gone afterwards.
        assert service.break_status(now=NOW + timedelta(minutes=6))["active"] is False
        # Duration capped at 30 minutes.
        capped = service.start_break(minutes=600, now=NOW)
        assert capped["remaining_seconds"] <= 30 * 60.0
        service.clear_break()
        assert service.break_status(now=NOW)["active"] is False
    finally:
        store.close()


def test_break_suppresses_semantic_lane_intervention():
    store, service = _service()
    try:
        session = _distracted(store)
        service.start_break(minutes=5, now=NOW)
        intervention = service.consider_intervention(session.id, now=NOW)
        assert intervention.status.value == "SKIPPED"
        assert "break" in intervention.reason
        assert store.query_interventions(limit=100) != []
        # After expiry the same session is actionable again.
        later = service.consider_intervention(
            session.id, now=NOW + timedelta(minutes=10))
        assert later.status.value == "PLANNED"
    finally:
        store.close()


def test_break_suppresses_realtime_tick_without_model_call():
    from test_realtime_pipeline import (
        ConfirmLeg,
        classify_all,
        distracting_payload,
        make_service,
        make_sessions,
    )
    from noema.application.realtime import FastModelVerifier

    store, service = make_service(distracting_payload())
    try:
        sessions = make_sessions("d", 25)
        for session in sessions:
            store.insert_meaningful_session(session)
        classify_all(service, store, sessions)
        leg = ConfirmLeg()
        service.fast_verifier = FastModelVerifier(fast_openrouter=leg)
        service.start_break(minutes=5, now=NOW)
        result = service.evaluate_realtime(now=NOW)
        assert result["action"] == "none (break active)"
        assert result["break"]["active"] is True
        assert leg.calls == 0
        assert store.query_interventions(limit=100000) == []
        assert store.query_detections(limit=100) == []
    finally:
        store.close()


def test_typed_actions_break_intentional_lockin():
    store, service = _service()
    try:
        session = _distracted(store)
        intervention = service.consider_intervention(session.id, execute=False)
        iid = intervention.id
        # 5 MIN BREAK starts a break.
        service.record_intervention_action(iid, "BREAK_5MIN", state="INTERACTED",
                                           metadata={"minutes": 5})
        assert service.break_status()["active"] is True
        actions = [item["action"] for item in
                   store.query_intervention_actions(iid, limit=100)]
        assert "break_started" in actions
        # LOCK IN ends the break.
        service.record_intervention_action(iid, "LOCK_IN", state="INTERACTED")
        assert service.break_status()["active"] is False
        # THIS IS INTENTIONAL records interpretation-negative feedback.
        service.record_intervention_action(iid, "INTENTIONAL", state="INTERACTED")
        feedback = store.query_intervention_feedback(iid)
        by_type = {row["feedback_type"]: row["value"] for row in feedback}
        assert by_type.get("INTERPRETATION") == "WRONG"
    finally:
        store.close()
