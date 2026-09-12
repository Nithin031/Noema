"""Benchmarks: fixtures, runner, persistence, deltas, report, CLI."""

import json
from datetime import datetime, timedelta, timezone

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification
from noema.observability.benchmark import (
    BENCHMARK_TEST_SET,
    FIXTURES,
    BenchmarkRunner,
    build_prompt,
    compare_runs,
    format_delta,
    format_report,
    validate_schema,
)
from noema.cli.benchmark import mock_providers, run_benchmark_command
from noema.observability.metrics import summarize_token_windows
from noema.observability.models import (
    InvocationRecord,
    OperationRecord,
    TokenBasis,
    utcnow_iso,
)
from noema.observability.recorder import TelemetryRecorder


def test_fixture_set_covers_required_scenarios():
    names = {item["name"] for item in FIXTURES}
    for required in ("simple_productive_coding", "research", "documentation",
                     "meeting", "social_distraction", "entertainment",
                     "ambiguous_activity", "mixed_activity", "long_session",
                     "high_context_batch", "malformed_edge_case"):
        assert required in names
    assert len(FIXTURES) == 11


def test_fixture_prompts_are_deterministic():
    first = [build_prompt(item["evidence"]) for item in FIXTURES]
    second = [build_prompt(item["evidence"]) for item in FIXTURES]
    assert first == second
    assert all("EVIDENCE" in prompt for prompt in first)
    # No real user data: non-empty fixtures carry the synthetic marker.
    assert all("benchmark-fixture" in prompt for prompt, item in zip(first, FIXTURES)
               if item["evidence"])


def test_schema_validator_accepts_and_rejects():
    assert validate_schema({
        "category": "productive", "productivity": "productive",
        "activity": "Work", "signal": "Evidence.", "confidence": 0.9}) is True
    assert validate_schema({"category": "productive"}) is False
    assert validate_schema({"category": "bogus", "productivity": "productive",
                            "activity": "a", "signal": "s"}) is False
    assert validate_schema({"category": "productive", "productivity": "productive",
                            "activity": "a", "signal": "s", "confidence": 9}) is False
    assert validate_schema("not a mapping") is False


def test_mock_runner_persists_and_reports():
    store = SQLiteStore()
    recorder = TelemetryRecorder(store)
    runner = BenchmarkRunner(repository=recorder.repository)
    run = runner.run(mock_providers(), iterations=2, note="unit test")
    assert run["test_set"] == BENCHMARK_TEST_SET
    assert run["status"] == "completed"
    assert len(run["results"]) == 3 * len(FIXTURES)
    flaky = [item for item in run["results"] if item["model"] == "mock-flaky"]
    assert flaky and all(item["failure_count"] == 2 for item in flaky)
    fast = next(item for item in run["results"]
                if item["model"] == "mock-fast" and item["fixture"] == "research")
    assert fast["success_count"] == 2 and fast["schema_valid_count"] == 2
    assert fast["mean_latency_ms"] is not None and fast["mean_latency_ms"] >= 0
    # Insufficient samples per fixture (n=2): P95 stays NULL, honestly.
    assert fast["p95_latency_ms"] is None
    stored = recorder.repository.get_benchmark_run(run["run_id"])
    assert stored is not None and len(stored["results"]) == len(run["results"])
    assert stored["environment"]["python"]
    report = format_report(run)
    assert "NOEMA MODEL BENCHMARK" in report
    assert "mock-fast" in report and "n<5" in report
    store.close()


def test_runner_with_enough_iterations_reports_p95():
    store = SQLiteStore()
    runner = BenchmarkRunner(repository=TelemetryRecorder(store).repository)
    run = runner.run(mock_providers()[:1], iterations=6,
                     fixtures=[item for item in FIXTURES[:1]])
    row = run["results"][0]
    assert row["p95_latency_ms"] is not None
    store.close()


def test_compare_runs_produces_deltas():
    before = {"run_id": "a", "timestamp": "t", "results": [
        {"provider": "mock", "model": "m", "fixture": "f",
         "p95_latency_ms": 100.0, "mean_latency_ms": 80.0,
         "mean_out_tokens": 200.0, "success_count": 5}]}
    after = {"run_id": "b", "timestamp": "t", "results": [
        {"provider": "mock", "model": "m", "fixture": "f",
         "p95_latency_ms": 80.0, "mean_latency_ms": 70.0,
         "mean_out_tokens": 180.0, "success_count": 5}]}
    comparison = compare_runs(before, after)
    row = comparison["rows"][0]
    assert row["latency_p95"]["absolute"] == -20.0
    assert row["latency_p95"]["relative"] == -0.2
    assert row["tokens_out"]["relative"] == -0.1
    text = format_delta(comparison)
    assert "BENCHMARK DELTA" in text and "-20.0%" in text


def test_benchmark_cli_mock_run_end_to_end(tmp_path):
    code = run_benchmark_command([
        "full", "--mock", "--db", str(tmp_path / "bench.sqlite3"),
        "--iterations", "2", "--note", "cli test"])
    assert code == 0
    store = SQLiteStore(str(tmp_path / "bench.sqlite3"))
    recorder = TelemetryRecorder(store)
    runs = recorder.repository.list_benchmark_runs()
    assert len(runs) == 1 and runs[0]["note"] == "cli test"
    assert runs[0]["environment"]["os"]
    store.close()


