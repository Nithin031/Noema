"""Integration: chain/classifier/verifier/service emit honest telemetry.

Fake providers control usage stashes, errors, and latencies. Every test
asserts both the product outcome (unchanged) and the telemetry recorded.
"""

import time

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.domain.activity import ActivityEvent
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classifier
from noema.infrastructure.providers import ProviderChain, ProviderError
from noema.observability.models import Purpose, Pipeline, TokenBasis
from noema.observability.recorder import TelemetryRecorder
from noema.application.realtime import FastModelVerifier


def valid_payload(category="productive", productivity="productive"):
    return {
        "category": category,
        "productivity": productivity,
        "activity": "Observed activity",
        "signal": "Observed evidence supports this result.",
        "confidence": 0.8,
    }


class FakeModel:
    """Scripted provider with controllable usage stash and failures."""

    def __init__(self, name, model, payload=None, error=None, usage=None,
                 tokens=100, latency=0.0, batch_error=None):
        self.name = name
        self.model = model
        self.payload = payload if payload is not None else valid_payload()
        self.error = error
        self.usage = usage
        self.tokens = tokens
        self.latency = latency
        self.batch_error = batch_error
        self.calls = 0
        self.batch_calls = 0
        self._last_usage = None

    def classify(self, session, prompt):
        self.calls += 1
        if self.latency:
            time.sleep(self.latency)
        if self.error:
            raise self.error
        self._last_usage = dict(self.usage) if self.usage else None
        return dict(self.payload)

    def classify_batch(self, session_ids, evidences):
        self.batch_calls += 1
        if self.latency:
            time.sleep(self.latency)
        if self.batch_error:
            raise self.batch_error
        self._last_usage = dict(self.usage) if self.usage else None
        return [dict(self.payload, session_id=session_id) for session_id in session_ids]

    def count_tokens(self, prompt):
        return self.tokens

    def complete_json(self, prompt, max_tokens=300):
        return self.classify(None, prompt)


def usage(input_tokens, output_tokens):
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens, "exact": True}


def make_chain(recorder, hosted, ollama=None):
    chain = ProviderChain(hosted=hosted, ollama=ollama, observer=recorder)
    return chain


def invocations(recorder, **filters):
    return recorder.repository.query_invocations(**filters)


def test_single_success_records_exact_tokens_and_request():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    chain = make_chain(recorder, [FakeModel("openrouter", "google/gemma-4-31b-it:free",
                                            usage=usage(1000, 50), tokens=900)])
    result = chain.classify(object(), "prompt")
    assert result["category"] == "productive"

    attempts = invocations(recorder, kind="attempt")
    requests = invocations(recorder, kind="request")
    assert len(attempts) == 1 and len(requests) == 1
    attempt = attempts[0]
    assert attempt.provider == "openrouter"
    assert attempt.token_basis == TokenBasis.EXACT
    assert (attempt.input_tokens, attempt.output_tokens, attempt.total_tokens) == (1000, 50, 1050)
    assert attempt.success == 1 and attempt.fallback_depth == 0
    assert attempt.purpose == Purpose.NORMAL_CLASSIFICATION
    assert attempt.pipeline == Pipeline.SEMANTIC_CLASSIFICATION
    assert attempt.operation == "SINGLE_CLASSIFY"
    assert attempt.quota_scope == "shared-openrouter-guards"
    assert attempt.cost == 0.0 and attempt.cost_basis == "FREE_TIER"
    assert attempt.latency_ms is not None and attempt.latency_ms >= 0
    assert requests[0].success == 1
    assert requests[0].request_id == attempt.request_id
    store.close()


def test_fallback_records_depth_reason_and_overhead():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    first = FakeModel("openrouter", "google/gemma-4-31b-it:free",
                      error=ProviderError("OpenRouter temporary failure (HTTP 503) x"),
                      tokens=900)
    second = FakeModel("gemini", "gemini-3.5-flash", usage=usage(800, 40), tokens=700)
    chain = make_chain(recorder, [first, second])
    result = chain.classify(object(), "prompt")
    assert result["category"] == "productive"

    attempts = sorted(invocations(recorder, kind="attempt"),
                      key=lambda item: item.fallback_depth)
    assert len(attempts) == 2
    assert attempts[0].success == 0 and attempts[0].error_type == "SERVER_ERROR"
    assert attempts[0].fallback_depth == 0
    # Failed attempt keeps the prompt estimate (minus output reserve).
    assert attempts[0].token_basis == TokenBasis.ESTIMATED
    assert attempts[1].success == 1 and attempts[1].fallback_depth == 1
    assert attempts[1].fallback_from == "openrouter"
    assert "503" in (attempts[1].fallback_reason or "")
    assert attempts[1].token_basis == TokenBasis.EXACT
    logical = invocations(recorder, kind="request")
    assert len(logical) == 1 and logical[0].success == 1
    assert logical[0].fallback_depth == 1
    store.close()


