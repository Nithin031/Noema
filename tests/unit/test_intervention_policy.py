"""Intervention policy: AFK veto, per-mode cooldown, ineffectiveness backoff."""

from datetime import datetime, timedelta, timezone

from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.domain.intervention import (
    InterventionEngine,
    InterventionMode,
    InterventionPolicy,
    InterventionStatus,
)
from noema.domain.outcomes import InterventionOutcome, RecoveryStatus

from test_intervention import context

NOW = datetime(2026, 9, 4, 10, 10, tzinfo=timezone.utc)


def outcome(mode, status, minutes_ago, iid="i"):
    return InterventionOutcome(
        intervention_id="{}{}".format(iid, minutes_ago),
        intervention_time=NOW - timedelta(minutes=minutes_ago),
        recovery_status=status,
        intervention_type=mode,
    )


# MANDATORY: AFK veto — no intervention at an empty chair.
def test_afk_vetoes_intervention():
    session, observation, intent, classification = context()
    engine = InterventionEngine()
    skipped = engine.consider(
        observation, session, intent, classification, now=NOW, presence_state="AFK")
    assert skipped.status == InterventionStatus.SKIPPED
    assert "AFK" in skipped.reason
    planned = engine.consider(
        observation, session, intent, classification, now=NOW, presence_state="active")
    assert planned.status == InterventionStatus.PLANNED


def test_per_mode_cooldown_stops_meme_spam():
    session, observation, intent, classification = context()
    engine = InterventionEngine(InterventionPolicy(mode=InterventionMode.MEME))
    first = engine.consider(observation, session, intent, classification, now=NOW)
    assert first.status == InterventionStatus.PLANNED
    # 20 minutes later the global 900s cooldown lapsed, but MEME's own
    # 3600s cooldown still holds.
    later = NOW + timedelta(minutes=20)
    skipped = engine.consider(
        observation, session, intent, classification, recent=[first], now=later)
    assert skipped.status == InterventionStatus.SKIPPED
    assert "MEME" in skipped.reason and "cooldown" in skipped.reason


def test_notification_mode_keeps_working_while_meme_cools_down():
    session, observation, intent, classification = context()
    meme_engine = InterventionEngine(InterventionPolicy(mode=InterventionMode.MEME))
    meme = meme_engine.consider(observation, session, intent, classification, now=NOW)
    notify_engine = InterventionEngine(
        InterventionPolicy(mode=InterventionMode.NOTIFICATION))
    later = NOW + timedelta(minutes=20)
    planned = notify_engine.consider(
        observation, session, intent, classification, recent=[meme], now=later)
    assert planned.status == InterventionStatus.PLANNED
    assert planned.mode == InterventionMode.NOTIFICATION


def test_repeated_ineffective_memes_deescalate_to_notification():
    session, observation, intent, classification = context()
    engine = InterventionEngine(InterventionPolicy(mode=InterventionMode.MEME))
    bad_history = [outcome("MEME", RecoveryStatus.NOT_RECOVERED, minutes)
                   for minutes in (60, 120, 180)]
    planned = engine.consider(
        observation, session, intent, classification, now=NOW,
        recent_outcomes=bad_history)
    assert planned.status == InterventionStatus.PLANNED
    assert planned.mode == InterventionMode.NOTIFICATION
    assert "de-escalated" in planned.reason


def test_repeated_ineffective_notifications_back_off():
    session, observation, intent, classification = context()
    engine = InterventionEngine()
    bad_history = [outcome("NOTIFICATION", RecoveryStatus.NOT_RECOVERED, minutes)
                   for minutes in (60, 120, 180)]
    skipped = engine.consider(
        observation, session, intent, classification, now=NOW,
        recent_outcomes=bad_history)
    assert skipped.status == InterventionStatus.SKIPPED
    assert "backing off" in skipped.reason


def test_recovery_and_pending_break_the_streak():
    session, observation, intent, classification = context()
    engine = InterventionEngine()
    history = [
        outcome("NOTIFICATION", RecoveryStatus.NOT_RECOVERED, 30),
        outcome("NOTIFICATION", RecoveryStatus.RECOVERED, 90),
        outcome("NOTIFICATION", RecoveryStatus.NOT_RECOVERED, 150),
        outcome("NOTIFICATION", RecoveryStatus.NOT_RECOVERED, 210),
    ]
    planned = engine.consider(
        observation, session, intent, classification, now=NOW,
        recent_outcomes=history)
    assert planned.status == InterventionStatus.PLANNED
    pending_history = [outcome("NOTIFICATION", RecoveryStatus.PENDING, 30)]
    planned = engine.consider(
        observation, session, intent, classification, now=NOW,
        recent_outcomes=pending_history)
    assert planned.status == InterventionStatus.PLANNED


def test_policy_validation():
    for kwargs in ({"cooldown_seconds": -1},
                   {"mode_cooldowns": {"MEME": -5}},
                   {"max_ineffective_streak": 0},
                   {"ineffective_lookback_seconds": -1}):
        try:
            InterventionPolicy(**kwargs)
        except ValueError:
            continue
        raise AssertionError("expected ValueError for {}".format(kwargs))


def test_afk_session_is_never_recovery():
    from noema.domain.outcomes import OutcomeTracker

    tracker = OutcomeTracker()

    class AfkSession:
        id = "afk1"
        start = NOW + timedelta(minutes=5)
        duration = 600.0
        active_duration_seconds = 0.0

    class Productive:
        productivity = "productive"

    session, observation, intent, classification = context()

    class FakeIntervention:
        id = "i1"
        created_at = NOW
        mode = InterventionMode.NOTIFICATION

    result = tracker.measure(
        FakeIntervention(), [AfkSession()], {"afk1": Productive()}, now=NOW)
    assert result.recovery_status == RecoveryStatus.PENDING
