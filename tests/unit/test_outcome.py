from datetime import datetime, timezone

from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification
from noema.domain.intervention import Intervention, InterventionMode, InterventionStatus
from noema.domain.outcomes import OutcomeTracker, RecoveryStatus
from noema.domain.sessions import ActivitySession
from noema.api import NoemaService
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter


def test_recovery_uses_first_three_minute_productive_session():
    intervention = Intervention(
        session_id="distracted", mode=InterventionMode.NOTIFICATION,
        status=InterventionStatus.EXECUTED, reason="test",
        created_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc),
        intervention_id="intervention-1",
    )
    too_short = ActivitySession(
        start="2026-09-04T10:01:00Z", end="2026-09-04T10:02:00Z",
        device="laptop", app="Code", event_keys=("short",),
    )
    recovered = ActivitySession(
        start="2026-09-04T10:03:00Z", end="2026-09-04T10:07:00Z",
        device="laptop", app="Code", event_keys=("recovered",),
    )
    classifications = {
        too_short.id: Classification(too_short.id, "development", productivity="productive"),
        recovered.id: Classification(recovered.id, "development", productivity="productive"),
    }
    outcome = OutcomeTracker().measure(intervention, [too_short, recovered], classifications)

    assert outcome.recovery_status == RecoveryStatus.RECOVERED
    assert outcome.recovery_session_id == recovered.id
    assert outcome.recovery_duration_seconds == 180


def test_pending_outcome_persists():
    intervention = Intervention(
        session_id="distracted", mode=InterventionMode.MEME,
        status=InterventionStatus.EXECUTED, reason="test",
        created_at="2026-09-04T10:00:00Z", intervention_id="intervention-2",
    )
    outcome = OutcomeTracker().measure(intervention, [], now="2026-09-04T10:10:00Z")
    assert outcome.recovery_status == RecoveryStatus.PENDING
    store = SQLiteStore()
    assert store.insert_outcome(outcome) is True
    assert store.query_outcomes(intervention_id="intervention-2")[0].recovery_status == RecoveryStatus.PENDING
    store.close()


def test_measure_outcome_finds_classification_beyond_global_query_limit():
    # Regression: measure_outcome used query_classifications(limit=100000) —
    # a global DESC-ordered scan. A classification with an old timestamp could
    # fall below the limit and cause the recovery session to appear unclassified,
    # yielding PENDING instead of RECOVERED.
    # The fix uses query_classification_map(session_ids) instead.
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)

    intervention = Intervention(
        session_id="distracted-session",
        mode=InterventionMode.NOTIFICATION,
        status=InterventionStatus.EXECUTED,
        reason="test",
        created_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc),
        intervention_id="intervention-regression",
    )
    store.insert_intervention(intervention)

    # Recovery session starts after the intervention.
    recovery = ActivitySession(
        start="2026-09-04T10:10:00Z", end="2026-09-04T10:15:00Z",
        device="laptop", app="VS Code", event_keys=("recovery-session",),
    )
    store.insert_session(recovery)

    # Give the recovery session a classification with a very old timestamp so
    # it ranks below any newer row in a DESC global query.
    old_stamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
    store.insert_classification(Classification(
        recovery.id, "development", activity_type="coding",
        productivity="productive", confidence=0.9,
        classification_status="classified",
        provider="gemini", model="gemini-test", source="gemini",
        classified_at=old_stamp,
    ))

    # Insert enough newer dummy classifications to exceed any plausible global limit.
    query_limit = 20
    for i in range(query_limit + 5):
        dummy = ActivitySession(
            start="2026-09-03T10:00:00Z", end="2026-09-03T10:01:00Z",
            device="laptop", app="Slack",
            event_keys=("dummy-outcome-{}".format(i),),
        )
        store.insert_session(dummy)
        store.insert_classification(Classification(
            dummy.id, "neutral", activity_type="messaging",
            productivity="neutral", confidence=0.5,
            classification_status="classified",
            provider="gemini", model="gemini-test", source="gemini",
        ))

    outcome = service.measure_outcome("intervention-regression")

    assert outcome.recovery_status == RecoveryStatus.RECOVERED, (
        "measure_outcome missed the stored classification — "
        "was it outside the global query window?"
    )
    assert outcome.recovery_session_id == recovery.id
    store.close()