def test_daemon_cli_dispatches_benchmark_and_metrics(tmp_path):
    from noema.cli.daemon import main

    db = str(tmp_path / "dispatch.sqlite3")
    assert main(["benchmark", "providers", "--mock", "--db", db, "--iterations", "1"]) == 0
    assert main(["metrics", "summary", "--db", db, "--days", "7"]) == 0
    assert main(["metrics", "prune", "--db", db, "--days", "30"]) == 0
    store = SQLiteStore(db)
    assert TelemetryRecorder(store).repository.list_benchmark_runs()
    store.close()


def test_metrics_and_benchmark_endpoints(tmp_path):
    store = SQLiteStore(str(tmp_path / "api.sqlite3"))
    service = NoemaService(ActivityWatchAdapter(), store)
    service.telemetry = TelemetryRecorder(store)
    app = create_app(service)

    import io
    import json as _json

    def call(path, method="GET"):
        result = {}

        def start_response(status, headers):
            result["status"] = status

        environ = {"REQUEST_METHOD": method, "PATH_INFO": path.split("?", 1)[0],
                   "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
                   "CONTENT_LENGTH": "0", "wsgi.input": io.BytesIO()}
        raw = b"".join(app(environ, start_response))
        return result["status"], _json.loads(raw.decode("utf-8"))

    for path in ("/api/metrics/summary", "/api/metrics/models", "/api/metrics/providers",
                 "/api/metrics/tokens", "/api/metrics/latency", "/api/metrics/realtime",
                 "/api/metrics/workers", "/api/benchmarks"):
        status, payload = call(path)
        assert status.startswith("200"), path
    status, payload = call("/api/benchmarks/does-not-exist")
    assert status.startswith("404")
    # Empty store: honest empty shapes, never fake values. (The test's own
    # API calls are recorded as operations, but no model ran.)
    status, summary = call("/api/metrics/summary")
    assert summary["invocations"] == 0
    assert summary["models"] == [] and summary["success_rate"] is None
    assert summary["runtime"]["thread_count"] >= 1
    assert summary["runtime"]["cpu_percent"] is None
    status, runs = call("/api/benchmarks")
    assert runs["runs"] == []
    store.close()


def test_retention_prunes_only_observability(tmp_path):
    store = SQLiteStore(str(tmp_path / "retention.sqlite3"))
    repository = TelemetryRecorder(store).repository
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat().replace("+00:00", "Z")
    repository.insert_invocation(InvocationRecord(
        invocation_id="o" * 32, request_id="r", timestamp=old,
        provider="gemini", model="m"))
    repository.insert_invocation(InvocationRecord(
        invocation_id="n" * 32, request_id="r", timestamp=utcnow_iso(),
        provider="gemini", model="m"))
    repository.insert_operation(OperationRecord(timestamp=old, kind="db", name="x"))
    # Product row that must survive pruning.
    store.insert_classification(Classification(
        session_id="keep-me", category="neutral", activity_type="browsing",
        productivity="neutral", confidence=0.1, provider="ollama",
        model="llama3.2:3b"))
    removed = repository.prune(30)
    assert removed["model_invocations"] == 1
    assert removed["operation_timings"] == 1
    assert repository.count_tables()["model_invocations"] == 1
    assert store.query_classifications(session_id="keep-me", limit=1)
    store.close()


def test_metrics_survive_restart(tmp_path):
    path = str(tmp_path / "restart.sqlite3")
    store = SQLiteStore(path)
    recorder = TelemetryRecorder(store)
    recorder.record_invocation(InvocationRecord(
        invocation_id="p" * 32, request_id="r", timestamp=utcnow_iso(),
        provider="ollama", model="llama3.2:3b", success=1))
    store.close()

    reopened = SQLiteStore(path)
    rows = TelemetryRecorder(reopened).repository.query_invocations()
    assert len(rows) == 1 and rows[0].provider == "ollama"
    reopened.close()


def test_token_windows_bucket_by_day():
    today = datetime.now(timezone.utc).date().isoformat()
    old = (datetime.now(timezone.utc).date() - timedelta(days=20)).isoformat()

    def record(day, basis, total, kind="attempt"):
        return InvocationRecord(
            invocation_id="{}-{}-{}".format(day, basis, total), request_id="r",
            timestamp="{}T10:00:00Z".format(day), provider="p", model="m",
            kind=kind, success=1, input_tokens=total, total_tokens=total,
            token_basis=basis)

    rows = [record(today, TokenBasis.EXACT, 100),
            record(today, TokenBasis.ESTIMATED, 50),
            record(old, TokenBasis.EXACT, 1000),
            record(today, TokenBasis.UNKNOWN, 0),
            record(today, TokenBasis.EXACT, 7, kind="request")]  # logical: never summed
    windows = summarize_token_windows(rows)["windows"]
    assert windows["today"]["input"]["exact"] == 100
    assert windows["today"]["input"]["estimated"] == 50
    assert windows["today"]["input"]["unknown_calls"] == 1
    assert windows["today"]["total"]["exact"] == 100
    assert windows["7d"]["input"]["exact"] == 100
    assert windows["30d"]["input"]["exact"] == 1100
    assert set(windows) == {"today", "7d", "30d"}
