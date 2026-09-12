"""User presence, AFK handling, event merging, and meaningful sessions.

PART 49-57 acceptance coverage. Presence answers "was the user
interacting?" from input timing only; window focus is never presence.
"""

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.domain.activity import ActivityEvent
from noema.infrastructure.browser.models import BrowserEvent
from noema.runtime import NoemaDaemon, DaemonConfig
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification, Classifier
from noema.infrastructure.ollama import OllamaError
from noema.domain.presence import (
    PresenceDetector,
    PresenceState,
    is_presence_event,
)
from noema.domain.sessions import Sessionizer


def call_app(app, method, path, body=b""):
    result = {}

    def start_response(status, headers):
        result["status"] = status

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path.split("?", 1)[0],
        "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    response = b"".join(app(environ, start_response))
    return result["status"], json.loads(response.decode("utf-8"))


def recent_stamp(minutes_ago, duration_seconds=0):
    moment = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=minutes_ago)
    return moment.isoformat().replace("+00:00", "Z")


def window_event(timestamp, duration, app="Firefox", title="Page", domain=None,
                 event_id=None, url=None):
    return ActivityEvent(
        timestamp=timestamp,
        duration=duration,
        device="laptop",
        app=app,
        title=title,
        domain=domain,
        url=url,
        bucket_id="window_laptop",
        source_event_id=event_id or "{}-{}-{}".format(timestamp, app, title),
    )


def afk_event(timestamp, duration, status, event_id=None):
    return ActivityEvent(
        timestamp=timestamp,
        duration=duration,
        device="laptop",
        bucket_id="aw-watcher-afk_Wizard",
        source_event_id=event_id or "afk-{}-{}".format(timestamp, status),
        metadata={"activitywatch_bucket_type": "afkstatus",
                  "activitywatch_client": "aw-watcher-afk",
                  "status": status},
    )


def tab_activation(timestamp):
    return BrowserEvent(event_type="tab_activated", window_id="17", tab_id="42",
                        timestamp=timestamp)


def detector(**kwargs):
    return PresenceDetector(timeout_seconds=180.0, **kwargs)


# PART 49.1: input within timeout -> ACTIVE.
def test_input_within_timeout_is_active():
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T10:00:00Z", 120, "not-afk")],
        start="2026-09-06T10:00:00Z", end="2026-09-06T10:05:00Z",
        now="2026-09-06T10:05:00Z")
    assert timeline.state_at("2026-09-06T10:01:00Z") == PresenceState.ACTIVE


# PART 49.2: no input beyond timeout -> AFK (PART 5 example: inputs at
# :00, :10 and :40 keep the watcher reporting not-afk until 10:05:41).
def test_no_input_beyond_timeout_is_afk():
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T10:00:00Z", 341, "not-afk"),
         afk_event("2026-09-06T10:05:41Z", 600, "afk")],
        start="2026-09-06T10:00:00Z", end="2026-09-06T10:20:00Z",
        now="2026-09-06T10:20:00Z")
    assert timeline.state_at("2026-09-06T10:03:00Z") == PresenceState.ACTIVE
    assert timeline.state_at("2026-09-06T10:06:00Z") == PresenceState.AFK


# PART 49.3: input resumes -> ACTIVE.
def test_resumed_input_returns_to_active():
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T10:00:00Z", 60, "not-afk"),
         afk_event("2026-09-06T10:05:00Z", 132, "afk"),
         afk_event("2026-09-06T10:07:12Z", 300, "not-afk")],
        start="2026-09-06T10:00:00Z", end="2026-09-06T10:20:00Z",
        now="2026-09-06T10:20:00Z")
    assert timeline.state_at("2026-09-06T10:06:00Z") == PresenceState.AFK
    assert timeline.state_at("2026-09-06T10:08:00Z") == PresenceState.ACTIVE
    states = [state for _, state in timeline.transitions()]
    assert states == ["active", "afk", "active"]


# PART 49.4: system lock -> AFK/system idle.
def test_system_lock_reports_afk():
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T10:00:00Z", 3600, "afk")],
        start="2026-09-06T10:00:00Z", end="2026-09-06T11:00:00Z",
        now="2026-09-06T11:00:00Z")
    assert timeline.state_at("2026-09-06T10:30:00Z") == PresenceState.AFK
    assert timeline.afk_seconds("2026-09-06T10:00:00Z", "2026-09-06T11:00:00Z") == 3600.0


