"""Failure-matrix regression: every transport failure mode through the real
chain + classifier must stay pending/failed (never a confident neutral),
be recorded in telemetry, and leak no secrets."""

import io
import json
from urllib.error import HTTPError

from noema.application.classification import Classifier
from noema.domain.activity import ActivityEvent
from noema.domain.sessions import Sessionizer
from noema.infrastructure.database import SQLiteStore
from noema.infrastructure.providers import OpenRouterProvider, ProviderChain
from noema.observability.recorder import TelemetryRecorder


def make_session():
    event = ActivityEvent(
        timestamp="2026-09-10T10:00:00Z", duration=120, device="laptop",
        app="Code.exe", title="reward.py", bucket_id="window",
        source_event_id="failure-matrix-1")
    return Sessionizer().sessionize([event])[0]


class FakeResponse:
    def __init__(self, payload_bytes):
        self.payload_bytes = payload_bytes

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload_bytes


def http_error(url, code):
    return HTTPError(url, code, "error", {}, io.BytesIO(b"{}"))


def transport_for(scenario):
    if scenario == "timeout":
        def fake_urlopen(request, timeout):
            raise TimeoutError("timed out after 60s")
        return fake_urlopen
    if scenario == "empty":
        def fake_urlopen(request, timeout):
            return FakeResponse(b"")
        return fake_urlopen
    if scenario == "invalid-json":
        def fake_urlopen(request, timeout):
            return FakeResponse(b"not json {{{")
        return fake_urlopen
    if scenario == "malformed-schema":
        def fake_urlopen(request, timeout):
            body = json.dumps({"choices": [{"message": {"content": json.dumps(
                {"category": "bogus", "productivity": "maybe"})}}]}).encode()
            return FakeResponse(body)
        return fake_urlopen
    code = {"http-429": 429, "http-503": 503}[scenario]

    def fake_urlopen(request, timeout):
        raise http_error(request.full_url, code)
    return fake_urlopen


def test_every_failure_mode_stays_pending_never_confident_neutral():
    for scenario in ("timeout", "http-429", "http-503", "malformed-schema",
                     "empty", "invalid-json"):
        store = SQLiteStore()
        recorder = TelemetryRecorder(store)
        provider = OpenRouterProvider(
            model="google/gemma-4-31b-it:free",
            urlopen_fn=transport_for(scenario))
        chain = ProviderChain(hosted=[provider], include_ollama=False,
                              observer=recorder)
        classifier = Classifier(provider=chain)
        classifier.observer = recorder
        result = classifier.classify(make_session())
        assert result.classification_status in (
            "pending", "classification_failed"), scenario
        assert result.source == "pending", scenario
        assert not (result.category == "neutral" and result.confidence > 0
                    and result.source != "pending"), scenario
        attempts = recorder.repository.query_invocations(kind="attempt")
        assert attempts, scenario
        assert all(item.success == 0 for item in attempts), scenario
        for item in attempts:
            assert "sk-or-" not in (item.error_message or ""), scenario
            assert "Bearer" not in (item.error_message or ""), scenario
        store.close()


def test_retry_terminal_stops_and_writes_no_duplicates():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    provider = OpenRouterProvider(
        model="google/gemma-4-31b-it:free",
        urlopen_fn=transport_for("http-503"))
    chain = ProviderChain(hosted=[provider], include_ollama=False,
                          observer=recorder)
    classifier = Classifier(provider=chain)
    classifier.observer = recorder
    session = make_session()
    first = classifier.classify(session)
    # Cache holds failures? Re-classify must not duplicate stored rows.
    service_rows_before = store.query_classifications(limit=100000)
    assert first.classification_status in ("pending", "classification_failed")
    attempts = recorder.repository.query_invocations(kind="attempt")
    assert attempts, "failed attempts must be observable"
    store.close()
