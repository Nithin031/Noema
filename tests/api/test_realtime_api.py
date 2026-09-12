"""Real-time API contract: status, detections, manual evaluation."""

import io
import json
from datetime import timedelta


from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.runtime import NoemaDaemon, DaemonConfig
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classifier
from noema.application.realtime import FastModelVerifier

from test_realtime_pipeline import (
    NOW,
    ConfirmLeg,
    classify_all,
    distracting_payload,
    make_service,
    make_sessions,
)


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


def populated():
    store, service = make_service(distracting_payload())
    sessions = make_sessions("d", 25)
    for session in sessions:
        store.insert_meaningful_session(session)
    classify_all(service, store, sessions)
    service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())
    return store, service, sessions


def test_realtime_status_reports_tracker_and_wiring():
    store, service, _ = populated()
    app = create_app(service)
    status, payload = call_app(app, "GET", "/api/realtime/status")
    assert status.startswith("200")
    assert payload["tracker"]["state"] == "NORMAL"
    assert payload["detector"]["enter_threshold"] == 0.70
    assert payload["fast_verifier_configured"] is True
    assert payload["last_detection"] is None
    store.close()


def test_detections_endpoint_lists_and_filters():
    store, service, sessions = populated()
    service.evaluate_realtime(now=NOW)
    app = create_app(service)
    status, payload = call_app(app, "GET", "/api/detections")
    assert status.startswith("200")
    assert len(payload["detections"]) >= 2
    first = payload["detections"][0]
    for key in ("detection_id", "timestamp", "decision", "candidate_score",
                "tracker_state", "reason"):
        assert key in first
    status, empty = call_app(app, "GET", "/api/detections")
    assert status.startswith("200")
    store.close()


def test_manual_evaluate_runs_through_daemon_lane():
    from datetime import datetime, timezone

    from test_realtime_pipeline import classify_all, make_sessions
    # The daemon lane evaluates at wall-clock now, so anchor the scenario
    # to wall-clock now instead of the fixed NOW used elsewhere.
    live_now = datetime.now(timezone.utc)
    store, service = make_service(distracting_payload())
    sessions = make_sessions("live", 25, end=live_now)
    for session in sessions:
        store.insert_meaningful_session(session)
    classify_all(service, store, sessions)
    service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())
    config = DaemonConfig(db_path=":memory:", lock_path=":memory:")
    daemon = NoemaDaemon(service, config)
    app = create_app(service, daemon=daemon)
    status, payload = call_app(app, "POST", "/api/realtime/evaluate", body=b"{}")
    assert status.startswith("200")
    assert payload["state"] == "INTERVENTION_COOLDOWN"
    status, status_payload = call_app(app, "GET", "/api/realtime/status")
    assert status_payload["tracker"]["state"] == "INTERVENTION_COOLDOWN"
    assert status_payload["last_detection"] is not None
    store.close()


def test_manual_evaluate_without_daemon_is_503():
    store, service, _ = populated()
    app = create_app(service)
    status, payload = call_app(app, "POST", "/api/realtime/evaluate", body=b"{}")
    assert status.startswith("503")
    store.close()


def test_quotas_distinguish_shared_and_model_scopes():
    from noema.infrastructure.providers import ProviderChain

    class StubProvider:
        def __init__(self, name, model):
            self.name = name
            self.model = model

    chain = ProviderChain(
        hosted=[StubProvider("gemini", "gemini-3.7-flash")],
        ollama=StubProvider("ollama", "llama3.2:3b"),
        openrouter=[StubProvider(
            "openrouter",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")],
        usage_path=None,
    )

    class ChainClassifier:
        provider = chain

    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    service.classifier = ChainClassifier()
    quotas = service.provider_quotas()

    assert quotas["openrouter_shared_quota"] is True
    assert quotas["openrouter_quota_note"]
    assert quotas["fast_distraction_model"] == (
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")
    rows = {row["model"]: row for row in quotas["models"]}
    fast = rows["nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"]
    assert fast["quota_scope"] == "shared-openrouter-guards"
    assert fast["role"] == "fast" and fast["fast_path"] is True
    assert fast["context_window"] == 256000 and fast["max_output"] == 65536
    assert fast["priority"] == 1 and fast["enabled"] is True
    gemini = rows["gemini-3.7-flash"]
    assert gemini["quota_scope"] == "model-specific"
    assert gemini["role"] == "fallback"
    for embedding in quotas["embeddings"]:
        # Unknown per-minute usage is null, never zero.
        assert embedding["rpm"]["used"] is None
        assert embedding["quota_scope"] == "model-specific-rpd-only"
    store.close()
