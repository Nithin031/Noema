from datetime import datetime, timezone

from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification
from noema.domain.intervention import Intervention, InterventionMode, InterventionStatus
from noema.domain.outcomes import OutcomeTracker, RecoveryStatus
from noema.domain.sessions import ActivitySession


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
