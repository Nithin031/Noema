from datetime import datetime, timezone

from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification
from noema.domain.intervention import (
    InterventionEngine,
    InterventionMode,
    InterventionPolicy,
    InterventionStatus,
)
from noema.domain.intent import Intent
from noema.domain.sessions import ActivitySession


def context():
    session = ActivitySession(
        start="2026-09-04T10:00:00Z",
        end="2026-09-04T10:10:00Z",
        device="laptop",
        app="Chrome",
        domain="reddit.com",
        event_keys=("distraction",),
        browser="firefox",
        browser_window_id="17",
        browser_tab_id="42",
    )
    observation = BehaviorObservation(
        session_id=session.id,
        state=BehaviorState.DISTRACTED,
        started_at=session.start,
        confidence=.9,
        distraction_score=.9,
        actionable=True,
        reason="distracting classification",
    )
    intent = Intent(
        text="Implement PPO reward shaping",
        goal="Implement PPO reward shaping",
        created_at=datetime(2026, 9, 4, 9, tzinfo=timezone.utc),
    )
    classification = Classification(
        session_id=session.id,
        category="entertainment",
        topic="reddit",
        activity_type="browsing",
        productivity="distracting",
        confidence=.9,
    )
    return session, observation, intent, classification


def test_intervention_modes_are_policy_gated_and_executable():
    session, observation, intent, classification = context()
    now = datetime(2026, 9, 4, 10, 10, tzinfo=timezone.utc)
    for mode, expected_key in (
        (InterventionMode.NOTIFICATION, "message"),
        (InterventionMode.HOLDOUT, "duration_seconds"),
        (InterventionMode.MEME, "template"),
    ):
        engine = InterventionEngine(InterventionPolicy(mode=mode, dry_run=True))
        planned = engine.consider(observation, session, intent, classification, now=now)
        assert planned.status == InterventionStatus.PLANNED
        assert planned.mode == mode
        assert expected_key in planned.payload
        executed = engine.execute(planned, now=now)
        assert executed.status == InterventionStatus.EXECUTED
        assert executed.payload["dry_run"] is True


def test_intervention_cooldown_and_handler_contract():
    session, observation, intent, classification = context()
    now = datetime(2026, 9, 4, 10, 10, tzinfo=timezone.utc)
    engine = InterventionEngine(
        InterventionPolicy(mode=InterventionMode.NOTIFICATION, dry_run=False, cooldown_seconds=900)
    )
    planned = engine.consider(observation, session, intent, classification, now=now)
    handler_calls = []
    executed = engine.execute(planned, handler=handler_calls.append, now=now)
    assert executed.status == InterventionStatus.EXECUTED
    assert handler_calls == [planned]

    skipped = engine.consider(
        observation, session, intent, classification, recent=[executed], now=now
    )
    assert skipped.status == InterventionStatus.SKIPPED
    assert "cooldown" in skipped.reason

    store = SQLiteStore()
    assert store.insert_intervention(executed) is True
    assert store.query_interventions(session_id=session.id)[0].status == InterventionStatus.EXECUTED
    store.close()


def test_intervention_exposes_explicit_exact_tab_action_contract():
    session, observation, intent, classification = context()
    planned = InterventionEngine(InterventionPolicy(mode=InterventionMode.MEME)).consider(
        observation, session, intent, classification,
        now=datetime(2026, 9, 4, 10, 10, tzinfo=timezone.utc),
    )

    action = planned.action.to_dict()
    assert action["intervention_id"] == planned.id
    assert action["type"] == "MEME"
    assert action["target"] == {
        "device": "laptop", "browser": "firefox", "window_id": "17", "tab_id": "42"
    }
    assert action["meme"]["top"] == "Implement PPO reward shaping"
    assert "LOCK_IN" in action["actions"]
    assert planned.to_dict()["target"]["tab_id"] == "42"