# PART 49.5: system sleep (data gap) fabricates nothing.
def test_sleep_gap_yields_unknown_not_presence():
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T10:00:00Z", 60, "not-afk")],
        start="2026-09-06T10:00:00Z", end="2026-09-06T12:00:00Z",
        now="2026-09-06T12:00:00Z")
    assert timeline.state_at("2026-09-06T11:00:00Z") == PresenceState.UNKNOWN
    assert timeline.active_seconds("2026-09-06T10:00:00Z", "2026-09-06T12:00:00Z") == 60.0


# PART 8: focused window without input still goes AFK after timeout.
def test_focused_window_is_not_presence():
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T10:00:00Z", 60, "not-afk"),
         afk_event("2026-09-06T10:03:00Z", 1020, "afk")],
        start="2026-09-06T10:00:00Z", end="2026-09-06T10:30:00Z",
        now="2026-09-06T10:30:00Z")
    # Window stayed focused the whole 20 minutes; only ~1 minute was active.
    assert timeline.active_seconds("2026-09-06T10:00:00Z", "2026-09-06T10:20:00Z") == 60.0
    assert timeline.afk_seconds("2026-09-06T10:00:00Z", "2026-09-06T10:20:00Z") == 1020.0


def test_browser_tab_activation_counts_as_input():
    timeline = detector().build_timeline(
        [], [tab_activation("2026-09-06T10:07:12Z")],
        start="2026-09-06T10:00:00Z", end="2026-09-06T10:20:00Z",
        now="2026-09-06T10:20:00Z")
    assert timeline.state_at("2026-09-06T10:07:30Z") == PresenceState.ACTIVE
    assert timeline.last_input_at is not None
    assert timeline.last_input_at.isoformat().startswith("2026-09-06T10:07:12")


def test_afk_rows_win_over_browser_signals():
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T10:05:00Z", 600, "afk")],
        [tab_activation("2026-09-06T10:07:12Z")],
        start="2026-09-06T10:00:00Z", end="2026-09-06T10:20:00Z",
        now="2026-09-06T10:20:00Z")
    assert timeline.state_at("2026-09-06T10:07:30Z") == PresenceState.AFK


def test_media_exception_is_inert_without_producer():
    # A periodic heartbeat carrying playback metadata is NOT user input on
    # its own; only the explicit flag honors it (and no producer sends it).
    event = BrowserEvent(event_type="heartbeat", window_id="1", tab_id="2",
                         timestamp="2026-09-06T10:00:00Z",
                         metadata={"audible": True})
    off = PresenceDetector(media_exception=False)
    on = PresenceDetector(media_exception=True)
    assert off.extract_input_times([event]) == []
    assert len(on.extract_input_times([event])) == 1


def test_afk_rows_never_become_activity_sessions():
    events = [
        window_event("2026-09-06T10:00:00Z", 60, app="Firefox", title="Research", event_id="w1"),
        afk_event("2026-09-06T10:00:00Z", 60, "not-afk", event_id="a1"),
    ]
    assert is_presence_event(events[1])
    assert not is_presence_event(events[0])
    sessions = Sessionizer().sessionize(events)
    assert len(sessions) == 1
    assert sessions[0].app == "Firefox"


# PART 50: rapid tab switches merge into one session candidate.
def test_rapid_tab_switching_merges_into_one_session():
    events = [
        window_event("2026-09-06T10:00:00Z", 1, app="Firefox", title="Research Paper", event_id="1"),
        window_event("2026-09-06T10:00:01Z", 2, app="VS Code", title="a.py", event_id="2"),
        window_event("2026-09-06T10:00:03Z", 1, app="OpenCode", title="oc", event_id="3"),
        window_event("2026-09-06T10:00:04Z", 3, app="Firefox", title="GitHub", domain="github.com", event_id="4"),
        window_event("2026-09-06T10:00:07Z", 5, app="Firefox", title="Google", domain="google.com", event_id="5"),
    ]
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T09:59:00Z", 300, "not-afk")],
        start="2026-09-06T09:59:00Z", end="2026-09-06T10:05:00Z")
    sessions = Sessionizer(merge_gap_seconds=15).sessionize(events, presence=timeline)
    assert len(sessions) == 1
    assert sessions[0].duration == 12.0


