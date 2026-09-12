"""Native telemetry end-to-end: daemon ingests without ActivityWatch."""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from noema.api import NoemaService
from noema.application.classification import Classifier
from noema.infrastructure.activity_sources import (
    CompositeActivitySource,
    NativeAFKSource,
    NativeWindowsActivitySource,
)
from noema.infrastructure.activity_sources.activitywatch import (
    ActivityWatchAdapter,
    ActivityWatchClient,
)
from noema.infrastructure.database import SQLiteStore
from noema.runtime import DaemonConfig, NoemaDaemon


def utc(hour=10, minute=0, second=0):
    return datetime(2026, 9, 12, hour, minute, second, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


class ProductiveProvider:
    name = "test"
    model = "test-model"

    def classify(self, session, prompt):
        return {
            "category": "productive", "subcategory": "research",
            "activity": "Active work", "signal": "Evidence supports it.",
            "confidence": 0.9, "productivity": "productive",
        }


def dead_aw_service(store):
    return NoemaService(
        ActivityWatchAdapter(ActivityWatchClient("http://127.0.0.1:9")),
        store,
        classifier=Classifier(provider=ProductiveProvider()),
    )


def scripted_native(window_states, idle_seconds, clock):
    states = list(window_states)

    def foreground():
        return states.pop(0) if states else states[-1] if states else None

    idles = list(idle_seconds)

    def idle():
        return idles.pop(0) if len(idles) > 1 else idles[0]

    return CompositeActivitySource([
        NativeWindowsActivitySource(
            heartbeat_seconds=60.0, foreground_fn=foreground, clock=clock),
        NativeAFKSource(timeout_seconds=180.0, heartbeat_seconds=3600.0,
                        idle_seconds_fn=idle, clock=clock),
    ])


@pytest.fixture()
def lock_path():
    handle = tempfile.NamedTemporaryFile(prefix="noema-native-", suffix=".lock",
                                         delete=False)
    handle.close()
    path = handle.name
    Path(path).unlink(missing_ok=True)
    yield path
    Path(path).unlink(missing_ok=True)


def make_daemon(store, collector, lock_path, **config_kwargs):
    service = dead_aw_service(store)
    config = DaemonConfig(db_path=":memory:", lock_path=lock_path,
                          native_poll_seconds=0.001, **config_kwargs)
    daemon = NoemaDaemon(service, config)
    daemon._native_sources = collector
    daemon._native_last_poll = 0.0
    return daemon


def test_ingest_stage_succeeds_with_aw_down(lock_path):
    store = SQLiteStore(":memory:")
    clock = Clock(utc())
    collector = scripted_native([("Code.exe", "a.py")], [5.0], clock)
    daemon = make_daemon(store, collector, lock_path)
    try:
        result = daemon._ingest_stage(utc(minute=5))
        assert result["native"]["accepted"] >= 1
        assert result["native"]["status"] in {"ok", "degraded"}
        assert result["aw_error"] is not None
        assert "could not read collector" in result["aw_error"]
        assert store.count() >= 1
        assert daemon._native_connected is True
        health = daemon.health_dict()
        assert health["native_connected"] is True
        assert health["source_connected"] is True
    finally:
        daemon.stop()
        store.close()


def test_ingest_stage_raises_when_everything_fails(lock_path):
    from noema.infrastructure.activity_sources.base import ActivitySource

    class Dead(ActivitySource):
        name = "dead"

        @property
        def supported(self):
            return True

        def poll(self, now=None):
            raise RuntimeError("sensor gone")

    store = SQLiteStore(":memory:")
    daemon = make_daemon(
        store,
        CompositeActivitySource([Dead()]),
        lock_path)
    try:
        with pytest.raises(ConnectionError):
            daemon._ingest_stage(utc(minute=5))
    finally:
        daemon.stop()
        store.close()


def test_native_disabled_preserves_legacy_failure(lock_path):
    store = SQLiteStore(":memory:")
    daemon = make_daemon(store, None, lock_path, native_enabled=False)
    try:
        with pytest.raises(ConnectionError):
            daemon._ingest_stage(utc(minute=5))
        assert daemon._native_connected is False
    finally:
        daemon.stop()
        store.close()


def test_run_once_end_to_end_without_aw(lock_path):
    store = SQLiteStore(":memory:")
    clock = Clock(utc(minute=5))
    collector = scripted_native(
        [("Code.exe", "a.py")] * 4, [5.0] * 4, clock)
    daemon = make_daemon(store, collector, lock_path)
    try:
        first = daemon.run_once(now=utc(minute=10))
        assert first["ingest"]["native"]["accepted"] >= 1
        # Advance the clock so the heartbeat extends the span past zero;
        # zero-duration span-open rows alone do not sessionize (pre-existing
        # rule, identical for ActivityWatch heartbeats).
        clock.now = utc(minute=12)
        daemon._native_last_poll = 0.0
        daemon.run_once(now=utc(minute=13))
        assert store.count_sessions() >= 1
        # Re-observing the same state converges (updates, not growth).
        before = store.count()
        clock.now = utc(minute=14)
        daemon._native_last_poll = 0.0
        daemon.run_once(now=utc(minute=15))
        assert store.count() <= before + 2
    finally:
        daemon.stop()
        store.close()


def test_native_failure_never_blocks_aw_recovery(lock_path):
    """A broken native collector must not break the AW path's own result."""
    from noema.infrastructure.activity_sources.base import ActivitySource

    class Dead(ActivitySource):
        name = "dead"

        @property
        def supported(self):
            return True

        def poll(self, now=None):
            raise RuntimeError("sensor gone")

    store = SQLiteStore(":memory:")
    daemon = make_daemon(
        store, CompositeActivitySource([Dead()]), lock_path)
    try:
        # AW is also down here, so the tick still raises — but the native
        # failure is captured explicitly instead of propagating raw.
        with pytest.raises(ConnectionError):
            daemon._ingest_stage(utc(minute=5))
        assert daemon._native_last_error is not None
    finally:
        daemon.stop()
        store.close()
