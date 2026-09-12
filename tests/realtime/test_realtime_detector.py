"""Candidate detector + hysteresis: boundaries, combination, debouncing."""

from datetime import datetime, timedelta, timezone

from noema.application.realtime.detector import (
    DetectionState,
    DetectionTracker,
    DetectorConfig,
    DistractionCandidateDetector,
)
from noema.application.realtime.features import BehaviorWindowConfig, build_behavior_windows

from test_realtime_features import FakeClassification, FakeSession, NOW, classes, moment


def window_for(sessions, mapping, minutes=60, **kwargs):
    (window,) = build_behavior_windows(
        sessions, mapping, now=NOW,
        config=BehaviorWindowConfig(windows_minutes=(minutes,)), **kwargs)
    return window


def detector():
    return DistractionCandidateDetector()


# MANDATORY TEST 1: 10 minutes productive coding → not a candidate.
def test_productive_coding_is_not_a_candidate():
    window = window_for(
        [FakeSession("a", moment(NOW, minutes=-10), moment(NOW), topic="reward.py")],
        classes([("a", "productive")]))
    decision = detector().score(window)
    assert decision.score == 0.0
    assert decision.is_candidate is False


# MANDATORY TEST 2: 2 minutes YouTube → no candidacy (too little evidence).
def test_two_minute_distraction_is_not_a_candidate():
    window = window_for(
        [FakeSession("a", moment(NOW, minutes=-12), moment(NOW, minutes=-10), topic="YouTube"),
         FakeSession("b", moment(NOW, minutes=-10), moment(NOW), topic="reward.py")],
        classes([("a", "distracting"), ("b", "productive")]))
    decision = detector().score(window)
    assert decision.is_candidate is False


# MANDATORY TEST 3: 25 minutes continuous distraction → candidate.
def test_continuous_distraction_is_a_candidate():
    window = window_for(
        [FakeSession("a", moment(NOW, minutes=-25), moment(NOW), topic="YouTube")],
        classes([("a", "distracting")]))
    decision = detector().score(window)
    assert decision.is_candidate is True
    assert 0.0 <= decision.score <= 1.0
    assert decision.reasons


# MANDATORY TEST 4: switching pattern visible and scored.
def test_switching_pattern_scores_and_combines_weak_signals():
    sessions = [
        FakeSession("y1", moment(NOW, minutes=-30), moment(NOW, minutes=-25), topic="YouTube"),
        FakeSession("c1", moment(NOW, minutes=-25), moment(NOW, minutes=-15), topic="reward.py"),
        FakeSession("i1", moment(NOW, minutes=-15), moment(NOW, minutes=-10), topic="Instagram"),
        FakeSession("c2", moment(NOW, minutes=-10), moment(NOW, minutes=-5), topic="reward.py"),
        FakeSession("r1", moment(NOW, minutes=-5), moment(NOW), topic="Reddit"),
    ]
    mapping = classes([
        ("y1", "distracting"), ("c1", "productive"), ("i1", "distracting"),
        ("c2", "productive"), ("r1", "distracting")])
    window = window_for(sessions, mapping)
    decision = detector().score(window)
    # The interleaved pattern is visible to the detector (switches, entries,
    # both directions) even though the user keeps returning to productive
    # work, so this must NOT be a candidate — recovery is happening.
    assert decision.signals["context_switching"] > 0
    assert decision.signals["productive_to_distraction"] > 0
    assert window.productive_to_distraction_switches == 2
    assert window.distraction_to_productive_switches == 2
    assert decision.is_candidate is False


# MANDATORY TEST 5: 50 minutes productive → not a candidate.
def test_long_productive_run_is_not_a_candidate():
    window = window_for(
        [FakeSession("a", moment(NOW, minutes=-50), moment(NOW), topic="reward.py")],
        classes([("a", "productive")]))
    assert detector().score(window).is_candidate is False


# MANDATORY TEST 6: AFK while browser focused → never a candidate, no call.
def test_afk_never_a_candidate():
    window = window_for(
        [FakeSession("a", moment(NOW, minutes=-30), moment(NOW), topic="YouTube",
                     active=0.0, afk=1800.0)],
        classes([("a", "distracting")]))
    decision = detector().score(window)
    assert decision.score == 0.0
    assert decision.is_candidate is False


