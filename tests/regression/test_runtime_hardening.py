import threading
import time
from datetime import datetime, timedelta, timezone

from noema.api import NoemaService
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore
from noema.runtime import NoemaDaemon, DaemonConfig
from noema.application.classification import Classifier


class EmptyActivityWatch:
    def list_buckets(self):
        return {}

    def get_events(self, bucket_id, start=None, end=None):
        return []


class SlowActivityWatch:
    def __init__(self):
        self.calls = 0

    def list_buckets(self):
        return {"window_laptop": {"hostname": "laptop"}}

    def get_events(self, bucket_id, start=None, end=None):
        self.calls += 1
        return [{
            "id": "runtime-event-{}".format(self.calls),
            "timestamp": ((end or datetime.now(timezone.utc)) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "duration": 600,
            "data": {"app": "VS Code", "title": "worker.py"},
        }]


class SlowProvider:
    model = "slow-test"

    def __init__(self):
        self.started = threading.Event()
        self.finished = threading.Event()

    def classify(self, session, prompt):
        self.started.set()
        time.sleep(0.2)
        self.finished.set()
        return {
            "category": "neutral", "productivity": "neutral",
            "activity": "Observed activity", "signal": "Observed evidence.",
            "confidence": 0.42,
        }


class FixedActivityWatch:
    def list_buckets(self):
        return {"window_laptop": {"hostname": "laptop"}}

    def get_events(self, bucket_id, start=None, end=None):
        return [{
            "id": "e2e-event",
            "timestamp": "2026-09-06T10:00:00Z",
            "duration": 600,
            "data": {"app": "VS Code", "title": "main.py"},
        }]


class StructuredProvider:
    model = "e2e-test"

    def classify(self, session, prompt):
        return {
            "category": "productive", "productivity": "productive",
            "activity": "Coding", "signal": "Observed editor evidence.",
            "confidence": 0.8,
        }


def make_daemon(store, activitywatch, classifier=None):
    service = NoemaService(
        ActivityWatchAdapter(activitywatch), store,
        classifier=classifier,
    )
    return NoemaDaemon(
        service,
        DaemonConfig(db_path=":memory:", lock_path=None, background_ai_enabled=True),
    )


def seed_batches(store, count):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for index in range(count):
        window_start = start + timedelta(minutes=index * 5)
        store.upsert_classification_batch(
            "seed-{}".format(index), window_start, window_start + timedelta(minutes=5),
            {"activity_ids": ["activity_{:03d}".format(index)]},
        )


def test_semantic_worker_drains_100_and_120_batches():
    for count in (100, 120):
        store = SQLiteStore()
        seed_batches(store, count)
        daemon = make_daemon(store, EmptyActivityWatch())
        assert daemon._run_named_worker("semantics") is True
        rows = store.query_classification_batches()
        statuses = [row["status"] for row in rows if row["batch_id"].startswith("seed-")]
        assert len(statuses) == count
        assert statuses.count("COMPLETED") == count
        assert statuses.count("PENDING") == 0
        assert statuses.count("PROCESSING") == 0
        store.close()


def test_daemon_restart_recovers_processing_batch_and_reprocesses():
    store = SQLiteStore()
    seed_batches(store, 1)
    assert store.claim_classification_batches()
    first = make_daemon(store, EmptyActivityWatch())
    assert store.query_classification_batches(status="RETRYABLE")
    assert first._run_named_worker("semantics") is True
    assert store.query_classification_batches(status="PROCESSING") == []
    assert store.query_classification_batches(status="RETRYABLE") == []
    assert store.query_classification_batches(status="COMPLETED")
    store.close()


def test_ingestion_worker_runs_while_semantic_provider_is_slow():
    store = SQLiteStore()
    activitywatch = SlowActivityWatch()
    provider = SlowProvider()
    daemon = make_daemon(store, activitywatch, Classifier(provider=provider))
    daemon._run_named_worker("ingest")

    semantic_thread = threading.Thread(target=lambda: daemon._run_named_worker("semantics"))
    semantic_thread.start()
    assert provider.started.wait(2)
    started = time.perf_counter()
    assert daemon._run_named_worker("ingest") is True
    ingest_elapsed = time.perf_counter() - started
    semantic_thread.join(2)

    assert provider.finished.is_set()
    assert activitywatch.calls >= 2
    assert ingest_elapsed < 0.15
    store.close()


def test_runtime_persists_five_minute_batch_and_classification():
    store = SQLiteStore()
    daemon = make_daemon(store, FixedActivityWatch(), Classifier(provider=StructuredProvider()))

    result = daemon.run_once(now="2026-09-06T10:10:00Z")

    assert result["ingest"]["ingested"]["accepted"] == 1
    batches = store.query_classification_batches()
    assert batches
    assert batches[-1]["status"] == "COMPLETED"
    assert batches[-1]["payload"]["activity_ids"]
    classifications = store.query_classifications()
    assert classifications
    assert classifications[0].classification_status == "classified"
    store.close()