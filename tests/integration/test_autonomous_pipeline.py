"""Autonomous classification loop: scheduler, idempotency, retries, status."""

import io
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.domain.activity import ActivityEvent
from noema.runtime import NoemaDaemon, DaemonConfig
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification, Classifier
from noema.infrastructure.providers import ProviderChain, ProviderError
from noema.domain.meaningful import MeaningfulSession
from noema.infrastructure.ollama import OllamaError


def _test_path(name):
    path = Path.cwd() / "tests" / "noema" / name
    path.unlink(missing_ok=True)
    return path


def call_app(app, method, path, body=b""):
    result = {}

    def start_response(status, headers):
        result["status"] = status

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    response = b"".join(app(environ, start_response))
    return result["status"], json.loads(response.decode("utf-8"))


class CountingClient:
    model = "test-model"

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0
        self.prompts = []

    def generate_json(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return dict(self.payload)


def valid_payload(category="productive"):
    return {
        "category": category,
        "subcategory": "research",
        "activity": "Verifying autonomous behavior",
        "signal": "The observed evidence supports this result.",
        "topic": "autonomy",
        "service": "test",
        "intent_signal": "verification",
        "activity_type": "research",
        "productivity": "productive",
        "confidence": 0.9,
    }


def make_meaningful(tag, start="2026-09-06T10:00:00Z"):
    return MeaningfulSession(
        start_time=start,
        end_time="2026-09-06T10:05:00Z",
        device_set=("laptop",),
        activity_session_ids=("raw-{}".format(tag),),
    )


class EmptyAW:
    def list_buckets(self):
        return {}

    def get_events(self, bucket_id, start=None, end=None):
        return []


class OneEventAW:
    def list_buckets(self):
        return {"window_laptop": {"hostname": "laptop"}}

    def get_events(self, bucket_id, start=None, end=None):
        return [{
            "id": "auto-event",
            "timestamp": "2026-09-06T10:00:00Z",
            "duration": 120,
            "data": {"app": "VS Code", "title": "reward.py"},
        }]


def test_failed_attempt_records_retry_error_and_backoff():
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(), store,
        classifier=Classifier(client=CountingClient(error=OllamaError("offline"))),
    )
    session = make_meaningful("retry-1")
    store.insert_meaningful_session(session)

    (result,) = service.classify_meaningful_sessions([session], max_retries=5)

    assert result.classification_status == "pending"
    assert result.retry_count == 1
    assert result.last_error
    assert result.next_retry_at is not None
    stored = store.query_classifications(session_id=session.id, limit=1)[0]
    assert stored.retry_count == 1
    assert stored.last_error
    store.close()


def test_terminal_failure_is_visible_and_never_retried():
    store = SQLiteStore()
    client = CountingClient(error=OllamaError("offline"))
    service = NoemaService(ActivityWatchAdapter(), store, classifier=Classifier(client=client))
    session = make_meaningful("terminal-1")
    store.insert_meaningful_session(session)

    service.classify_meaningful_sessions([session], max_retries=2)
    service.classify_meaningful_sessions([session], max_retries=2)
    # Two explicit attempts × 3 transport retries each. The scheduler itself
    # would not re-attempt this row again (see eligibility below).
    assert client.calls == 6
    # Third attempt would exceed max_retries: the scheduler must skip it.
    eligible = service.pending_classification_sessions(
        store.query_meaningful_sessions(limit=100000), max_retries=2)
    assert [item.id for item in eligible] == []
    coverage = service.classification_coverage(max_retries=2)
    assert coverage["failed"] == 1
    assert coverage["pending"] == 0
    assert coverage["classified"] == 0
    store.close()


def test_backoff_defers_retry_until_due():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store,
                                  classifier=Classifier(client=CountingClient(error=OllamaError("down"))))
    session = make_meaningful("backoff-1")
    store.insert_meaningful_session(session)
    service.classify_meaningful_sessions([session], max_retries=5)

    now = datetime.now(timezone.utc)
    assert service.pending_classification_sessions(
        store.query_meaningful_sessions(limit=100000)) == []
    future = now + timedelta(hours=3)
    assert [item.id for item in service.pending_classification_sessions(
        store.query_meaningful_sessions(limit=100000), now=future)] == [session.id]
    store.close()


