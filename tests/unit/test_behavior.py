from datetime import datetime, timezone

from noema.domain.behavior import BehaviorState, BehaviorEngine
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification
from noema.domain.intent import AlignmentResult
from noema.domain.sessions import ActivitySession
from noema.api import NoemaService
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter


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


def test_evaluate_stored_behavior_finds_classification_beyond_global_query_limit():
    # Regression: evaluate_stored_behavior used query_classifications(limit=N)
    # which returns the N most recent rows globally.  On a database with more
    # than N total classifications the target session's classification could
    # fall outside that top-N slice, making the session appear unclassified
    # and its behavior state collapse to NORMAL even when it was DISTRACTED.
    # The fix uses query_classification_map(session_ids) instead.
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)

    target = ActivitySession(
        start="2026-09-04T10:00:00Z", end="2026-09-04T10:10:00Z",
        device="laptop", app="Instagram", domain="instagram.com",
        event_keys=("target",),
    )
    store.insert_session(target)
    # Insert the target's classification with a very old timestamp so it is
    # ranked below any fresh classification in a DESC-ordered global query.
    old_stamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
    store.insert_classification(Classification(
        target.id, "entertainment", activity_type="browsing",
        productivity="distracting", confidence=0.9,
        classification_status="classified",
        provider="gemini", model="gemini-test", source="gemini",
        classified_at=old_stamp,
    ))

    # Insert enough newer dummy classifications to push the target beyond
    # any small global limit.
    query_limit = 20
    for i in range(query_limit + 5):
        dummy = ActivitySession(
            start="2026-09-03T10:00:00Z", end="2026-09-03T10:01:00Z",
            device="laptop", app="VS Code",
            event_keys=("dummy-{}".format(i),),
        )
        store.insert_session(dummy)
        store.insert_classification(Classification(
            dummy.id, "neutral", activity_type="browsing",
            productivity="neutral", confidence=0.5,
            classification_status="classified",
            provider="gemini", model="gemini-test", source="gemini",
        ))

    observations = service.evaluate_stored_behavior(
        start="2026-09-04T09:00:00Z",
        end="2026-09-04T11:00:00Z",
        limit=query_limit,
    )

    by_id = {obs.session_id: obs for obs in observations}
    assert target.id in by_id, "target session missing from behavior observations"
    assert by_id[target.id].state == BehaviorState.DISTRACTED, (
        "evaluate_stored_behavior missed the stored classification — "
        "was it outside the top-{} global query window?".format(query_limit)
    )
    store.close()
