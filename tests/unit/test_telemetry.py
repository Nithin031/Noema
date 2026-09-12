"""Unit tests: percentiles, records, recorder, repository, error taxonomy."""

from noema.infrastructure.database import SQLiteStore
from noema.observability.models import (
    InvocationRecord,
    OperationRecord,
    Purpose,
    Pipeline,
    TokenBasis,
    invocation_id,
    utcnow_iso,
)
from noema.observability.percentile import (
    deltas,
    percentile,
    rate,
    summarize_latencies,
)
from noema.observability.provider_telemetry import (
    assemble_invocation,
    classify_error,
    cost_for_model,
    quota_scope_for,
    take_usage,
)
from noema.observability.recorder import TelemetryRecorder
from noema.observability.repository import TelemetryRepository


def test_percentile_uses_linear_interpolation():
    assert percentile([], 50) is None
    assert percentile([7.0], 50) == 7.0
    assert percentile([0.0, 10.0], 50) == 5.0
    assert percentile([0.0, 10.0, 20.0, 30.0], 50) == 15.0
    # rank = 0.95 * 3 = 2.85 -> 20 + 10 * 0.85
    assert abs(percentile([0.0, 10.0, 20.0, 30.0], 95) - 28.5) < 1e-9
    assert percentile([0.0, 10.0, 20.0, 30.0], 0) == 0.0
    assert percentile([0.0, 10.0, 20.0, 30.0], 100) == 30.0


def test_summarize_latencies_reports_sample_honesty():
    empty = summarize_latencies([])
    assert empty["n"] == 0 and empty["p95"] is None and empty["insufficient_samples"] is True
    tiny = summarize_latencies([100.0, 200.0])
    assert tiny["n"] == 2 and tiny["p50"] == 150.0
    assert tiny["p95"] is None and tiny["p99"] is None and tiny["insufficient_samples"] is True
    full = summarize_latencies([10.0, 20.0, 30.0, 40.0, 50.0])
    assert full["n"] == 5 and full["p50"] == 30.0
    assert full["p95"] is not None and full["p99"] is not None
    assert full["insufficient_samples"] is False
    assert full["min"] == 10.0 and full["max"] == 50.0 and full["mean"] == 30.0
    messy = summarize_latencies([10.0, "junk", None, float("nan"), float("inf")])
    assert messy["n"] == 1 and messy["p50"] == 10.0


def test_rate_and_deltas():
    assert rate(1, 2) == 0.5
    assert rate(0, 0) is None
    assert rate("junk", 4) is None
    assert deltas(100.0, 80.0) == {"before": 100.0, "after": 80.0,
                                   "absolute": -20.0, "relative": -0.2}
    assert deltas(None, 5.0)["relative"] is None


def test_purpose_and_pipeline_normalize_unknowns():
    assert Purpose.normalize("fast_distraction") == "FAST_DISTRACTION"
    assert Purpose.normalize("nonsense") == "OTHER"
    assert Pipeline.normalize(None) == "OTHER"
    assert Pipeline.normalize("realtime_detection") == "REALTIME_DETECTION"


def test_invocation_id_is_stable_per_attempt():
    first = invocation_id("req", "gemini", "m", 0, 123)
    assert first == invocation_id("req", "gemini", "m", 0, 123)
    assert first != invocation_id("req", "gemini", "m", 1, 123)
    assert first != invocation_id("other", "gemini", "m", 0, 123)


def test_classify_error_taxonomy():
    assert classify_error(TimeoutError("timed out"))[0] == "TIMEOUT"
    assert classify_error(Exception("HTTP 429 slow down"))[0] == "RATE_LIMITED"
    assert classify_error(Exception("HTTP 503 overloaded"))[0] == "SERVER_ERROR"
    assert classify_error(Exception("HTTP 502 bad gateway"))[0] == "SERVER_ERROR"
    assert classify_error(Exception("401 unauthorized key"))[0] == "AUTH"
    assert classify_error(ValueError("invalid json"))[0] == "MALFORMED"
    assert classify_error(Exception("request exceeds token limit"))[0] == "CONTEXT_OVERFLOW"
    assert classify_error(Exception("quota exhausted"))[0] == "QUOTA_DENIED"
    assert classify_error(Exception("weird"))[0] == "PROVIDER_ERROR"
    kind, code, message = classify_error(Exception("x" * 9999))
    assert kind == "PROVIDER_ERROR" and len(message) <= 320