def test_success_resets_retry_counters():
    store = SQLiteStore()
    failing = CountingClient(error=OllamaError("down"))
    service = NoemaService(ActivityWatchAdapter(), store, classifier=Classifier(client=failing))
    session = make_meaningful("reset-1")
    store.insert_meaningful_session(session)
    service.classify_meaningful_sessions([session], max_retries=5)
    assert store.query_classifications(session_id=session.id, limit=1)[0].retry_count == 1

    service.classifier = Classifier(client=CountingClient(payload=valid_payload()))
    (result,) = service.classify_meaningful_sessions([session], max_retries=5)
    assert result.classification_status == "classified"
    assert result.retry_count == 0
    assert result.last_error is None
    assert result.next_retry_at is None
    store.close()


def test_version_bump_requeues_classified_rows():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store,
                                  classifier=Classifier(client=CountingClient(payload=valid_payload())))
    session = make_meaningful("version-1")
    store.insert_meaningful_session(session)
    service.classify_meaningful_sessions([session], classifier_version="1")

    assert service.pending_classification_sessions(
        store.query_meaningful_sessions(limit=100000), classifier_version="1") == []
    due = service.pending_classification_sessions(
        store.query_meaningful_sessions(limit=100000), classifier_version="2")
    assert [item.id for item in due] == [session.id]
    store.close()


def test_second_cycle_makes_zero_llm_calls():
    store = SQLiteStore()
    client = CountingClient(payload=valid_payload())
    service = NoemaService(
        ActivityWatchAdapter(OneEventAW()), store, classifier=Classifier(client=client))
    daemon = NoemaDaemon(
        service,
        DaemonConfig(db_path=":memory:", lock_path=str(_test_path(".auto-idem.lock"))),
    )
    first = daemon.run_once(now="2026-09-06T10:10:00Z")
    assert first["semantics"]["classifications"] == 1
    calls_after_first = client.calls
    assert calls_after_first >= 1
    second = daemon.run_once(now="2026-09-06T10:20:00Z")
    assert second["semantics"]["classifications"] == 0
    assert client.calls == calls_after_first
    store.close()


def test_empty_cycle_makes_no_llm_calls():
    store = SQLiteStore()
    client = CountingClient(payload=valid_payload())
    service = NoemaService(
        ActivityWatchAdapter(EmptyAW()), store, classifier=Classifier(client=client))
    daemon = NoemaDaemon(
        service,
        DaemonConfig(db_path=":memory:", lock_path=str(_test_path(".auto-empty.lock"))),
    )
    daemon.run_once(now="2026-09-06T10:10:00Z")
    assert client.calls == 0
    store.close()


def test_daemon_classifies_automatically_without_manual_calls():
    store = SQLiteStore()
    client = CountingClient(payload=valid_payload())
    service = NoemaService(
        ActivityWatchAdapter(OneEventAW()), store, classifier=Classifier(client=client))
    # Pre-build outside the tiny test window so the scheduler's unbounded
    # backlog scan finds the session, exactly like production history.
    service.ingest_events([ActivityEvent(
        timestamp="2026-09-06T10:00:00Z", duration=120, device="laptop",
        app="VS Code", title="reward.py", bucket_id="window_laptop",
        source_event_id="auto-prebuilt",
    )])
    service.sessionize_stored_events()
    service.build_meaningful_sessions(start="2026-09-05T00:00:00Z", end="2026-09-07T00:00:00Z")
    assert len(store.query_meaningful_sessions(limit=100000)) >= 1
    daemon = NoemaDaemon(
        service,
        DaemonConfig(
            db_path=":memory:",
            lock_path=str(_test_path(".auto-loop.lock")),
            ingest_interval_seconds=0.05,
            classification_interval_seconds=0.1,
            behavior_interval_seconds=60,
            outcome_interval_seconds=60,
            report_interval_seconds=3600,
        ),
    )
    try:
        daemon.start()
        deadline = time.monotonic() + 5.0
        classified = []
        while time.monotonic() < deadline:
            classified = [item for item in store.query_classifications(limit=100000)
                          if item.classification_status == "classified"]
            if classified:
                break
            time.sleep(0.05)
        assert classified, "scheduler never classified without manual action"
        assert daemon.health_dict()["workers"]["semantics"]["run_count"] >= 1
    finally:
        daemon.stop()
        store.close()