def test_all_failed_stays_pending_and_records_every_attempt():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    chain = make_chain(recorder, [FakeModel("gemini", "gemini-3.5-flash",
                                            error=ProviderError("down"), tokens=700)],
                       ollama=None)
    chain.include_ollama = False
    try:
        chain.classify(object(), "prompt")
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass
    attempts = invocations(recorder, kind="attempt")
    logical = invocations(recorder, kind="request")
    assert len(attempts) == 1 and attempts[0].success == 0
    assert len(logical) == 1 and logical[0].success == 0
    store.close()


def test_quota_denial_skips_call_and_records_skip():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    first = FakeModel("gemini", "gemini-3.5-flash", usage=usage(100, 10), tokens=100)
    chain = make_chain(recorder, [first])
    chain.include_ollama = False  # isolate the denial: no local tail
    for _ in range(20):
        chain.ledger.consume("llm", "gemini-3.5-flash", 700)
    try:
        chain.classify(object(), "prompt")
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass
    assert first.calls == 0  # never called: denial happens BEFORE the call
    assert invocations(recorder, kind="attempt") == []
    skips = recorder.repository.query_operations(kind="quota_skip")
    assert len(skips) == 1
    assert skips[0]["name"] == "gemini-3.5-flash"
    store.close()


def test_batch_records_utilization_sessions_and_tokens():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    model = FakeModel("gemini", "gemini-3.5-flash", usage=usage(4000, 200), tokens=3900)
    chain = make_chain(recorder, [model])

    class Session:
        def __init__(self, sid):
            self.id = sid

    entries = [{"session": Session("s{}".format(index)),
                "evidence": {"blob": "x" * 200},
                "prompt": "p"} for index in range(4)]
    results = chain.classify_batch(entries)
    assert len(results) == 4

    attempts = invocations(recorder, kind="attempt", operation="BATCH_GROUP")
    logical = invocations(recorder, kind="request", operation="BATCH_GROUP")
    assert len(attempts) == 1 and len(logical) == 1
    attempt = attempts[0]
    assert attempt.batch_size == 4
    assert attempt.token_basis == TokenBasis.EXACT
    assert attempt.total_tokens == 4200
    budget = chain._batch_budget("gemini-3.5-flash")
    assert attempt.batch_budget_tokens == budget
    assert attempt.batch_input_tokens == 3900
    assert attempt.batch_utilization == round(3900 / budget, 4)
    assert logical[0].success == 1
    assert set(logical[0].to_dict()["session_ids"]) == {"s0", "s1", "s2", "s3"}
    packs = recorder.repository.query_operations(kind="batch_pack")
    assert len(packs) == 1
    assert packs[0]["metadata"]["sessions"] == 4
    store.close()


def test_classifier_retry_attempts_recorded_once_each():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)

    class Flaky:
        name = "ollama"
        model = "llama3.2:3b"

        def __init__(self):
            self.calls = 0
            self._last_usage = None

        def classify(self, session, prompt):
            self.calls += 1
            if self.calls < 3:
                raise ProviderError("flaky transport")
            self._last_usage = {"input_tokens": 60, "output_tokens": 12,
                                "total_tokens": 72, "exact": True}
            return valid_payload()

        def count_tokens(self, prompt):
            return 60

    import noema.application.classification as classifier_module

    original_sleep = classifier_module.time.sleep
    classifier_module.time.sleep = lambda seconds: None
    try:
        classifier = Classifier(provider=Flaky())
        classifier.observer = recorder
        from noema.domain.sessions import Sessionizer
        from noema.domain.activity import ActivityEvent

        event = ActivityEvent(
            timestamp="2026-09-10T10:00:00Z", duration=60, device="laptop",
            app="Code.exe", title="reward.py", bucket_id="window",
            source_event_id="retry-1")
        session = Sessionizer().sessionize([event])[0]
        result = classifier.classify(session)
        assert result.classification_status == "classified"
    finally:
        classifier_module.time.sleep = original_sleep

    attempts = sorted(invocations(recorder, kind="attempt"),
                      key=lambda item: item.attempt_number)
    assert [item.attempt_number for item in attempts] == [0, 1, 2]
    assert all(item.success == (item.attempt_number == 2) for item in attempts)
    assert attempts[2].token_basis == TokenBasis.EXACT
    logical = invocations(recorder, kind="request")
    assert len(logical) == 1 and logical[0].success == 1
    store.close()


def test_verifier_records_verify_attempts_with_purpose():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    leg = FakeModel("openrouter", "fast-model", payload={
        "concerning": True, "severity": 3, "confidence": 0.8,
        "reason": "sustained drift", "recommended_intervention": "reframe",
    }, usage=usage(300, 60), tokens=250)

    class FakeChain:
        observer = recorder
        rate_limits = {}
        ledger = None
        include_ollama = False
        ollama = None

        def _model_usable(self, provider, prompt):
            return 500

    verifier = FastModelVerifier(chain=FakeChain(), fast_openrouter=leg)
    result = verifier.verify({"window_minutes": 60, "distraction_ratio": 0.5},
                             0.8, ["distraction_ratio=0.90"], presence_state="active")
    assert result.concerning is True
    attempts = invocations(recorder, purpose="FAST_DISTRACTION")
    assert len(attempts) == 2  # attempt + logical request
    attempt = next(item for item in attempts if item.kind == "attempt")
    assert attempt.pipeline == "REALTIME_DETECTION"
    assert attempt.operation == "VERIFY"
    assert attempt.total_tokens == 360
    store.close()