def test_cost_never_invented():
    cost, basis = cost_for_model("google/gemma-4-31b-it:free")
    assert (cost, basis) == (0.0, "FREE_TIER")
    cost, basis = cost_for_model("gemini-3.5-flash")
    assert cost is None and basis == "UNKNOWN"
    cost, basis = cost_for_model(None)
    assert cost is None and basis == "UNKNOWN"


def test_quota_scope_vocabulary():
    assert quota_scope_for("openrouter") == "shared-openrouter-guards"
    assert quota_scope_for("ollama") == "local"
    assert quota_scope_for("gemini") == "model-specific"
    assert quota_scope_for("mystery") is None


def test_take_usage_reads_stash_without_clearing():
    class Fake:
        _last_usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                       "exact": True, "finish_reason": "stop",
                       "provider_request_id": "req-1"}

    usage = take_usage(Fake())
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (10, 5, 15)
    assert usage.exact is True and usage.finish_reason == "stop"
    assert usage.provider_request_id == "req-1"
    assert take_usage(object()).exact is False
    assert take_usage(object()).input_tokens is None


def test_assemble_marks_estimates_and_unknowns_honestly():
    exact = assemble_invocation(
        request_id="r", timestamp_iso=utcnow_iso(), timestamp_ms=1,
        provider_name="openrouter", model="m:free", purpose="NORMAL_CLASSIFICATION",
        pipeline="SEMANTIC_CLASSIFICATION", operation="SINGLE_CLASSIFY",
        usage=take_usage(type("P", (), {"_last_usage": {
            "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
            "exact": True}})()),
        error=None, success_payload=True, latency_ms=500.0)
    assert exact.token_basis == "EXACT"
    assert (exact.input_tokens, exact.output_tokens, exact.total_tokens) == (100, 20, 120)
    assert exact.tps == round(20 / 0.5, 2) and exact.tps_basis == "approx_total_latency"
    assert exact.cost == 0.0 and exact.cost_basis == "FREE_TIER"

    estimated = assemble_invocation(
        request_id="r", timestamp_iso=utcnow_iso(), timestamp_ms=1,
        provider_name="ollama", model="llama3.2:3b", purpose="NORMAL_CLASSIFICATION",
        pipeline="SEMANTIC_CLASSIFICATION", operation="SINGLE_CLASSIFY",
        estimated_input_tokens=800, error=None, success_payload=True, latency_ms=2000.0)
    assert estimated.token_basis == "ESTIMATED"
    assert estimated.input_tokens == 800 and estimated.output_tokens is None
    assert estimated.tps is None and estimated.cost is None and estimated.cost_basis == "UNKNOWN"

    unknown = assemble_invocation(
        request_id="r", timestamp_iso=utcnow_iso(), timestamp_ms=1,
        provider_name="gemini", model="gemini-3.5-flash", purpose="OTHER",
        pipeline="OTHER", operation="X",
        error=None, success_payload=True, latency_ms=100.0)
    assert unknown.token_basis == "UNKNOWN"
    assert unknown.input_tokens is None

    failed = assemble_invocation(
        request_id="r", timestamp_iso=utcnow_iso(), timestamp_ms=1,
        provider_name="gemini", model="gemini-3.5-flash", purpose="OTHER",
        pipeline="OTHER", operation="X",
        error=TimeoutError("timed out"), success_payload=False, latency_ms=100.0)
    assert failed.success == 0 and failed.error_type == "TIMEOUT"

    malformed = assemble_invocation(
        request_id="r", timestamp_iso=utcnow_iso(), timestamp_ms=1,
        provider_name="gemini", model="gemini-3.5-flash", purpose="OTHER",
        pipeline="OTHER", operation="X",
        error=None, success_payload=False, latency_ms=100.0)
    assert malformed.success == 0 and malformed.error_type == "MALFORMED"


def test_generation_time_basis_when_provider_reports_it():
    record = assemble_invocation(
        request_id="r", timestamp_iso=utcnow_iso(), timestamp_ms=1,
        provider_name="ollama", model="llama3.2:3b", purpose="OTHER",
        pipeline="OTHER", operation="X",
        usage=take_usage(type("P", (), {"_last_usage": {
            "input_tokens": 50, "output_tokens": 100, "total_tokens": 150,
            "exact": True, "generation_ms": 2000.0, "total_duration_ms": 2500.0}})()),
        error=None, success_payload=True, latency_ms=2500.0)
    assert record.generation_ms == 2000.0
    assert record.tps == 50.0 and record.tps_basis == "generation_time"