# PART 51: 30-minute window, input for first 4 minutes only.
def test_long_window_clips_to_active_portion():
    events = [window_event("2026-09-06T10:00:00Z", 1800, app="Firefox", title="Research", event_id="long")]
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T10:00:00Z", 240, "not-afk"),
         afk_event("2026-09-06T10:04:00Z", 1560, "afk")],
        start="2026-09-06T10:00:00Z", end="2026-09-06T10:30:00Z")
    sessions = Sessionizer(merge_gap_seconds=15).sessionize(events, presence=timeline)
    assert len(sessions) == 1
    assert sessions[0].duration == 240.0


# PART 52: AFK splits one app's run into two active sessions.
def test_afk_splits_return_from_afk():
    events = [
        window_event("2026-09-06T09:00:00Z", 600, app="OpenCode", title="oc", event_id="a"),
        window_event("2026-09-06T09:20:00Z", 720, app="OpenCode", title="oc", event_id="b"),
    ]
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T09:00:00Z", 480, "not-afk"),
         afk_event("2026-09-06T09:08:00Z", 720, "afk"),
         afk_event("2026-09-06T09:20:00Z", 720, "not-afk")],
        start="2026-09-06T09:00:00Z", end="2026-09-06T09:40:00Z")
    sessions = Sessionizer(merge_gap_seconds=15).sessionize(events, presence=timeline)
    assert len(sessions) == 2
    assert sessions[0].end.isoformat() == "2026-09-06T09:08:00+00:00"
    assert sessions[1].start.isoformat() == "2026-09-06T09:20:00+00:00"


# PART 15: a long detour stays a boundary even with small gaps.
def test_long_detour_is_not_merged():
    events = [
        window_event("2026-09-06T10:00:00Z", 2400, app="VS Code", title="a.py", event_id="c1"),
        window_event("2026-09-06T10:41:00Z", 120, app="WhatsApp", title="chat", event_id="w"),
        window_event("2026-09-06T10:44:00Z", 2160, app="VS Code", title="a.py", event_id="c2"),
    ]
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T09:55:00Z", 5400, "not-afk")],
        start="2026-09-06T09:55:00Z", end="2026-09-06T11:30:00Z")
    sessions = Sessionizer(merge_gap_seconds=15).sessionize(events, presence=timeline)
    assert len(sessions) == 3


# PART 53: multi-app active window becomes one meaningful session.
def test_active_window_becomes_one_meaningful_session():
    from noema.domain.meaningful import MeaningfulSessionEngine

    store_events = [
        window_event("2026-09-06T09:00:00Z", 15, app="Firefox", title="KITTI",
                     domain="x.org", event_id="e1"),
        window_event("2026-09-06T09:00:15Z", 3, app="Firefox", title="Google Search",
                     domain="google.com", event_id="e2"),
        window_event("2026-09-06T09:00:18Z", 8, app="Firefox", title="GitHub",
                     domain="github.com", event_id="e3"),
        window_event("2026-09-06T09:00:26Z", 40, app="OpenCode", title="oc", event_id="e4"),
        window_event("2026-09-06T09:01:06Z", 22, app="VS Code", title="a.py", event_id="e5"),
        window_event("2026-09-06T09:01:28Z", 31, app="Firefox", title="KITTI-MOTS",
                     domain="y.org", event_id="e6"),
    ]
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T08:59:00Z", 600, "not-afk")],
        start="2026-09-06T08:59:00Z", end="2026-09-06T09:05:00Z")
    sessions = Sessionizer(merge_gap_seconds=15).sessionize(store_events, presence=timeline)
    assert len(sessions) == 1
    meaningful = MeaningfulSessionEngine().build(sessions, presence=timeline)
    assert len(meaningful) == 1
    assert meaningful[0].active_duration_seconds == 119.0
    assert meaningful[0].afk_duration_seconds == 0.0


def test_meaningful_session_records_afk_within_span():
    from noema.domain.meaningful import MeaningfulSessionEngine
    from noema.domain.sessions import ActivitySession

    first = ActivitySession(start="2026-09-06T10:00:00Z", end="2026-09-06T10:05:00Z",
                            device="laptop", app="Firefox", title="Research", event_keys=("k1",))
    second = ActivitySession(start="2026-09-06T10:06:00Z", end="2026-09-06T10:11:00Z",
                             device="laptop", app="Firefox", title="Research", event_keys=("k2",))
    timeline = detector().build_timeline(
        [afk_event("2026-09-06T09:55:00Z", 600, "not-afk"),
         afk_event("2026-09-06T10:05:00Z", 600, "afk"),
         afk_event("2026-09-06T10:15:00Z", 600, "not-afk")],
        start="2026-09-06T09:55:00Z", end="2026-09-06T10:25:00Z")
    # AFK between the runs vetoes the merge even for identical context.
    merged = MeaningfulSessionEngine().build([first, second], presence=timeline)
    assert len(merged) == 2
    assert merged[0].active_duration_seconds == 300.0
    untouched = MeaningfulSessionEngine().build([first, second])
    assert len(untouched) == 1