def test_goal_misalignment_adds_but_never_decides_alone():
    sessions = [FakeSession("a", moment(NOW, minutes=-10), moment(NOW), topic="work")]
    aligned = window_for(sessions, classes([("a", "productive")]),
                         current_goal="ship", goal_alignment=1.0)
    misaligned = window_for(sessions, classes([("a", "productive")]),
                            current_goal="ship", goal_alignment=0.0)
    assert detector().score(misaligned).score >= detector().score(aligned).score
    assert detector().score(misaligned).is_candidate is False


def test_unclassified_heavy_window_is_tempered():
    sessions = [FakeSession("a", moment(NOW, minutes=-55), moment(NOW, minutes=-5), topic="?")]
    window = window_for(sessions, {"a": FakeClassification("x", status="pending", source="pending")})
    decision = detector().score(window)
    assert decision.is_candidate is False


def test_config_validation():
    for kwargs in ({"weights": {"a": 1.0}}, {"weights": dict.fromkeys(
            ["sustained_run", "distraction_minutes", "distraction_ratio",
             "distraction_entries",
             "short_bursts", "distraction_streak", "repeated_context",
             "context_switching", "productive_to_distraction",
             "time_since_productive", "goal_misalignment",
             "current_session"], 0.5)},
            {"enter_threshold": 0.4, "exit_threshold": 0.6},
            {"min_active_seconds": -1}):
        try:
            DetectorConfig(**kwargs)
        except ValueError:
            continue
        raise AssertionError("expected ValueError for {}".format(kwargs))


# MANDATORY TEST 8: exact boundary is deterministic (enter at >= 0.70).
def test_enter_boundary_is_deterministic():
    tracker = DetectionTracker()
    assert tracker.update(0.699, NOW) == DetectionState.NORMAL.value
    assert tracker.update(0.70, NOW) == DetectionState.CANDIDATE.value


# MANDATORY TEST 9: oscillation around enter does not flap.
def test_hysteresis_prevents_flapping():
    tracker = DetectionTracker()
    assert tracker.update(0.80, NOW) == DetectionState.CANDIDATE.value
    for score in (0.69, 0.71, 0.65, 0.72, 0.55, 0.69):
        assert tracker.update(score, moment(NOW, seconds=10)) == DetectionState.CANDIDATE.value
    assert tracker.update(0.50, moment(NOW, seconds=20)) == DetectionState.NORMAL.value
    assert tracker.update(0.69, moment(NOW, seconds=30)) == DetectionState.NORMAL.value


def test_full_lifecycle_with_cooldown_and_recovery():
    tracker = DetectionTracker(DetectorConfig(cooldown_seconds=60, recovery_seconds=60))
    assert tracker.update(0.8, NOW) == DetectionState.CANDIDATE.value
    assert tracker.notify_confirmed(0.8, NOW) == DetectionState.CONFIRMED.value
    assert tracker.notify_intervention(NOW) == DetectionState.INTERVENTION_COOLDOWN.value
    # Still cooling: high score cannot re-enter.
    assert tracker.update(0.9, moment(NOW, seconds=30)) == DetectionState.INTERVENTION_COOLDOWN.value
    # Cooldown elapsed, score low → recovering; then normal after recovery time.
    assert tracker.update(0.2, moment(NOW, seconds=61)) == DetectionState.RECOVERING.value
    assert tracker.update(0.2, moment(NOW, seconds=100)) == DetectionState.RECOVERING.value
    assert tracker.update(0.2, moment(NOW, seconds=122)) == DetectionState.NORMAL.value


def test_tracker_survives_serialization_round_trip():
    tracker = DetectionTracker()
    tracker.update(0.8, NOW)
    clone = DetectionTracker.from_dict(tracker.to_dict())
    assert clone.state == DetectionState.CANDIDATE.value
    assert clone.update(0.4, NOW) == DetectionState.NORMAL.value
    assert DetectionTracker.from_dict({}).state == DetectionState.NORMAL.value
