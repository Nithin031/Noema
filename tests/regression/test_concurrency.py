"""Concurrency regression: scheduler + manual run + polling + realtime +
provider calls at once. No deadlocks, no corruption, no duplicate work."""

import threading
import time
from datetime import datetime, timedelta, timezone

from noema.api import NoemaService, create_app
from noema.application.classification import Classifier
from noema.domain.activity import ActivityEvent
from noema.infrastructure.activity_sources.activitywatch import (
    ActivityWatchAdapter,
)
from noema.infrastructure.database import SQLiteStore
from noema.runtime import DaemonConfig, NoemaDaemon

import io
import json


class BurstActivityWatch:
    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def list_buckets(self):
        return {"window_laptop": {"hostname": "laptop"}}

    def get_events(self, bucket_id, start=None, end=None):
        with self._lock:
            self.calls += 1
            tag = self.calls
        base = (end or datetime.now(timezone.utc)) - timedelta(seconds=2)
        return [{
            "id": "burst-{}-{}".format(tag, index),
            "timestamp": (base - timedelta(seconds=index)).isoformat().replace("+00:00", "Z"),
            "duration": 1,
            "device": "laptop",
            "data": {"app": "Code.exe", "title": "work-{}.py".format(index)},
        } for index in range(5)]


class QuickProvider:
    name = "mock"
    model = "mock-concurrency"

    def classify(self, session, prompt):
        time.sleep(0.01)
        return {
            "category": "productive", "productivity": "productive",
            "activity": "Observed activity", "signal": "Observed evidence.",
            "confidence": 0.7,
        }

    def count_tokens(self, prompt):
        return 100


def call_app(app, path):
    result = {}

    def start_response(status, headers):
        result["status"] = status

    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": path.split("?", 1)[0],
               "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
               "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO()}
    raw = b"".join(app(environ, start_response))
    return result["status"], json.loads(raw.decode("utf-8"))


def test_concurrent_scheduler_manual_polling_realtime_and_provider(tmp_path):
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(BurstActivityWatch()), store,
        classifier=Classifier(provider=QuickProvider()))
    config = DaemonConfig(
        db_path=":memory:", lock_path=str(tmp_path / "concurrency.lock"),
        ingest_interval_seconds=0.05, behavior_interval_seconds=0.2,
        outcome_interval_seconds=0.2, realtime_eval_interval_seconds=0.2,
        classification_interval_seconds=3600, background_ai_enabled=False)
    daemon = NoemaDaemon(service, config)
    app = create_app(service, daemon=daemon)
    errors = []
    stop = threading.Event()

    def poll():
        try:
            while not stop.is_set():
                status, _ = call_app(app, "/api/dashboard/summary?range=today")
                assert status.startswith("200")
                status, _ = call_app(app, "/api/metrics/summary")
                assert status.startswith("200")
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    def manual():
        try:
            for _ in range(3):
                service.evaluate_realtime()
                time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    daemon.start()
    try:
        workers = [threading.Thread(target=poll) for _ in range(3)]
        workers.append(threading.Thread(target=manual))
        for worker in workers:
            worker.start()
        workers[-1].join(timeout=30)
        assert not workers[-1].is_alive(), "manual worker deadlocked"
        stop.set()
        for worker in workers[:-1]:
            worker.join(timeout=30)
            assert not worker.is_alive(), "poll worker deadlocked"
    finally:
        daemon.stop()
        stop.set()
    assert errors == []
    # No duplicate classifications for the same session.
    seen = store.query_classifications(limit=100000)
    by_session = {}
    for item in seen:
        by_session.setdefault(item.session_id, []).append(item)
    assert all(len(items) == 1 for items in by_session.values())
    # Database still coherent.
    integrity = store._connection.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok"
    store.close()