class CountingClient:
    model = "test-counter"

    def __init__(self, payload=None):
        self.payload = payload
        self.calls = 0

    def generate_json(self, prompt):
        self.calls += 1
        return dict(self.payload)


def valid_payload():
    return {
        "category": "productive", "subcategory": "research",
        "activity": "Verifying presence behavior",
        "signal": "The observed evidence supports this result.",
        "topic": "presence", "service": "test",
        "intent_signal": "verification", "activity_type": "research",
        "productivity": "productive", "confidence": 0.9,
    }


class AfkAndWindowAW:
    """Fake collector serving one window bucket plus one afk bucket."""

    def __init__(self, window_events, afk_events):
        self.window_events = window_events
        self.afk_events = afk_events

    def list_buckets(self):
        return {
            "aw-window": {"hostname": "laptop", "type": "currentwindow",
                          "client": "aw-watcher-window"},
            "aw-afk": {"hostname": "laptop", "type": "afkstatus",
                       "client": "aw-watcher-afk"},
        }

    def get_events(self, bucket_id, start=None, end=None):
        if bucket_id == "aw-afk":
            return self.afk_events
        return self.window_events


def aw_event(event_id, timestamp, duration, status=None, app="Firefox", title="Research"):
    if status is not None:
        return {"id": event_id, "timestamp": timestamp, "duration": duration,
                "data": {"status": status}}
    return {"id": event_id, "timestamp": timestamp, "duration": duration,
            "data": {"app": app, "title": title}}


# PART 49.6 + 54 + 56: AFK-covered activity costs zero LLM calls.
def test_afk_covered_activity_never_reaches_the_provider():
    activitywatch = AfkAndWindowAW(
        [aw_event("w1", "2026-09-06T10:05:00Z", 600)],
        [aw_event("a1", "2026-09-06T10:00:00Z", 300, status="not-afk"),
         aw_event("a2", "2026-09-06T10:05:00Z", 600, status="afk")],
    )
    store = SQLiteStore()
    client = CountingClient(payload=valid_payload())
    service = NoemaService(
        ActivityWatchAdapter(activitywatch), store,
        classifier=Classifier(client=client),
        sessionizer=Sessionizer(merge_gap_seconds=15),
        presence_detector=PresenceDetector(),
    )
    lock = str(Path.cwd() / "tests" / "noema" / ".presence-afk.lock")
    daemon = NoemaDaemon(service, DaemonConfig(db_path=":memory:", lock_path=lock))
    try:
        daemon.run_once(now="2026-09-06T10:30:00Z")
        assert client.calls == 0
        assert store.query_meaningful_sessions(limit=100000) == []
    finally:
        daemon.stop()
        store.close()
        Path(lock).unlink(missing_ok=True)


