from noema.domain.behavior import BehaviorState, BehaviorEngine
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification
from noema.domain.intent import AlignmentResult
from noema.domain.sessions import ActivitySession


def make_session(start, app, domain=None, duration=60):
    return ActivitySession(
        start=start,
        end="2026-09-04T" + ("10:01:00Z" if start.endswith("10:00:00Z") else "10:11:00Z" if start.endswith("10:10:00Z") else "10:21:00Z"),
        device="laptop",
        app=app,
        domain=domain,
        event_keys=(start,),
    )


def test_behavior_engine_transitions_and_controls_actionability():
    focused = ActivitySession(
        start="2026-09-04T10:00:00Z", end="2026-09-04T10:01:00Z", device="laptop",
        app="VS Code", event_keys=("focused",),
    )
    distracted = ActivitySession(
        start="2026-09-04T10:10:00Z", end="2026-09-04T10:20:00Z", device="laptop",
        app="Chrome", domain="reddit.com", event_keys=("distracted",),
    )
    recovering = ActivitySession(
        start="2026-09-04T10:20:00Z", end="2026-09-04T10:21:00Z", device="laptop",
        app="VS Code", event_keys=("recovering",),
    )
    sessions = [focused, distracted, recovering]
    classifications = {
        focused.id: Classification(focused.id, "development", activity_type="coding", productivity="productive", confidence=.9, classification_status="classified"),
        distracted.id: Classification(distracted.id, "entertainment", activity_type="browsing", productivity="distracting", confidence=.9, classification_status="classified"),
        recovering.id: Classification(recovering.id, "development", activity_type="coding", productivity="productive", confidence=.9, classification_status="classified"),
    }
    alignments = {
        focused.id: AlignmentResult(focused.id, "intent", True, .9, .9, "aligned"),
        distracted.id: AlignmentResult(distracted.id, "intent", False, .0, .9, "off goal"),
        recovering.id: AlignmentResult(recovering.id, "intent", True, .9, .9, "aligned"),
    }

    observations = BehaviorEngine(min_actionable_distraction_seconds=300).evaluate(
        sessions, classifications, alignments
    )

    assert [item.state for item in observations] == [
        BehaviorState.FOCUSED, BehaviorState.DISTRACTED, BehaviorState.RECOVERING
    ]
    assert observations[0].actionable is False
    assert observations[1].actionable is True
    assert observations[2].previous_state == BehaviorState.DISTRACTED


def test_behavior_observations_persist():
    session = ActivitySession(
        start="2026-09-04T10:00:00Z", end="2026-09-04T10:01:00Z", device="laptop",
        app="Chrome", event_keys=("one",),
    )
    observation = BehaviorEngine().evaluate([session])[0]
    store = SQLiteStore()
    assert store.insert_behavior_observation(observation) is True
    assert store.insert_behavior_observation(observation) is False
    found = store.query_behavior_observations()
    assert found[0].state == BehaviorState.NORMAL
    store.close()


def test_behavior_ignores_pending_classifications():
    session = make_session("2026-09-04T10:00:00Z", "Chrome", "reddit.com")
    pending = Classification(
        session.id, "entertainment", activity_type="browsing",
        productivity="distracting", confidence=.9,
    )
    assert pending.classification_status == "pending"
    observation = BehaviorEngine().evaluate([session], {session.id: pending})[0]
    assert observation.state == BehaviorState.NORMAL
    assert observation.actionable is False


def test_behavior_ignores_failed_classifications():
    session = make_session("2026-09-04T10:00:00Z", "Chrome", "reddit.com")
    failed = Classification(
        session.id, "entertainment", activity_type="browsing",
        productivity="distracting", confidence=.9,
        classification_status="classification_failed",
    )
    observation = BehaviorEngine().evaluate([session], {session.id: failed})[0]
    assert observation.state == BehaviorState.NORMAL
    assert observation.actionable is False
