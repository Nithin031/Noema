import io
import json

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.domain.activity import ActivityEvent
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classifier
from noema.infrastructure.providers import ProviderChain, ProviderError


def call_app(app, path, method="GET", body=b""):
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
    raw = b"".join(app(environ, start_response))
    return result["status"], json.loads(raw.decode("utf-8"))


class OfflineHost:
    name = "gemini"
    model = "gemini-2.5-flash"

    def classify(self, session, prompt):
        raise ProviderError("offline")


def test_quotas_reports_limits_used_and_left_per_model():
    ollama = OfflineHost()
    ollama.name = "ollama"
    ollama.model = "llama3.2:3b"
    chain = ProviderChain(hosted=[OfflineHost()], ollama=ollama)
    service = NoemaService(ActivityWatchAdapter(), SQLiteStore(), classifier=Classifier(provider=chain))
    app = create_app(service)

    status, payload = call_app(app, "/api/debug/quotas")

    assert status.startswith("200")
    assert payload["provider"] == "hosted_chain"
    by_model = {row["model"]: row for row in payload["models"]}
    assert by_model["gemini-2.5-flash"]["rpm"]["limit"] == 5
    assert by_model["gemini-2.5-flash"]["rpd"]["limit"] == 20
    assert by_model["gemini-2.5-flash"]["rpd"]["left"] == 20
    assert by_model["gemini-2.5-flash"]["status"] == "ok"
    assert any(row["model"] == "gemini-embedding-001" for row in payload["embeddings"])
    service.store.close()


def test_quotas_exhausted_model_shows_no_remaining():
    ollama = OfflineHost()
    ollama.name = "ollama"
    ollama.model = "llama3.2:3b"
    chain = ProviderChain(hosted=[OfflineHost()], ollama=ollama)
    for _ in range(20):
        chain.ledger.consume("llm", "gemini-2.5-flash", 700)
    service = NoemaService(ActivityWatchAdapter(), SQLiteStore(), classifier=Classifier(provider=chain))
    app = create_app(service)

    status, payload = call_app(app, "/api/debug/quotas")

    assert status.startswith("200")
    row = next(item for item in payload["models"] if item["model"] == "gemini-2.5-flash")
    assert row["rpd"] == {"limit": 20, "used": 20, "left": 0}
    assert row["status"] == "exhausted"
    service.store.close()


def test_recent_activity_supports_rolling_24h_range():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    service.ingest_events([
        ActivityEvent(
            timestamp="2026-09-06T10:00:00Z", duration=60, device="laptop",
            app="Firefox", title="Docs", domain="docs.python.org",
            bucket_id="window", source_event_id="in-24h",
        ),
    ])
    app = create_app(service)

    status, payload = call_app(app, "/api/dashboard/recent-activity?range=24h&limit=50")

    assert status.startswith("200")
    assert payload["range"] == "24h"
    assert isinstance(payload["activities"], list)
    service.store.close()


def test_classification_status_quota_carries_local_estimate_flag():
    """requestsRemaining in /api/classification/status is a local ledger value.

    The field must never be presented as exact provider-authoritative quota.
    Regression for the missing isLocalEstimate / quotaScope labels that let
    callers tell the difference between a model-specific cap and a shared guard.
    """
    import tempfile
    import os
    from noema.runtime import NoemaDaemon, DaemonConfig

    ollama = OfflineHost()
    ollama.name = "ollama"
    ollama.model = "llama3.2:3b"
    chain = ProviderChain(hosted=[OfflineHost()], ollama=ollama, usage_path=None)
    service = NoemaService(
        ActivityWatchAdapter(),
        SQLiteStore(),
        classifier=Classifier(provider=chain),
    )
    lock_path = tempfile.mktemp(suffix=".lock")
    try:
        config = DaemonConfig(db_path=":memory:", lock_path=lock_path)
        daemon = NoemaDaemon(service, config)
        app = create_app(service, daemon=daemon)

        status, payload = call_app(app, "/api/classification/status")

        assert status.startswith("200")
        quota = payload["quota"]
        # Must carry explicit local-estimate flag — the value is never
        # provider-authoritative.
        assert quota.get("isLocalEstimate") is True, (
            "quota.isLocalEstimate must be True; got {}".format(quota)
        )
        # Must carry scope so callers know whether the limit is per-model or a
        # conservative shared guard.
        assert "quotaScope" in quota, (
            "quota.quotaScope must be present; got {}".format(quota)
        )
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass
        service.store.close()