def test_startup_schedules_first_pass_within_seconds():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(EmptyAW()), store)
    daemon = NoemaDaemon(
        service,
        DaemonConfig(db_path=":memory:", lock_path=str(_test_path(".auto-startup.lock"))),
    )
    try:
        daemon.start()
        wait = daemon._next_due["semantics"] - time.monotonic()
        assert 0 < wait <= 20
    finally:
        daemon.stop()
        store.close()


def test_20_minute_cadence_is_the_default():
    from noema.config.settings import DaemonConfig as Cfg
    config = Cfg()
    assert config.classification_interval_seconds == 1200
    assert config.classification_interval_seconds / 60 == 20.0


def test_20_minute_interval_can_be_set_by_canonical_env():
    import os as _os
    from noema.config.settings import DaemonConfig as Cfg
    saved = _os.environ.get("NOEMA_CLASSIFICATION_INTERVAL_MINUTES")
    _os.environ["NOEMA_CLASSIFICATION_INTERVAL_MINUTES"] = "20"
    try:
        config = Cfg.from_environment()
        assert config.classification_interval_seconds == 1200.0
    finally:
        if saved is None:
            _os.environ.pop("NOEMA_CLASSIFICATION_INTERVAL_MINUTES", None)
        else:
            _os.environ["NOEMA_CLASSIFICATION_INTERVAL_MINUTES"] = saved


def test_no_new_work_cycle_makes_zero_api_calls():
    """Spec 25: a cycle with no pending/eligible work must not call a model."""
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(EmptyAW()), store)
    session = make_meaningful("already-done", start="2026-09-06T09:00:00Z")
    service.store.insert_meaningful_sessions([session])
    service.store.insert_classification(Classification(
        session.id, "productive", activity_type="coding", productivity="productive",
        confidence=0.9, source="gemini", classification_status="classified",
        model="gemini-3.5-flash-lite", classifier_version="1",
    ))
    now = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    pending = service.pending_classification_sessions(
        [session], now=now, classifier_version="1", max_retries=5)
    assert pending == []


def test_unchanged_classified_episode_is_not_reclassified():
    """Spec 26: an unchanged, already-classified episode under the current
    version is not eligible again; a classifier-version bump explicitly
    requeues it."""
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(EmptyAW()), store)
    session = make_meaningful("unchanged", start="2026-09-06T09:00:00Z")
    service.store.insert_meaningful_sessions([session])
    service.store.insert_classification(Classification(
        session.id, "neutral", activity_type="browsing", productivity="neutral",
        confidence=0.4, source="gemini", classification_status="classified",
        model="gemini-3.5-flash-lite", classifier_version="1",
    ))
    now = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    pending = service.pending_classification_sessions(
        [session], now=now, classifier_version="1", max_retries=5)
    assert pending == []
    requeued = service.pending_classification_sessions(
        [session], now=now, classifier_version="2", max_retries=5)
    assert requeued == [session]


def test_provider_error_never_kills_the_cycle():
    store = SQLiteStore()
    client = CountingClient(error=OllamaError("boom"))
    service = NoemaService(
        ActivityWatchAdapter(OneEventAW()), store, classifier=Classifier(client=client))
    daemon = NoemaDaemon(
        service,
        DaemonConfig(db_path=":memory:", lock_path=str(_test_path(".auto-survive.lock"))),
    )
    result = daemon.run_once(now="2026-09-06T10:10:00Z")
    assert result["semantics"]["classifications"] == 1
    assert result["semantics"]["failed"] == 1
    # run_once drives stages directly (no worker bookkeeping); the scheduler
    # itself must stay usable, and the failed row must back off: a cycle one
    # minute later finds nothing due.
    assert daemon.health_dict()["workers"]["semantics"]["status"] != "error"
    calls_after_first = client.calls
    again = daemon.run_once(now="2026-09-06T10:11:00Z")
    assert again["semantics"]["classifications"] == 0
    assert client.calls == calls_after_first
    rows = store.query_classifications(limit=100000)
    assert len(rows) == 1
    assert rows[0].retry_count == 1
    assert rows[0].last_error
    store.close()