def test_recorder_observe_model_call_round_trip():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)

    class FakeProvider:
        name = "openrouter"
        _last_usage = {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50, "exact": True}

    with recorder.observe_model_call(
            FakeProvider(), "x:free", "NORMAL_CLASSIFICATION",
            "SEMANTIC_CLASSIFICATION", "SINGLE_CLASSIFY", "req-1") as handle:
        from noema.observability.provider_telemetry import take_usage as take

        handle.usage = take(FakeProvider())
        handle.success_payload = True
        handle.session_ids = ["s1"]
    rows = recorder.repository.query_invocations()
    assert len(rows) == 1
    row = rows[0]
    assert row.provider == "openrouter" and row.success == 1
    assert row.token_basis == "EXACT" and row.total_tokens == 50
    assert row.to_dict()["session_ids"] == ["s1"]
    store.close()


def test_recorder_records_failures_and_never_raises():
    recorder = TelemetryRecorder(None)
    assert recorder.record_invocation(InvocationRecord(
        invocation_id="x", request_id="y", timestamp=utcnow_iso(), provider="p")) is False
    assert recorder.record_operation("db", "op") is False

    class Boom:
        def insert_invocation(self, record):
            raise RuntimeError("db is gone")

    class FakeRepo:
        def insert_invocation(self, record):
            return Boom().insert_invocation(record)

        def insert_operation(self, *args, **kwargs):
            raise RuntimeError("db is gone")

    recorder = TelemetryRecorder(repository=FakeRepo())
    assert recorder.record_invocation(InvocationRecord(
        invocation_id="x", request_id="y", timestamp=utcnow_iso(), provider="p")) is False
    assert recorder.record_operation("db", "op") is False

    store = SQLiteStore()
    recorder = TelemetryRecorder(store)

    class ExplodingProvider:
        name = "gemini"

    try:
        with recorder.observe_model_call(
                ExplodingProvider(), "m", "OTHER", "OTHER", "X", "req-2") as handle:
            handle.success_payload = False
            raise RuntimeError("provider blew up")
    except RuntimeError:
        pass
    rows = recorder.repository.query_invocations()
    assert len(rows) == 1
    assert rows[0].success == 0
    store.close()


def test_repository_filters_and_idempotent_insert():
    store = SQLiteStore()
    repository = TelemetryRepository(store)
    record = InvocationRecord(
        invocation_id="a" * 32, request_id="req", timestamp="2026-09-10T00:00:00Z",
        provider="gemini", model="m", purpose="NORMAL_CLASSIFICATION",
        pipeline="SEMANTIC_CLASSIFICATION", operation="SINGLE_CLASSIFY", kind="attempt",
        success=1, input_tokens=10, token_basis=TokenBasis.EXACT)
    assert repository.insert_invocation(record) is True
    assert repository.insert_invocation(record) is False  # idempotent
    assert len(repository.query_invocations(provider="gemini")) == 1
    assert repository.query_invocations(provider="ollama") == []
    assert len(repository.query_invocations(kind="attempt", success=True)) == 1
    assert repository.query_invocations(since="2026-09-11T00:00:00Z") == []
    assert repository.count_tables()["model_invocations"] == 1
    operation = OperationRecord(kind="worker_run", name="ingest", duration_ms=12.5)
    assert isinstance(repository.insert_operation(operation), int)
    assert len(repository.query_operations(kind="worker_run", name="ingest")) == 1
    store.close()


def test_invocation_record_round_trip():
    record = InvocationRecord(
        invocation_id="b" * 32, request_id="req", timestamp=utcnow_iso(),
        provider="p", model="m", batch_budget_tokens=100,
        batch_input_tokens=72, batch_utilization=0.72,
        detection_id="d1", intervention_id="i1")
    clone = InvocationRecord.from_row(record.to_dict())
    assert clone.batch_utilization == 0.72
    assert clone.detection_id == "d1" and clone.intervention_id == "i1"
    assert clone.to_dict()["provider"] == "p"
