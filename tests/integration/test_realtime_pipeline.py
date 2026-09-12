"""Real-time pipeline integration: sessions → windows → detector → verify.

Covers the autonomous detector (not the diagnostic fast-check endpoint):
rolling features feed the candidate detector, fresh candidates verify
through a fake fast leg, concerning confirmations attempt intervention
only on existing actionable behavior evidence, and every meaningful
decision persists exactly once.
"""

from datetime import datetime, timedelta, timezone

from noema.api import NoemaService
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.runtime import NoemaDaemon, DaemonConfig
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classifier
from noema.domain.meaningful import MeaningfulSession
from noema.infrastructure.ollama import OllamaError
from noema.application.realtime import FastModelVerifier

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class CountingClient:
    model = "test-model"

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = 0

    def generate_json(self, prompt):
        self.calls += 1
        if self.error:
            raise self.error
        return dict(self.payload)


def distracting_payload():
    return {
        "category": "distractive",
        "subcategory": "social_media",
        "activity": "Browsing Instagram",
        "signal": "The page identifies Instagram social consumption.",
        "topic": "social media",
        "project": None,
        "service": "Instagram",
        "intent_signal": "casual social browsing",
        "activity_type": "browsing",
        "productivity": "distracting",
        "confidence": 0.85,
    }


def productive_payload():
    payload = distracting_payload()
    payload.update({
        "category": "productive",
        "activity": "Working on Noema",
        "signal": "Editor evidence shows active development.",
        "topic": "activity intelligence",
        "activity_type": "coding",
        "productivity": "productive",
        "confidence": 0.9,
    })
    return payload


def make_sessions(tag, productivity_minutes, topic="Instagram", end=NOW, size=5):
    sessions = []
    cursor = end
    for index in range(productivity_minutes // size):
        start = cursor - timedelta(minutes=size)
        sessions.append(MeaningfulSession(
            start_time=start.isoformat().replace("+00:00", "Z"),
            end_time=cursor.isoformat().replace("+00:00", "Z"),
            device_set=("laptop",),
            activity_session_ids=("raw-{}{}".format(tag, index),),
            primary_topic=topic,
        ))
        cursor = start
    return sorted(sessions, key=lambda item: item.start_time)


def make_service(payload):
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(), store,
        classifier=Classifier(client=CountingClient(payload=payload)),
    )
    return store, service


def classify_all(service, store, sessions):
    service.classify_meaningful_sessions(sessions, max_retries=5)
    stored = {
        item.session_id: item for item in store.query_classifications(limit=100000)
    }
    service.evaluate_behavior(sessions, stored, {}, persist=True)


class ConfirmLeg:
    name = "openrouter"
    model = "fast-test-model"

    def __init__(self):
        self.calls = 0

    def complete_json(self, prompt, max_tokens=300):
        self.calls += 1
        return {
            "concerning": True,
            "severity": 4,
            "confidence": 0.88,
            "reason": "sustained drift with repeated switches",
            "recommended_intervention": "reframe",
        }


# MANDATORY TEST 3+10: sustained distraction → candidate → confirmed →
# intervention → (outcome measured separately by the outcome worker).
def test_sustained_distraction_runs_the_full_loop():
    store, service = make_service(distracting_payload())
    sessions = make_sessions("d", 25)
    for session in sessions:
        store.insert_meaningful_session(session)
    classify_all(service, store, sessions)
    service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())

    before = len(store.query_classifications(limit=100000))
    result = service.evaluate_realtime(now=NOW, execute_intervention=True)

    assert result["state"] == "INTERVENTION_COOLDOWN"
    assert result["verification"]["concerning"] is True
    assert result["intervention"]["status"] == "EXECUTED"
    assert result["intervention"]["mode"] == "NOTIFICATION"
    decisions = [item["decision"] for item in result["detections"]]
    assert "verified_concerning" in decisions
    assert "intervention_triggered" in decisions
    # Real-time detection never writes classifications.
    assert len(store.query_classifications(limit=100000)) == before
    interventions = store.query_interventions(limit=100000)
    assert len(interventions) == 1
    assert interventions[0].status.value == "EXECUTED"
    # Tracker state survives through the persisted snapshot.
    assert store.get_state("realtime.tracker")
    store.close()