def test_service_stages_record_db_write_timings():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    service = NoemaService(ActivityWatchAdapter(), store)
    service.telemetry = recorder
    service.ingest_events([
        ActivityEvent(
            timestamp="2026-09-10T10:00:00Z", duration=60, device="laptop",
            app="Firefox", title="Docs", domain="docs.python.org",
            bucket_id="window", source_event_id="telemetry-1"),
    ])
    sessions = service.sessionize_stored_events()
    assert sessions
    ops = {item["name"]: item for item in recorder.repository.query_operations(kind="db")}
    assert "event_ingest" in ops and ops["event_ingest"]["rows_affected"] == 1
    assert "session_rebuild" in ops and ops["session_rebuild"]["rows_affected"] == len(sessions)
    assert all(item["duration_ms"] is not None for item in ops.values())
    store.close()


def test_meme_generation_records_model_call():
    from noema.domain.behavior import BehaviorObservation, BehaviorState
    from noema.domain.sessions import Sessionizer

    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    service = NoemaService(ActivityWatchAdapter(), store)
    service.telemetry = recorder
    event = ActivityEvent(
        timestamp="2026-09-10T10:00:00Z", duration=600, device="laptop",
        app="Firefox", title="Feed", domain="example.com",
        bucket_id="window", source_event_id="meme-1")
    session = Sessionizer().sessionize([event])[0]
    store.insert_sessions([session])
    store.insert_behavior_observations([BehaviorObservation(
        session_id=session.id, state=BehaviorState.DISTRACTED,
        started_at=session.start, confidence=0.9, distraction_score=0.9,
        actionable=True, reason="test")])
    service.generate_meme(session.id)
    memes = invocations(recorder, purpose="MEME_GENERATION")
    assert len(memes) == 1
    assert memes[0].pipeline == "MEME" and memes[0].operation == "GENERATE_MEME"
    assert memes[0].to_dict()["session_ids"] == [session.id]
    store.close()


def test_embedding_records_exact_or_estimated_tokens():
    import json as _json

    from noema.infrastructure.embeddings import embed_texts

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return _json.dumps(self.payload).encode("utf-8")

    def opener(payload):
        def fake_opener(request, timeout):
            return FakeResponse(payload)
        return fake_opener

    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    vectors = embed_texts(
        ["alpha"], urlopen_fn=opener({"embeddings": [[0.1]], "prompt_eval_count": 7}),
        recorder=recorder)
    assert vectors == [[0.1]]
    rows = invocations(recorder, purpose="EMBEDDING")
    assert len(rows) == 1
    assert rows[0].token_basis == TokenBasis.EXACT and rows[0].input_tokens == 7

    vectors = embed_texts(["beta beta"], urlopen_fn=opener({"embeddings": [[0.2]]}),
                          recorder=recorder)
    assert vectors == [[0.2]]
    rows = invocations(recorder, purpose="EMBEDDING")
    assert len(rows) == 2
    assert rows[1].token_basis == TokenBasis.ESTIMATED
    assert rows[1].input_tokens and rows[1].input_tokens > 0

    def failing_opener(request, timeout):
        raise OSError("connection refused")

    try:
        embed_texts(["gamma"], urlopen_fn=failing_opener, recorder=recorder)
        raise AssertionError("expected OllamaError")
    except Exception as exc:
        assert "embedding request failed" in str(exc)
    rows = invocations(recorder, purpose="EMBEDDING")
    assert len(rows) == 3 and rows[2].success == 0
    store.close()


def test_metrics_prune_command_keeps_product_data(tmp_path):
    from datetime import datetime, timedelta, timezone

    from noema.application.classification import Classification
    from noema.cli.benchmark import run_metrics_command
    from noema.observability.models import InvocationRecord, utcnow_iso

    db = str(tmp_path / "prune.sqlite3")
    store = SQLiteStore(db)
    recorder = TelemetryRecorder(store)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat().replace("+00:00", "Z")
    recorder.record_invocation(InvocationRecord(
        invocation_id="q" * 32, request_id="r", timestamp=old,
        provider="gemini", model="m"))
    recorder.record_invocation(InvocationRecord(
        invocation_id="w" * 32, request_id="r", timestamp=utcnow_iso(),
        provider="gemini", model="m"))
    store.insert_classification(Classification(
        session_id="keep", category="neutral", activity_type="browsing",
        productivity="neutral", confidence=0.1, provider="ollama",
        model="llama3.2:3b"))
    store.close()

    assert run_metrics_command(["prune", "--db", db, "--days", "30"]) == 0
    reopened = SQLiteStore(db)
    assert TelemetryRecorder(reopened).repository.count_tables()["model_invocations"] == 1
    assert reopened.query_classifications(session_id="keep", limit=1)
    reopened.close()