def test_end_to_end_active_flow_classifies_once_afk_never():
    """PART 66: 10:00 active research, AFK gap, 10:10 rapid coding run."""
    from noema.application.classification import Classifier
    from noema.domain.meaningful import MeaningfulSessionEngine

    class CountingProvider:
        name = "gemini"
        model = "test-flash"

        def __init__(self):
            self.calls = 0

        def classify(self, session, prompt):
            self.calls += 1
            return {
                "category": "productive", "subcategory": "research",
                "activity": "Active work", "signal": "Evidence supports it.",
                "confidence": 0.9, "productivity": "productive",
            }

    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(), store,
        classifier=Classifier(provider=CountingProvider()),
        sessionizer=Sessionizer(merge_gap_seconds=15),
        presence_detector=PresenceDetector(),
    )
    service.ingest_events([
        window_event("2026-09-06T10:00:00Z", 60, app="Firefox", title="Research paper",
                     domain="x.org", event_id="r1"),
        window_event("2026-09-06T10:01:00Z", 60, app="Firefox", title="Research paper",
                     domain="x.org", event_id="r2"),
        window_event("2026-09-06T10:02:00Z", 60, app="Firefox", title="Research paper",
                     domain="x.org", event_id="r3"),
        afk_event("2026-09-06T10:00:00Z", 180, "not-afk", event_id="p1"),
        afk_event("2026-09-06T10:03:00Z", 420, "afk", event_id="p2"),
        afk_event("2026-09-06T10:10:00Z", 660, "not-afk", event_id="p3"),
        window_event("2026-09-06T10:10:00Z", 2, app="OpenCode", title="oc", event_id="c1"),
        window_event("2026-09-06T10:10:02Z", 3, app="VS Code", title="a.py", event_id="c2"),
        window_event("2026-09-06T10:10:05Z", 4, app="Firefox", title="GitHub",
                     domain="github.com", event_id="c3"),
        window_event("2026-09-06T10:10:09Z", 200, app="VS Code", title="a.py", event_id="c4"),
        window_event("2026-09-06T10:13:29Z", 191, app="VS Code", title="a.py", event_id="c5"),
    ])
    sessions = service.sessionize_stored_events()
    # AFK span yields no session; the two active runs do.
    assert len(sessions) == 2
    assert sessions[0].duration == 180.0
    meaningful = service.build_meaningful_sessions(
        start="2026-09-06T09:55:00Z", end="2026-09-06T10:25:00Z")
    assert len(meaningful) == 2
    assert all(item.afk_duration_seconds == 0.0 for item in meaningful)
    pending = service.pending_classification_sessions(meaningful)
    assert len(pending) == 2
    results = service.classify_meaningful_sessions(pending)
    assert all(item.classification_status == "classified" for item in results)
    assert service.classifier.provider.calls == 2
    # Second pass: idempotent, zero new LLM calls.
    again = service.pending_classification_sessions(
        service.store.query_meaningful_sessions(limit=100000))
    assert again == []
    store.close()


def test_pending_gate_excludes_measured_afk_only_sessions():
    from noema.domain.meaningful import MeaningfulSession

    service = NoemaService(ActivityWatchAdapter(), SQLiteStore())
    afk_only = MeaningfulSession(
        start_time="2026-09-06T10:05:00Z", end_time="2026-09-06T10:15:00Z",
        device_set=("laptop",), activity_session_ids=("raw-1",),
        active_duration_seconds=0.0, afk_duration_seconds=600.0)
    assert service.pending_classification_sessions([afk_only]) == []
    service.store.close()


# PART 57: presence + recent-classifications endpoints serve backend truth.
def test_presence_and_recent_classification_endpoints():
    from noema.application.classification import Classification
    from noema.domain.meaningful import MeaningfulSession

    event_stamp = recent_stamp(10)
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    service.ingest_events([
        ActivityEvent(
            timestamp=event_stamp, duration=120, device="laptop",
            app="Firefox", title="Research", domain="x.org",
            bucket_id="window", source_event_id="p1",
        ),
        ActivityEvent(
            timestamp=event_stamp, duration=600, device="laptop",
            bucket_id="aw-afk", source_event_id="pa1",
            metadata={"activitywatch_bucket_type": "afkstatus", "status": "not-afk"},
        ),
    ])
    sessions = service.sessionize_stored_events()
    assert len(sessions) == 1
    end_stamp = recent_stamp(8)
    meaningful = MeaningfulSession(
        start_time=event_stamp, end_time=end_stamp,
        device_set=("laptop",), activity_session_ids=(sessions[0].id,),
        active_duration_seconds=120.0, afk_duration_seconds=0.0)
    store.insert_meaningful_session(meaningful)
    store.insert_classification(Classification(
        meaningful.id, "productive", topic="research", activity_type="reading",
        productivity="productive", confidence=0.9, provider="gemini",
        model="gemini-3.5-flash-lite", source="gemini",
        classification_status="classified",
    ))
    app = create_app(service)

    status, presence = call_app(app, "GET", "/api/presence/current")
    assert status.startswith("200")
    assert presence["state"] in {"active", "afk", "unknown"}
    assert "idle_seconds" in presence
    assert presence["current_session"]["session_id"] == meaningful.id
    assert presence["current_session"]["classification"]["category"] == "productive"

    status, recent = call_app(app, "GET", "/api/dashboard/recent-classifications?limit=5")
    assert status.startswith("200")
    assert len(recent["classifications"]) == 1
    assert recent["classifications"][0]["category"] == "productive"

    status, summary = call_app(app, "GET", "/api/dashboard/summary?range=today")
    assert status.startswith("200")
    store.close()
