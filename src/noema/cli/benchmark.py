"""``benchmark`` and ``metrics`` command handlers (daemon CLI dispatches here).

Benchmarks run fixed synthetic fixtures through one provider at a time
with per-model isolation. They never mutate production routing, models,
thresholds, or quotas beyond the calls they explicitly make, and every
run is persisted with its environment for before/after comparison.
``--mock`` runs the same matrix against deterministic scripted providers
for CI and offline use.
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, List, Optional, Sequence

from noema.observability.benchmark import (
    BENCHMARK_TEST_SET,
    FIXTURES,
    BenchmarkRunner,
    compare_runs,
    format_delta,
    format_report,
)
from noema.observability.models import Purpose
from noema.observability.recorder import TelemetryRecorder


class MockBenchmarkProvider:
    """Deterministic scripted provider for offline benchmarks and tests."""

    def __init__(self, name: str = "mock", model: str = "mock-model",
                 latency_ms: float = 120.0, out_tokens: int = 180,
                 fail: bool = False, malformed: bool = False):
        self.name = name
        self.model = model
        self.latency_ms = latency_ms
        self.out_tokens = out_tokens
        self.fail = fail
        self.malformed = malformed
        self.calls = 0
        self._last_usage = None

    def classify(self, session: Any, prompt: str) -> Mapping[str, Any]:
        self.calls += 1
        time.sleep(max(0.0, self.latency_ms / 1000.0))
        self._last_usage = {
            "input_tokens": max(1, len(prompt) // 4),
            "output_tokens": self.out_tokens,
            "total_tokens": max(1, len(prompt) // 4) + self.out_tokens,
            "exact": True,
        }
        if self.fail:
            from noema.infrastructure.ollama import OllamaError

            raise OllamaError("mock provider failure")
        if self.malformed:
            return {"unexpected": "shape"}
        return {
            "category": "productive",
            "productivity": "productive",
            "activity": "Mock benchmark activity",
            "signal": "Deterministic mock evidence.",
            "confidence": 0.8,
        }

    def complete_json(self, prompt: str, max_tokens: int = 300) -> Mapping[str, Any]:
        return self.classify(None, prompt)


def mock_providers() -> List[MockBenchmarkProvider]:
    """Small deterministic provider set exercising success/failure paths."""
    return [
        MockBenchmarkProvider("mock", "mock-fast", latency_ms=80.0, out_tokens=120),
        MockBenchmarkProvider("mock", "mock-slow", latency_ms=250.0, out_tokens=220),
        MockBenchmarkProvider("mock", "mock-flaky", latency_ms=100.0, fail=True),
    ]


def build_benchmark_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark", description="Run Noema model benchmarks")
    parser.add_argument("target", nargs="?", default="full",
                        choices=["providers", "classification", "realtime", "pipeline", "full"],
                        help="providers: quick sweep; classification: full fixture matrix; "
                             "realtime: fast-verification prompts; pipeline: both; full: both + delta vs previous")
    parser.add_argument("--config", default=None, help="JSON daemon config file")
    parser.add_argument("--db", default=None, help="database path (defaults to configured db)")
    parser.add_argument("--mock", action="store_true", help="use deterministic mock providers (no quota spend)")
    parser.add_argument("--models", default=None,
                        help="comma-separated provider:model allowlist, e.g. ollama:*,gemini:gemini-3.5-flash")
    parser.add_argument("--iterations", type=int, default=5, help="iterations per model per fixture")
    parser.add_argument("--note", default="", help="note stored with the run")
    parser.add_argument("--compare", default=None, help="run_id to compare against (default: previous run)")
    parser.add_argument("--json", action="store_true", help="emit the run payload as JSON")
    return parser


def build_metrics_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="metrics", description="Noema metrics utilities")
    sub = parser.add_subparsers(dest="command", required=True)
    prune = sub.add_parser("prune", help="delete observability rows older than retention")
    prune.add_argument("--db", default=None, help="database path (defaults to configured db)")
    prune.add_argument("--config", default=None, help="JSON daemon config file")
    prune.add_argument("--days", type=float, default=None,
                       help="retention days (defaults to configured metrics_retention_days)")
    summary = sub.add_parser("summary", help="print a metrics summary table")
    summary.add_argument("--db", default=None, help="database path (defaults to configured db)")
    summary.add_argument("--config", default=None, help="JSON daemon config file")
    summary.add_argument("--days", type=int, default=7, help="window in days")
    return parser


def _load_config(config_path: Optional[str]) -> Any:
    from noema.config.settings import DaemonConfig, load_dotenv

    load_dotenv()
    config = DaemonConfig.from_environment()
    if config_path:
        config = DaemonConfig.from_file(config_path, base=config)
    return config


def _store_for(config: Any, db_override: Optional[str]) -> Any:
    from noema.infrastructure.database import SQLiteStore

    return SQLiteStore(db_override or config.db_path)


def _match_models(patterns: Optional[str], provider: Any) -> bool:
    if not patterns:
        return True
    name = str(getattr(provider, "name", "") or "")
    model = str(getattr(provider, "model", "") or "")
    for pattern in patterns.split(","):
        pattern = pattern.strip()
        if not pattern or ":" not in pattern:
            continue
        want_provider, want_model = pattern.split(":", 1)
        want_provider, want_model = want_provider.strip(), want_model.strip()
        if want_provider not in ("", "*", name):
            continue
        if want_model in ("", "*", model):
            return True
    return False


def _real_providers(config: Any, purpose_fast: bool = False) -> List[Any]:
    """Build real single providers for per-model isolated benchmarking."""
    import os

    providers: List[Any] = []
    if config.openrouter_enabled and os.environ.get(config.openrouter_api_key_env, "").strip():
        from noema.infrastructure.providers import OpenRouterProvider

        for model in config.openrouter_free_models:
            providers.append(OpenRouterProvider(
                model=model, api_key_env=config.openrouter_api_key_env,
                base_url=config.openrouter_base_url,
                timeout=config.fast_model_timeout_seconds if purpose_fast else config.openrouter_timeout_seconds,
                title=config.openrouter_title))
    if os.environ.get(config.gemini_api_key_env, "").strip():
        from noema.infrastructure.providers import GeminiProvider

        for model in config.gemini_models:
            providers.append(GeminiProvider(
                model=model, api_key_env=config.gemini_api_key_env,
                base_url=config.gemini_base_url, timeout=config.gemini_timeout_seconds))
    if config.ollama_enabled:
        from noema.infrastructure.providers import OllamaProvider
        from noema.infrastructure.ollama import OllamaClient

        providers.append(OllamaProvider(OllamaClient(
            base_url=config.ollama_base_url, model=config.ollama_model,
            timeout=config.ollama_timeout_seconds)))
    return providers


def _quota_chain(config: Any) -> Any:
    """A chain used ONLY for quota gating benchmark calls, never routing."""
    try:
        from noema.cli.daemon import _classifier

        classifier = _classifier(config)
        provider = getattr(classifier, "provider", None)
        from noema.infrastructure.providers import ProviderChain

        return provider if isinstance(provider, ProviderChain) else None
    except Exception:
        return None


def run_benchmark_command(argv: Sequence[str]) -> int:
    args = build_benchmark_parser().parse_args(list(argv))
    config = _load_config(args.config)
    store = _store_for(config, args.db)
    recorder = TelemetryRecorder(store)
    try:
        iterations = max(1, int(args.iterations or 5))
        if args.mock:
            providers: List[Any] = mock_providers()
            chain = None
        else:
            purpose_fast = args.target == "realtime"
            providers = [item for item in _real_providers(config, purpose_fast)
                         if _match_models(args.models, item)]
            if not providers:
                print("No benchmarkable providers: --mock, set API keys, or enable Ollama.")
                return 2
            chain = _quota_chain(config)
        runner = BenchmarkRunner(repository=recorder.repository, chain=chain)
        if args.target == "providers":
            fixtures = [item for item in FIXTURES if item["name"] in
                        ("simple_productive_coding", "social_distraction", "ambiguous_activity")]
            purpose = Purpose.NORMAL_CLASSIFICATION
        elif args.target == "realtime":
            fixtures = [item for item in FIXTURES if item["name"] in
                        ("social_distraction", "entertainment", "ambiguous_activity",
                         "mixed_activity", "malformed_edge_case")]
            purpose = Purpose.FAST_DISTRACTION
        else:
            fixtures = list(FIXTURES)
            purpose = (Purpose.FAST_DISTRACTION if args.target == "realtime"
                       else Purpose.NORMAL_CLASSIFICATION)
        run = runner.run(providers, fixtures=fixtures, iterations=iterations,
                         purpose=purpose, note=args.note,
                         profile="cli:{}".format(args.target))
        if args.json:
            print(json.dumps(run, ensure_ascii=False, indent=1, default=str))
        else:
            print(format_report(run))
        if args.target == "full":
            try:
                runs = recorder.repository.list_benchmark_runs(limit=5)
                previous = next((item for item in runs if item["run_id"] != run["run_id"]), None)
                compare_id = args.compare
                if compare_id:
                    previous = recorder.repository.get_benchmark_run(compare_id)
                if previous and previous.get("results"):
                    print("")
                    print(format_delta(compare_runs(previous, run)))
                else:
                    print("\nNo previous benchmark run to compare against.")
            except Exception as exc:
                print("\nDelta unavailable: {}".format(exc))
        return 0
    finally:
        try:
            store.close()
        except Exception:
            pass


def run_metrics_command(argv: Sequence[str]) -> int:
    args = build_metrics_parser().parse_args(list(argv))
    config = _load_config(args.config)
    store = _store_for(config, args.db)
    recorder = TelemetryRecorder(store)
    try:
        if args.command == "prune":
            days = args.days if args.days is not None else config.metrics_retention_days
            removed = recorder.repository.prune(days)
            print("Pruned observability rows older than {} days: {}".format(days, removed))
            print("Product tables untouched.")
            return 0
        if args.command == "summary":
            from noema.observability.metrics import (
                summarize_api,
                summarize_model_table,
                summarize_token_windows,
            )

            days = max(1, int(args.days or 7))
            invocations = recorder.repository.query_invocations(limit=100000)
            recent = [item for item in invocations if item.timestamp[:10] >= _days_ago(days)]
            print("METRICS SUMMARY (last {}d, {} invocations)".format(days, len(recent)))
            for row in summarize_model_table(recent):
                lat = row["latency"]
                print("- {} {} [{}]: n={} ok={} p50={} p95={} tok_in(exact)={} fail={}".format(
                    row["provider"], row["model"], row["purpose"], row["requests"],
                    row["successful_requests"],
                    _fmt(lat.get("p50")), _fmt(lat.get("p95")),
                    row["tokens_exact_in"], row["failed_requests"]))
            tokens = summarize_token_windows(recent)
            for label in ("today", "7d", "30d"):
                bucket = tokens["windows"][label]
                print("{}: in(exact)={} in(est)={} out(exact)={} unknown_calls={}".format(
                    label, bucket["input"]["exact"], bucket["input"]["estimated"],
                    bucket["output"]["exact"], bucket["input"]["unknown_calls"]))
            api_rows = summarize_api(recorder.repository.query_operations(kind="api_request", limit=100000))
            for row in api_rows[:15]:
                print("api {} x{} p50={} p95={}".format(
                    row["route"], row["requests"], _fmt(row["latency"].get("p50")),
                    _fmt(row["latency"].get("p95"))))
            return 0
        return 2
    finally:
        try:
            store.close()
        except Exception:
            pass


def _days_ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc).date() - timedelta(days=max(0, days - 1))).isoformat()


def _fmt(value: Any) -> str:
    if value is None:
        return "n<5"
    try:
        return "{:.0f}ms".format(float(value))
    except (TypeError, ValueError):
        return "?"


def main_benchmark(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point honoring the daemon CLI architecture (see daemon/cli)."""
    return run_benchmark_command(list(argv or []))


def main_metrics(argv: Optional[Sequence[str]] = None) -> int:
    return run_metrics_command(list(argv or []))
