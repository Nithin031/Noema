"""Rolling behavioral feature windows: correctness + boundaries."""

from datetime import datetime, timedelta, timezone

from noema.application.realtime.features import (
    BehaviorWindowConfig,
    build_behavior_windows,
)


def moment(base, **delta):
    return base + timedelta(**delta)


class FakeSession:
    def __init__(self, sid, start, end, topic="work", active=None, afk=0.0):
        self.id = sid
        self.start_time = start
        self.end_time = end
        self.primary_topic = topic
        self.primary_task = None
        self.dominant_category = None
        total = (end - start).total_seconds()
        self.afk_duration_seconds = float(afk)
        self.active_duration_seconds = float(total - afk) if active is None else float(active)


class FakeClassification:
    def __init__(self, productivity, confidence=0.9, status="classified", source="gemini"):
        self.productivity = productivity
        self.confidence = confidence
        self.classification_status = status
        self.source = source


def classes(pairs):
    return {sid: FakeClassification(prod) for sid, prod in pairs}


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def test_productive_coding_has_no_distraction_features():
    sessions = [
        FakeSession("a", moment(NOW, minutes=-10), moment(NOW), topic="reward.py"),
    ]
    (window,) = build_behavior_windows(
        sessions, classes([("a", "productive")]), now=NOW,
        config=BehaviorWindowConfig(windows_minutes=(60,)),
    )
    assert window.productive_seconds == 600.0
    assert window.distractive_seconds == 0.0
    assert window.distraction_ratio == 0.0
    assert window.distraction_entries == 0
    assert window.longest_distraction_streak == 0
    assert window.current_session_category == "productive"
    assert window.time_since_last_productive_session == 0.0


def test_continuous_distraction_accumulates():
    sessions = [
        FakeSession("a", moment(NOW, minutes=-25), moment(NOW), topic="YouTube"),
    ]
    (window,) = build_behavior_windows(
        sessions, classes([("a", "distracting")]), now=NOW,
        config=BehaviorWindowConfig(windows_minutes=(60,)),
    )
    assert window.distractive_seconds == 1500.0
    assert window.distraction_ratio == 1.0
    assert window.distraction_entries == 1
    assert window.time_since_last_productive_session is None


def test_switching_sequence_is_visible():
    sessions = [
        FakeSession("y1", moment(NOW, minutes=-30), moment(NOW, minutes=-25), topic="YouTube"),
        FakeSession("c1", moment(NOW, minutes=-25), moment(NOW, minutes=-15), topic="reward.py"),
        FakeSession("i1", moment(NOW, minutes=-15), moment(NOW, minutes=-10), topic="Instagram"),
        FakeSession("c2", moment(NOW, minutes=-10), moment(NOW, minutes=-5), topic="reward.py"),
        FakeSession("r1", moment(NOW, minutes=-5), moment(NOW), topic="Reddit"),
    ]
    mapping = classes([
        ("y1", "distracting"), ("c1", "productive"), ("i1", "distracting"),
        ("c2", "productive"), ("r1", "distracting"),
    ])
    (window,) = build_behavior_windows(
        sessions, mapping, now=NOW, config=BehaviorWindowConfig(windows_minutes=(60,)))
    assert window.context_switch_count == 4
    assert window.productive_to_distraction_switches == 2
    assert window.distraction_to_productive_switches == 2
    assert window.distraction_entries == 3
    assert window.productive_entries == 2
    assert window.unique_distraction_contexts == 3
    assert len(window.recent_session_sequence) == 5


def test_repeated_distraction_contexts_and_streaks():
    sessions = [
        FakeSession("a", moment(NOW, minutes=-20), moment(NOW, minutes=-15), topic="YouTube"),
        FakeSession("b", moment(NOW, minutes=-15), moment(NOW, minutes=-10), topic="YouTube"),
        FakeSession("c", moment(NOW, minutes=-10), moment(NOW, minutes=-9), topic="memes"),
        FakeSession("d", moment(NOW, minutes=-9), moment(NOW), topic="work"),
    ]
    mapping = classes([
        ("a", "distracting"), ("b", "distracting"),
        ("c", "distracting"), ("d", "productive"),
    ])
    (window,) = build_behavior_windows(
        sessions, mapping, now=NOW, config=BehaviorWindowConfig(windows_minutes=(60,)))
    assert window.repeated_distraction_contexts == {"YouTube": 2}
    assert window.longest_distraction_streak == 3
    assert window.short_distraction_bursts == 1  # only the 1-minute session
    assert window.time_since_last_productive_session == 0.0