def test_second_tick_does_not_reverify_or_reintervene():
    store, service = make_service(distracting_payload())
    sessions = make_sessions("d", 25)
    for session in sessions:
        store.insert_meaningful_session(session)
    classify_all(service, store, sessions)
    leg = ConfirmLeg()
    service.fast_verifier = FastModelVerifier(fast_openrouter=leg)

    first = service.evaluate_realtime(now=NOW, execute_intervention=True)
    assert leg.calls == 1
    second = service.evaluate_realtime(
        now=NOW + timedelta(seconds=60), execute_intervention=True)
    # Cooling down: no second model call, no second intervention.
    assert leg.calls == 1
    assert len(store.query_interventions(limit=100000)) == 1
    assert second["state"] == "INTERVENTION_COOLDOWN"
    store.close()


# MANDATORY TEST 1+5: productive work → NORMAL, silent, zero model calls.
def test_productive_work_stays_silent():
    store, service = make_service(productive_payload())
    sessions = make_sessions("p", 50, topic="reward.py")
    for session in sessions:
        store.insert_meaningful_session(session)
    classify_all(service, store, sessions)
    leg = ConfirmLeg()
    service.fast_verifier = FastModelVerifier(fast_openrouter=leg)

    result = service.evaluate_realtime(now=NOW)

    assert result["state"] == "NORMAL"
    assert leg.calls == 0
    assert result["detections"] == []
    assert result["verification"] is None
    assert result["intervention"] is None
    assert store.query_detections(limit=10) == []
    store.close()


def test_afk_vetoes_the_whole_evaluation():
    store, service = make_service(distracting_payload())
    sessions = make_sessions("d", 25)
    for session in sessions:
        store.insert_meaningful_session(session)
    classify_all(service, store, sessions)
    leg = ConfirmLeg()
    service.fast_verifier = FastModelVerifier(fast_openrouter=leg)
    service.presence_snapshot = lambda now=None: {
        "state": "afk", "current_session": {"session_id": sessions[-1].id}}

    result = service.evaluate_realtime(now=NOW)

    assert result["presence"] == "afk"
    assert leg.calls == 0
    assert result.get("intervention") is None
    assert result.get("verification") is None
    store.close()


def test_detection_rows_are_idempotent_and_queryable():
    store, service = make_service(distracting_payload())
    sessions = make_sessions("d", 25)
    for session in sessions:
        store.insert_meaningful_session(session)
    classify_all(service, store, sessions)
    service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())

    service.evaluate_realtime(now=NOW)
    count = len(store.query_detections(limit=100))
    assert count >= 2
    # Replaying the identical tick cannot duplicate rows.
    service.evaluate_realtime(now=NOW)
    assert len(store.query_detections(limit=100)) == count
    scoped = store.query_detections(
        session_id=sessions[-1].id, limit=100)
    assert all(item.session_id == sessions[-1].id for item in scoped)
    status = service.realtime_status()
    assert status["tracker"]["state"] == "INTERVENTION_COOLDOWN"
    assert status["last_detection"]["decision"] in {
        "verified_concerning", "intervention_triggered"}
    store.close()


def test_corrupt_tracker_snapshot_fails_safe_to_normal():
    store, service = make_service(productive_payload())
    store.set_state("realtime.tracker", "{not valid json")
    result = service.evaluate_realtime(now=NOW)
    assert result["state"] == "NORMAL"
    assert result["tracker"]["state"] == "NORMAL"
    store.close()


def test_daemon_realtime_worker_is_independent_and_disablable():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    config = DaemonConfig(db_path=":memory:", lock_path=":memory:")
    daemon = NoemaDaemon(service, config)
    assert "realtime" in daemon._workers
    assert daemon._interval_for("realtime") == config.realtime_eval_interval_seconds

    disabled = DaemonConfig(
        db_path=":memory:", lock_path=":memory:", realtime_enabled=False)
    quiet = NoemaDaemon(service, disabled)
    assert quiet._workers["realtime"].status == "disabled"
    assert quiet._run_named_worker("realtime") is False
    store.close()