def test_status_endpoint_reports_scheduler_truth():
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(EmptyAW()), store,
        classifier=Classifier(client=CountingClient(payload=valid_payload())))
    daemon = NoemaDaemon(
        service,
        DaemonConfig(db_path=":memory:", lock_path=str(_test_path(".auto-status.lock"))),
    )
    app = create_app(service, daemon=daemon)
    try:
        status, payload = call_app(app, "GET", "/api/classification/status")
        assert status.startswith("200")
        for key in ("enabled", "intervalMinutes", "status", "lastRunAt", "nextRunAt",
                    "processedLastRun", "failedLastRun", "pendingCount", "processingCount",
                    "failedCount", "classifiedCount", "totalEligibleCount", "coveragePercent",
                    "latestClassificationAt", "provider", "model", "quota"):
            assert key in payload, key
        assert payload["enabled"] is True
        assert payload["intervalMinutes"] == 20
        assert payload["status"] in {"idle", "running", "disabled", "error"}
    finally:
        store.close()


def test_summary_separates_unclassified_from_neutral():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    service.ingest_events([
        ActivityEvent(
            timestamp="2026-09-06T10:00:00Z", duration=60, device="laptop",
            app="Firefox", title="Docs", domain="docs.python.org",
            bucket_id="window", source_event_id="s1",
        ),
        ActivityEvent(
            timestamp="2026-09-06T11:00:00Z", duration=60, device="laptop",
            app="MysteryApp", title="Unknown window",
            bucket_id="window", source_event_id="s2",
        ),
    ])
    sessions = service.sessionize_stored_events()
    assert len(sessions) == 2
    service.classifier = Classifier(client=CountingClient(payload=valid_payload()))
    service.classify_sessions([sessions[0]])
    summary = service.daily_summary(
        datetime(2026, 9, 6, tzinfo=timezone.utc), datetime(2026, 9, 7, tzinfo=timezone.utc))

    assert summary["total_time"] == 120.0
    assert summary["productive_time"] == 60.0
    assert summary["neutral_time"] == 0.0
    assert summary["unclassified_time"] == 60.0
    assert summary["total_time"] == (
        summary["productive_time"] + summary["distractive_time"]
        + summary["neutral_time"] + summary["unclassified_time"]
    )
    assert summary["productivity_score"] == 100
    store.close()


def test_exhausted_model_is_skipped_without_api_calls_or_lost_work():
    from noema.infrastructure.providers import MODEL_LIMITS

    seen = []

    class FakeBatchHost:
        name = "gemini"

        def __init__(self, model):
            self.model = model
            self.calls = 0

        def classify_batch(self, session_ids, evidences):
            self.calls += 1
            seen.append(self.model)
            raise ProviderError("offline")

        def classify(self, session, prompt):
            self.calls += 1
            raise ProviderError("offline")

    class Fallback:
        name = "ollama"
        model = "llama3.2:3b"

        def __init__(self):
            self.calls = 0

        def classify(self, session, prompt):
            self.calls += 1
            return dict(valid_payload(), category="neutral", productivity="neutral")

    first = FakeBatchHost("gemini-2.5-flash")
    fallback = Fallback()
    chain = ProviderChain(hosted=[first], ollama=fallback, include_ollama=True)
    for _ in range(MODEL_LIMITS["gemini-2.5-flash"][2]):
        chain.ledger.consume("llm", "gemini-2.5-flash", 700)

    session = make_meaningful("quota-1")
    payload = chain.classify(session, "prompt")

    assert first.calls == 0
    assert fallback.calls == 1
    assert payload["category"] == "neutral"