def test_afk_only_sessions_never_create_distraction():
    sessions = [
        FakeSession("a", moment(NOW, minutes=-30), moment(NOW), topic="YouTube",
                    active=0.0, afk=1800.0),
    ]
    (window,) = build_behavior_windows(
        sessions, classes([("a", "distracting")]), now=NOW,
        config=BehaviorWindowConfig(windows_minutes=(60,)),
    )
    assert window.afk_seconds == 1800.0
    assert window.active_seconds == 0.0
    assert window.distractive_seconds == 0.0
    assert window.distraction_ratio == 0.0
    assert window.distraction_entries == 0
    assert window.current_session_category == "afk"


def test_return_from_afk_continues_correctly():
    sessions = [
        FakeSession("afk", moment(NOW, minutes=-40), moment(NOW, minutes=-10),
                    topic="YouTube", active=0.0, afk=1800.0),
        FakeSession("back", moment(NOW, minutes=-10), moment(NOW), topic="reward.py"),
    ]
    mapping = classes([("afk", "distracting"), ("back", "productive")])
    (window,) = build_behavior_windows(
        sessions, mapping, now=NOW, config=BehaviorWindowConfig(windows_minutes=(60,)))
    assert window.afk_seconds == 1800.0
    assert window.productive_seconds == 600.0
    assert window.distractive_seconds == 0.0
    assert window.current_session_category == "productive"


def test_unclassified_and_heuristic_and_failed_stay_unclassified():
    sessions = [
        FakeSession("a", moment(NOW, minutes=-9), moment(NOW, minutes=-6), topic="x"),
        FakeSession("b", moment(NOW, minutes=-6), moment(NOW, minutes=-3), topic="y"),
        FakeSession("c", moment(NOW, minutes=-3), moment(NOW), topic="z"),
    ]
    mapping = {
        "a": FakeClassification("neutral", status="pending", source="pending"),
        "b": FakeClassification("productive", source="heuristic"),
        "c": FakeClassification("productive", status="classification_failed", source="gemini"),
    }
    (window,) = build_behavior_windows(
        sessions, mapping, now=NOW, config=BehaviorWindowConfig(windows_minutes=(60,)))
    assert window.unclassified_seconds == 540.0
    assert window.productive_seconds == 0.0
    assert window.neutral_entries == 0
    assert window.current_session_category == "unclassified"


def test_windows_clip_to_bounds_and_support_all_spans():
    sessions = [
        FakeSession("a", moment(NOW, minutes=-90), moment(NOW), topic="work"),
    ]
    windows = build_behavior_windows(
        sessions, classes([("a", "productive")]), now=NOW)
    assert [window.window_minutes for window in windows] == [5, 15, 30, 60]
    assert windows[0].productive_seconds == 300.0
    assert windows[-1].productive_seconds == 3600.0


def test_deterministic_for_identical_input():
    sessions = [
        FakeSession("a", moment(NOW, minutes=-20), moment(NOW, minutes=-10), topic="YouTube"),
        FakeSession("b", moment(NOW, minutes=-10), moment(NOW), topic="work"),
    ]
    mapping = classes([("a", "distracting"), ("b", "productive")])
    first = build_behavior_windows(sessions, mapping, now=NOW)
    second = build_behavior_windows(sessions, mapping, now=NOW)
    assert [window.to_dict() for window in first] == [window.to_dict() for window in second]


def test_compact_summary_hides_private_content():
    sessions = [
        FakeSession("a", moment(NOW, minutes=-10), moment(NOW), topic="secret project"),
    ]
    (window,) = build_behavior_windows(
        sessions, classes([("a", "productive")]), now=NOW,
        config=BehaviorWindowConfig(windows_minutes=(60,)),
        current_goal="finish the report", goal_alignment=0.8,
    )
    summary = window.compact_summary()
    dumped = str(summary)
    assert "secret project" not in dumped
    assert "finish the report" not in dumped
    assert summary["goal_alignment"] == 0.8
    assert summary["current_category"] == "productive"


def test_config_rejects_bad_values():
    for kwargs in ({"windows_minutes": ()}, {"windows_minutes": (0,)},
                   {"short_burst_seconds": -1}, {"recent_sequence_limit": 0}):
        try:
            BehaviorWindowConfig(**kwargs)
        except ValueError:
            continue
        raise AssertionError("expected ValueError for {}".format(kwargs))
