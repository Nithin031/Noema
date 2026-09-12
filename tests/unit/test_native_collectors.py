"""Native telemetry collectors: deterministic behavior + failure safety."""

from datetime import datetime, timedelta, timezone

import pytest

from noema.domain.activity import ActivityEvent
from noema.domain.presence.detector import afk_status, is_presence_event
from noema.domain.privacy.filter import PrivacyFilter
from noema.domain.sessions import Sessionizer
from noema.infrastructure.activity_sources import (
    ActivityWatchSourceAdapter,
    BrowserActivitySource,
    CompositeActivitySource,
    NativeAFKSource,
    NativeWindowsActivitySource,
)
from noema.infrastructure.activity_sources.activitywatch import (
    ActivityWatchAdapter,
)


def utc(hour=10, minute=0, second=0):
    return datetime(2026, 9, 12, hour, minute, second, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


class ForegroundScript:
    """Scripted (app, title) states, None entries, or raised errors.

    Exhausted scripts repeat their last state (a foreground window
    normally stays put); pass explicit trailing entries for changes.
    """

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0
        self._last = ("Code.exe", "main.py")

    def __call__(self):
        self.calls += 1
        if self.states:
            item = self.states.pop(0)
            if isinstance(item, Exception):
                raise item
            self._last = item
        return self._last


# --- window collector -------------------------------------------------


def test_window_change_closes_span_and_opens_new():
    clock = Clock(utc())
    script = ForegroundScript([("Code.exe", "a.py"), ("Code.exe", "b.py")])
    source = NativeWindowsActivitySource(
        heartbeat_seconds=60.0, foreground_fn=script, clock=clock)
    first = source.poll()
    assert len(first.events) == 1
    assert first.events[0].app == "Code.exe"
    assert first.events[0].title == "a.py"
    clock.advance(3)
    second = source.poll()
    # Old span closed with its real 3s duration + new span opened.
    assert len(second.events) == 2
    assert second.events[0].title == "a.py"
    assert second.events[0].duration == 3.0
    assert second.events[1].title == "b.py"
    assert second.events[0].source_event_id != second.events[1].source_event_id


def test_process_change_emits_new_event():
    clock = Clock(utc())
    script = ForegroundScript([("Code.exe", "a.py"), ("firefox.exe", "Tab")])
    source = NativeWindowsActivitySource(
        heartbeat_seconds=60.0, foreground_fn=script, clock=clock)
    source.poll()
    clock.advance(2)
    result = source.poll()
    assert [event.app for event in result.events] == ["Code.exe", "firefox.exe"]


def test_same_window_suppresses_between_heartbeats_then_extends():
    clock = Clock(utc())
    script = ForegroundScript([("Code.exe", "a.py")] * 5)
    source = NativeWindowsActivitySource(
        heartbeat_seconds=10.0, foreground_fn=script, clock=clock)
    assert len(source.poll().events) == 1
    clock.advance(3)
    assert source.poll().events == []
    clock.advance(3)
    assert source.poll().events == []
    clock.advance(5)
    heartbeat = source.poll()
    assert len(heartbeat.events) == 1
    # Same stable span id, duration extended to 11s.
    assert heartbeat.events[0].duration == 11.0


def test_unobservable_window_closes_span_without_fabrication():
    clock = Clock(utc())
    script = ForegroundScript([("Code.exe", "a.py"), None])
    source = NativeWindowsActivitySource(
        heartbeat_seconds=60.0, foreground_fn=script, clock=clock)
    source.poll()
    clock.advance(4)
    result = source.poll()
    assert len(result.events) == 1
    assert result.events[0].duration == 4.0
    # No open span remains; a further blind poll emits nothing.
    clock.advance(4)
    assert source.poll().events == []


def test_window_probe_error_is_explicit_not_fabricated():
    clock = Clock(utc())
    script = ForegroundScript([OSError("handle closed")])
    source = NativeWindowsActivitySource(
        foreground_fn=script, clock=clock)
    result = source.poll()
    assert result.status == "error"
    assert result.events == []
    assert "OSError" in result.detail


def test_window_events_are_screen_time_not_presence():
    event = NativeWindowsActivitySource(
        foreground_fn=ForegroundScript([("Code.exe", "a.py")]),
        clock=Clock(utc())).poll().events[0]
    assert event.source == "noema_native"
    assert event.bucket_id == "noema-window"
    assert event.metadata["event_kind"] == "window"
    assert not is_presence_event(event)


def test_collector_restart_starts_fresh_span():
    clock = Clock(utc())
    first = NativeWindowsActivitySource(
        foreground_fn=ForegroundScript([("Code.exe", "a.py")]), clock=clock)
    event_before = first.poll().events[0]
    clock.advance(30)
    # New instance (restart): no fabricated 30s history, new span id.
    second = NativeWindowsActivitySource(
        foreground_fn=ForegroundScript([("Code.exe", "a.py")]), clock=clock)
    event_after = second.poll().events[0]
    assert event_after.duration == 0.0
    assert event_after.source_event_id != event_before.source_event_id


def test_naive_clock_is_treated_as_utc():
    naive = datetime(2026, 9, 12, 10, 0, 0)
    source = NativeWindowsActivitySource(
        foreground_fn=ForegroundScript([("Code.exe", "a.py")]),
        clock=lambda: naive)
    event = source.poll().events[0]
    assert event.timestamp.tzinfo is not None


# --- presence collector -----------------------------------------------


def test_idle_transition_timestamps_last_input():
    clock = Clock(utc())
    idles = iter([10.0, 200.0])
    source = NativeAFKSource(timeout_seconds=180.0, heartbeat_seconds=3600.0,
                             idle_seconds_fn=lambda: next(idles), clock=clock)
    # First active instant proves nothing yet: no claim, no row.
    assert source.poll().events == []
    clock.advance(190)
    transition = source.poll()
    # The prior slice would end before it started (input predates it), so
    # it is dropped; the AFK slice stands, exactly like an AW afk ping.
    assert len(transition.events) == 1
    event = transition.events[0]
    assert afk_status(event) == "afk"
    # Honest transition instant: last input, i.e. poll time minus idle.
    assert event.timestamp == utc() + timedelta(seconds=190 - 200)
    assert event.duration == 200.0


def test_active_transition_and_heartbeat():
    clock = Clock(utc())
    idles = iter([200.0, 5.0, 6.0, 7.0])
    source = NativeAFKSource(timeout_seconds=180.0, heartbeat_seconds=10.0,
                             idle_seconds_fn=lambda: next(idles), clock=clock)
    # Already idle at first sight: honest [last-input, now] AFK slice.
    first = source.poll()
    assert len(first.events) == 1
    assert afk_status(first.events[0]) == "afk"
    assert first.events[0].duration == 200.0
    clock.advance(10)
    back = source.poll()
    assert [afk_status(event) for event in back.events] == ["afk", "not-afk"]
    clock.advance(3)
    assert source.poll().events == []
    clock.advance(8)
    heartbeat = source.poll()
    assert len(heartbeat.events) == 1
    assert afk_status(heartbeat.events[0]) == "not-afk"


def test_presence_rows_satisfy_afk_veto_contract():
    clock = Clock(utc())
    source = NativeAFKSource(idle_seconds_fn=lambda: 0.0, clock=clock,
                             heartbeat_seconds=10.0)
    assert source.poll().events == []
    clock.advance(11)
    rows = source.poll().events
    assert len(rows) == 1
    event = rows[0]
    assert is_presence_event(event)
    assert afk_status(event) == "not-afk"
    assert event.app is None and event.title is None


def test_input_clock_failure_never_reports_active():
    def broken():
        raise OSError("GetLastInputInfo failed")

    source = NativeAFKSource(idle_seconds_fn=broken, clock=Clock(utc()))
    result = source.poll()
    assert result.status == "error"
    assert result.events == []


def test_unsupported_platform_reports_unavailable():
    source = NativeWindowsActivitySource(foreground_fn=None)
    source_supported = source.supported  # real platform answer
    assert isinstance(source_supported, bool)
    presence = NativeAFKSource(idle_seconds_fn=None)
    assert isinstance(presence.supported, bool)


# --- composite / browser / legacy -------------------------------------


def test_composite_isolates_failing_source():
    good = NativeWindowsActivitySource(
        foreground_fn=ForegroundScript([("Code.exe", "a.py")]),
        clock=Clock(utc()))

    class Broken(NativeWindowsActivitySource):
        name = "broken"

        def poll(self, now=None):
            raise RuntimeError("sensor exploded")

    composite = CompositeActivitySource([Broken(), good])
    result = composite.poll(utc())
    assert result.status == "error"
    assert len(result.events) == 1
    assert result.events[0].app == "Code.exe"


def test_browser_source_reports_bridge_state():
    assert BrowserActivitySource().poll().status == "unavailable"
    attached = BrowserActivitySource(
        bridge_snapshot_fn=lambda: {"registrations": []})
    assert attached.poll().status == "degraded"
    live = BrowserActivitySource(
        bridge_snapshot_fn=lambda: {"registrations": [{"id": "x"}]})
    assert live.poll().status == "ok"
    assert live.poll().events == []


def test_legacy_adapter_alias_preserved():
    assert ActivityWatchSourceAdapter is ActivityWatchAdapter


# --- downstream: privacy, store, sessions ------------------------------


def test_native_events_pass_privacy_and_store_without_duplicates():
    from noema.infrastructure.database import SQLiteStore

    store = SQLiteStore(":memory:")
    try:
        filt = PrivacyFilter()
        clock = Clock(utc())
        source = NativeWindowsActivitySource(
            foreground_fn=ForegroundScript([("Code.exe", "a.py")]),
            clock=clock)
        event = source.poll().events[0]
        kept = filt.filter(event)
        assert kept is not None
        assert store.upsert_event(kept) == "inserted"
        # Identical re-emission converges (duplicate, not a second row).
        assert store.upsert_event(kept) == "duplicate"
        assert store.count() == 1
        # Heartbeat extension updates the same logical row.
        clock.advance(6)
        extended = source.poll().events[0]
        assert store.upsert_event(filt.filter(extended)) == "updated"
        assert store.count() == 1
    finally:
        store.close()


def test_blocked_app_still_filtered_for_native_rows():
    filt = PrivacyFilter()
    event = ActivityEvent(timestamp=utc(), duration=5.0, app="1Password",
                          title="vault", source="noema_native",
                          bucket_id="noema-window")
    assert filt.filter(event) is None


def test_native_events_build_sessions():
    clock = Clock(utc())
    script = ForegroundScript([("Code.exe", "a.py")] * 3 + [("Code.exe", "b.py")])
    source = NativeWindowsActivitySource(
        heartbeat_seconds=1.0, foreground_fn=script, clock=clock)
    rows = []
    for step in range(4):
        rows.extend(source.poll().events)
        clock.advance(2)
    sessions = Sessionizer().sessionize(rows)
    assert len(sessions) >= 1
    assert all(session.app == "Code.exe" for session in sessions)


def test_live_windows_probe_shape():
    """Live OS probe: asserts shape, not values (Windows only)."""
    source = NativeWindowsActivitySource()
    if not source.supported:
        pytest.skip("no foreground probe on this platform")
    result = source.poll()
    assert result.status in {"ok", "degraded", "error"}
    for event in result.events:
        assert isinstance(event, ActivityEvent)
        assert event.timestamp.tzinfo is not None
        assert event.duration >= 0


def test_live_presence_probe_shape():
    source = NativeAFKSource()
    if not source.supported:
        pytest.skip("no input clock on this platform")
    result = source.poll()
    assert result.status in {"ok", "degraded", "error"}
    for event in result.events:
        assert afk_status(event) in {"afk", "not-afk"}
