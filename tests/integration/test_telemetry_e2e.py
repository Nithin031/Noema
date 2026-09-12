"""Required real-world scenario with telemetry (§66).

Collect activity → meaningful session → classify (telemetry recorded) →
distraction pattern → realtime evaluation (staged timings) → fast
verification (telemetry recorded) → intervention → productive return →
outcome → metrics inspection → restart (metrics intact) → benchmark
again → compare. All providers are fakes; no network.
"""

from datetime import datetime, timedelta, timezone


from noema.api import NoemaService
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classifier
from noema.observability.benchmark import BenchmarkRunner, compare_runs
from noema.cli.benchmark import mock_providers
from noema.observability.recorder import TelemetryRecorder
from noema.application.realtime import FastModelVerifier

from test_realtime_e2e import ScriptedAW, drive
from test_realtime_pipeline import (
    ConfirmLeg,
    CountingClient,
    distracting_payload,
    productive_payload,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def wired_service(store, payload):
    service = NoemaService(
        ActivityWatchAdapter(ScriptedAW("Firefox", "YouTube", NOW - timedelta(minutes=25), 25)),
        store,
        classifier=Classifier(client=CountingClient(payload=payload)),
    )
    recorder = TelemetryRecorder(store)
    service.telemetry = recorder
    service.classifier.observer = recorder
    service.fast_verifier = FastModelVerifier(
        fast_openrouter=ConfirmLeg(), observer=recorder)
    return service, recorder


def test_full_scenario_is_fully_observable(tmp_path):
    path = str(tmp_path / "scenario.sqlite3")
    store = SQLiteStore(path)
    service, recorder = wired_service(store, distracting_payload())

    # 1-6: activity → session → classification with telemetry.
    result = drive(service, NOW)
    assert result["state"] == "INTERVENTION_COOLDOWN"
    normal = recorder.repository.query_invocations(purpose="NORMAL_CLASSIFICATION")
    assert normal, "classification calls must be recorded"
    assert all(item.pipeline == "SEMANTIC_CLASSIFICATION" for item in normal)
    attempts = [item for item in normal if item.kind == "attempt"]
    assert attempts, "at least one classification attempt recorded"

    # 7-10: realtime evaluation stages + verification + intervention.
    evals = recorder.repository.query_operations(kind="realtime_eval")
    assert len(evals) == 1
    stages = evals[0]["metadata"]
    assert stages["tracker_state"] == "INTERVENTION_COOLDOWN"
    assert stages["detection_ids"], "eval links its detection rows"
    assert stages["feature_ms"] is not None and stages["feature_ms"] >= 0
    verify = recorder.repository.query_invocations(purpose="FAST_DISTRACTION")
    assert any(item.kind == "attempt" and item.success for item in verify)
    assert all(item.pipeline == "REALTIME_DETECTION" for item in verify)
    interventions = store.query_interventions(limit=100000)
    assert len(interventions) == 1

    # 11-12: productive return → measured recovery.
    service.adapter = ActivityWatchAdapter(
        ScriptedAW("VS Code", "reward.py", NOW, 10, tag="recovery"))
    service.classifier = Classifier(
        client=CountingClient(payload=productive_payload()))
    service.classifier.observer = recorder
    service.ingest_telemetry(start=NOW, end=NOW + timedelta(minutes=10))
    service.sessionize_stored_events(
        start=NOW, end=NOW + timedelta(minutes=10), limit=100000)
    service.build_meaningful_sessions(
        start=NOW, end=NOW + timedelta(minutes=10), limit=100000,
        sequence_terminated=False)
    fresh = [item for item in service.store.query_meaningful_sessions(limit=100000)
             if item.start_time >= NOW]
    assert fresh
    service.classify_meaningful_sessions(fresh, max_retries=5)
    outcome = service.measure_outcome(
        interventions[0].id, now=NOW + timedelta(minutes=30))
    assert outcome.recovery_status.value == "RECOVERED"

    # 13: metrics inspection answers the headline questions.
    bundle = service.metrics_bundle(days=3650, daemon=None)
    assert bundle["recording"] is True
    assert bundle["models"], "model table must list the test provider"
    funnel = bundle["realtime"]
    assert funnel["evaluations"] >= 1 and funnel["confirmed"] >= 1
    assert funnel["interventions_triggered"] >= 1
    assert funnel["recovery_rate"] == 1.0
    assert funnel["by_intervention_type"], "recovery split by type must exist"
    tokens = bundle["tokens"]["efficiency"]
    assert tokens["classified_sessions"] >= 1
    assert tokens["classification_tokens_total"] >= 0
    assert bundle["quota"]["configured"] is False  # no chain attached here
    db_ops = recorder.repository.query_operations(kind="db")
    assert {item["name"] for item in db_ops} >= {
        "event_ingest", "session_rebuild", "meaningful_generation",
        "classification_write", "intervention_write", "outcome_write"}

    # 14-15: restart keeps every metric row; domain rows intact too.
    before_invocations = recorder.repository.count_tables()["model_invocations"]
    store.close()
    reopened = SQLiteStore(path)
    resurrected = TelemetryRecorder(reopened).repository
    assert resurrected.count_tables()["model_invocations"] == before_invocations
    assert reopened.query_interventions(limit=100000)
    reopened.close()

    # 16-17: benchmark again on the same store, then compare revisions.
    store = SQLiteStore(path)
    runner = BenchmarkRunner(repository=TelemetryRecorder(store).repository)
    first = runner.run(mock_providers()[:1], iterations=2, note="before")
    second = runner.run(mock_providers()[:1], iterations=2, note="after")
    comparison = compare_runs(first, second)
    assert comparison["rows"], "delta must cover fixture rows"
    assert all("latency_p95" in row for row in comparison["rows"])
    stored = TelemetryRecorder(store).repository.get_benchmark_run(first["run_id"])
    assert stored is not None and len(stored["results"]) == len(first["results"])
    store.close()
