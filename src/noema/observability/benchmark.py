"""Controlled model comparison benchmarks.

A benchmark runs a FIXED deterministic fixture set (synthetic evidence,
never real user history) through one provider at a time with the same
prompt template, measuring latency, tokens, success, and schema
validity. Results persist as benchmark runs so revisions compare
before/after. Benchmarks never change production models, thresholds,
prompts, or provider priority, and never route through the production
chain (per-model isolation, with quota gating when a chain is supplied).
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence

from noema.observability.models import BenchmarkResult, BenchmarkRun, Purpose
from noema.observability.percentile import deltas, summarize_latencies
from noema.observability.provider_telemetry import classify_error, take_usage

BENCHMARK_TEST_SET = "noema-bench-v1"

PROMPT_TEMPLATE = (
    "Classify this desktop activity session. Return ONLY a JSON object with "
    "keys category (productive|distractive|neutral), productivity "
    "(productive|distracting|neutral), activity (short description), signal "
    "(why, citing evidence), confidence (0.0-1.0).\nEVIDENCE:\n{payload}"
)

REQUIRED_SCHEMA_KEYS = ("category", "productivity", "activity", "signal")
ALLOWED_CATEGORIES = {"productive", "distractive", "neutral"}
ALLOWED_PRODUCTIVITY = {"productive", "distracting", "neutral"}


def _evidence(**fields: Any) -> Dict[str, Any]:
    base = {
        "session_type": "activity",
        "device": "laptop",
        "duration_seconds": 300,
        "event_count": 42,
        "source": "benchmark-fixture",
    }
    base.update(fields)
    return base


FIXTURES: List[Dict[str, Any]] = [
    {"name": "simple_productive_coding",
     "evidence": _evidence(app="Code.exe", title="reward.py - workspace",
                           domain=None, url=None)},
    {"name": "research",
     "evidence": _evidence(app="Firefox", title="Attention Is All You Need - arXiv",
                           domain="arxiv.org", url="https://arxiv.org/abs/1706.03762")},
    {"name": "documentation",
     "evidence": _evidence(app="Firefox", title="sqlite3 — DB-API 2.0 interface",
                           domain="docs.python.org", url="https://docs.python.org/3/library/sqlite3.html")},
    {"name": "meeting",
     "evidence": _evidence(app="Teams.exe", title="Weekly sync - Contoso",
                           domain=None, url=None, duration_seconds=1800)},
    {"name": "social_distraction",
     "evidence": _evidence(app="Firefox", title="Instagram feed",
                           domain="instagram.com", url="https://instagram.com/")},
    {"name": "entertainment",
     "evidence": _evidence(app="Firefox", title="Funny cat compilation",
                           domain="youtube.com", url="https://youtube.com/watch?v=x")},
    {"name": "ambiguous_activity",
     "evidence": _evidence(app="Firefox", title="New tab", domain=None, url=None,
                           duration_seconds=20, event_count=3)},
    {"name": "mixed_activity",
     "evidence": _evidence(app="Code.exe", title="reward.py - workspace",
                           domain="github.com", url="https://github.com/",
                           observed_applications=["Code.exe", "Firefox", "Slack"],
                           observed_titles=["reward.py", "Instagram feed", "general"])},
    {"name": "long_session",
     "evidence": _evidence(app="Code.exe", title="refactor.py - workspace",
                           domain=None, url=None, duration_seconds=7200,
                           event_count=1800)},
    {"name": "high_context_batch",
     "evidence": _evidence(app="Firefox", title="Research notes " + "x" * 4000,
                           domain="example.com", url="https://example.com/notes",
                           duration_seconds=3600, event_count=900,
                           observed_titles=["note {}".format(index) for index in range(60)])},
    {"name": "malformed_edge_case",
     "evidence": {}},
]


def build_prompt(evidence: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(evidence), ensure_ascii=False, sort_keys=True, default=str)
    return PROMPT_TEMPLATE.format(payload=payload)


def validate_schema(payload: Any) -> bool:
    """Schema validity: required keys with allowed categorical values."""
    if not isinstance(payload, Mapping):
        return False
    for key in REQUIRED_SCHEMA_KEYS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return False
    if str(payload.get("category", "")).strip().lower() not in ALLOWED_CATEGORIES:
        return False
    if str(payload.get("productivity", "")).strip().lower() not in ALLOWED_PRODUCTIVITY:
        return False
    confidence = payload.get("confidence", 0.5)
    try:
        number = float(confidence)
    except (TypeError, ValueError):
        return False
    return 0.0 <= number <= 1.0


def benchmark_environment(profile: str = "") -> Dict[str, Any]:
    """Environment labels for comparability. No sensitive machine data."""
    revision = "unknown"
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip() or "unknown"
    except Exception:
        revision = "unknown"
    return {
        "os": "{} {}".format(platform.system(), platform.release()),
        "python": platform.python_version(),
        "app_revision": revision,
        "cpu_count": os.cpu_count(),
        "profile": str(profile or ""),
    }


class BenchmarkRunner:
    """Run fixtures against providers with per-model isolation."""

    def __init__(self, repository: Any = None, chain: Any = None):
        self.repository = repository
        # Optional chain: used ONLY for quota gating (skip models that may
        # not be called), never for routing benchmark traffic.
        self.chain = chain

    def _quota_ok(self, provider: Any, prompt: str) -> bool:
        chain = self.chain
        if chain is None:
            return True
        if getattr(provider, "name", "") == "ollama":
            return True
        usable = getattr(chain, "_model_usable", None)
        if not callable(usable):
            return True
        try:
            return int(usable(provider, prompt)) >= 0
        except Exception:
            return False

    def run_model(self, provider: Any, fixture: Mapping[str, Any],
                  iterations: int = 5) -> Dict[str, Any]:
        """Run one fixture against one provider, aggregated honestly."""
        name = str(getattr(provider, "name", "?") or "?")
        model = getattr(provider, "model", None)
        prompt = build_prompt(fixture["evidence"])
        classify = getattr(provider, "classify", None)
        latencies: List[float] = []
        out_tokens: List[float] = []
        tps_values: List[float] = []
        success = 0
        schema_valid = 0
        failures = 0
        last_error: Optional[str] = None
        session = SimpleNamespace(id="benchmark-{}".format(fixture["name"]),
                                  app="benchmark", title="benchmark",
                                  domain=None, url=None)
        for _ in range(max(1, int(iterations))):
            if not callable(classify):
                failures += 1
                last_error = "ProviderError: provider has no classify method"
                continue
            if not self._quota_ok(provider, prompt):
                failures += 1
                last_error = "QuotaDenied: model not currently callable under quota"
                continue
            started = time.perf_counter()
            try:
                payload = classify(session, prompt)
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                call_error = None
            except Exception as exc:  # noqa: BLE001 - measured, then counted
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                latencies.append(latency_ms)
                failures += 1
                _, _, message = classify_error(exc)
                last_error = message
                continue
            latencies.append(latency_ms)
            usage = take_usage(provider)
            valid = validate_schema(payload)
            if valid:
                success += 1
                schema_valid += 1
            else:
                failures += 1
                last_error = "Malformed: response failed schema validation"
            out = usage.output_tokens
            if out is None and isinstance(payload, Mapping):
                out = max(1, len(json.dumps(payload, ensure_ascii=False, default=str)) // 4)
                exact = False
            else:
                exact = bool(usage.exact and out is not None)
            if out is not None and latency_ms > 0:
                out_tokens.append(float(out))
                tps_values.append(round(out / (latency_ms / 1000.0), 2))
            _ = exact
        latency = summarize_latencies(latencies)
        return {
            "provider": name,
            "model": str(model) if model is not None else None,
            "fixture": str(fixture["name"]),
            "iterations": max(1, int(iterations)),
            "success_count": success,
            "schema_valid_count": schema_valid,
            "failure_count": failures,
            "mean_latency_ms": latency["mean"],
            "p50_latency_ms": latency["p50"],
            "p95_latency_ms": latency["p95"],
            "mean_out_tokens": (sum(out_tokens) / len(out_tokens)) if out_tokens else None,
            "mean_tps": (sum(tps_values) / len(tps_values)) if tps_values else None,
            "last_error": last_error,
        }

    def run(self, providers: Sequence[Any],
            fixtures: Optional[Sequence[Mapping[str, Any]]] = None,
            iterations: int = 5,
            purpose: str = Purpose.NORMAL_CLASSIFICATION,
            test_set: str = BENCHMARK_TEST_SET,
            note: str = "",
            profile: str = "") -> Dict[str, Any]:
        """Run the full matrix and persist the run when a repository exists."""
        fixtures = list(fixtures) if fixtures is not None else list(FIXTURES)
        run_id = uuid.uuid4().hex[:16]
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        environment = benchmark_environment(profile=profile)
        started = time.perf_counter()
        results: List[Dict[str, Any]] = []
        for provider in providers:
            for fixture in fixtures:
                results.append(self.run_model(provider, fixture, iterations=iterations))
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        run = BenchmarkRun(
            run_id=run_id, timestamp=timestamp,
            environment_json=json.dumps(environment, ensure_ascii=False, sort_keys=True),
            test_set=test_set, purpose=Purpose.normalize(purpose),
            iterations=int(iterations), duration_ms=duration_ms,
            status="completed", note=str(note or "")[:500],
        )
        if self.repository is not None:
            try:
                self.repository.insert_benchmark_run(run)
                for item in results:
                    self.repository.insert_benchmark_result(BenchmarkResult(
                        run_id=run_id, provider=item["provider"], model=item["model"],
                        fixture=item["fixture"], iterations=item["iterations"],
                        success_count=item["success_count"],
                        schema_valid_count=item["schema_valid_count"],
                        failure_count=item["failure_count"],
                        mean_latency_ms=item["mean_latency_ms"],
                        p50_latency_ms=item["p50_latency_ms"],
                        p95_latency_ms=item["p95_latency_ms"],
                        mean_out_tokens=item["mean_out_tokens"],
                        mean_tps=item["mean_tps"],
                    ))
            except Exception:
                pass  # A persistence failure must not invalidate the run.
        payload = run.to_dict()
        payload["results"] = results
        return payload


def compare_runs(before: Mapping[str, Any], after: Mapping[str, Any]) -> Dict[str, Any]:
    """Before/after deltas keyed by (provider, model, fixture)."""
    def _index(run: Mapping[str, Any]) -> Dict[Any, Mapping[str, Any]]:
        return {(item.get("provider"), item.get("model"), item.get("fixture")): item
                for item in (run.get("results") or [])}

    older, newer = _index(before), _index(after)
    rows = []
    for key in sorted(set(older) | set(newer), key=str):
        old, new = older.get(key, {}), newer.get(key, {})
        rows.append({
            "provider": key[0],
            "model": key[1],
            "fixture": key[2],
            "latency_p95": deltas(old.get("p95_latency_ms"), new.get("p95_latency_ms")),
            "latency_mean": deltas(old.get("mean_latency_ms"), new.get("mean_latency_ms")),
            "tokens_out": deltas(old.get("mean_out_tokens"), new.get("mean_out_tokens")),
            "success": deltas(old.get("success_count"), new.get("success_count")),
            "present_before": key in older,
            "present_after": key in newer,
        })
    return {
        "before": {"run_id": before.get("run_id"), "timestamp": before.get("timestamp")},
        "after": {"run_id": after.get("run_id"), "timestamp": after.get("timestamp")},
        "rows": rows,
    }


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    widths = [len(str(head)) for head in headers]
    text_rows = [[("" if value is None else str(value)) for value in row] for row in rows]
    for row in text_rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    lines = ["  ".join(str(head).ljust(widths[index]) for index, head in enumerate(headers)).rstrip()]
    for row in text_rows:
        lines.append("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip())
    return "\n".join(lines)


def _ms(value: Any) -> str:
    if value is None:
        return "n<5"
    try:
        return "{:.0f}ms".format(float(value))
    except (TypeError, ValueError):
        return "?"


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return "{:+.1%}".format(float(value))
    except (TypeError, ValueError):
        return "?"


def format_report(run: Mapping[str, Any]) -> str:
    """Readable text tables for one benchmark run."""
    results = run.get("results") or []
    by_model: Dict[Any, List[Mapping[str, Any]]] = {}
    for item in results:
        by_model.setdefault((item.get("provider"), item.get("model")), []).append(item)
    model_rows = []
    for (provider, model), items in sorted(by_model.items(), key=str):
        latencies = [item["mean_latency_ms"] for item in items if item.get("mean_latency_ms") is not None]
        p95s = [item["p95_latency_ms"] for item in items if item.get("p95_latency_ms") is not None]
        outs = [item["mean_out_tokens"] for item in items if item.get("mean_out_tokens") is not None]
        tps = [item["mean_tps"] for item in items if item.get("mean_tps") is not None]
        succ = sum(item.get("success_count", 0) for item in items)
        total = sum(item.get("iterations", 0) for item in items)
        mean_lat = sum(latencies) / len(latencies) if latencies else None
        p95_lat = max(p95s) if p95s else None
        model_rows.append([
            "{} {}".format(provider, model or "?"),
            total,
            _ms(mean_lat),
            _ms(p95_lat) if p95_lat is not None else "n<5",
            "{:.0f}".format(sum(outs) / len(outs)) if outs else "—",
            "{:.1f}".format(sum(tps) / len(tps)) if tps else "—",
            "{:.1%}".format(succ / total) if total else "—",
        ])
    lines = [
        "NOEMA MODEL BENCHMARK",
        "run {} · {} · {} iterations · {}".format(
            run.get("run_id"), run.get("timestamp"), run.get("iterations"), run.get("test_set")),
        "",
        _table(["MODEL", "N", "MEAN", "P95", "OUT TOK", "TOK/S", "SUCCESS"], model_rows),
        "",
        "Per-fixture detail:",
        _table(
            ["FIXTURE", "MODEL", "N", "OK", "SCHEMA", "FAIL", "MEAN", "P95"],
            [[item.get("fixture"), "{} {}".format(item.get("provider"), item.get("model") or "?"),
              item.get("iterations"), item.get("success_count"), item.get("schema_valid_count"),
              item.get("failure_count"), _ms(item.get("mean_latency_ms")),
              _ms(item.get("p95_latency_ms")) if item.get("p95_latency_ms") is not None else "n<5"]
             for item in results],
        ),
        "",
        "Notes: P95 needs n>=5 per fixture (shown as n<5 otherwise). "
        "TOK/S is output tokens over total latency (approximation). "
        "No prompts, responses, or secrets stored.",
    ]
    return "\n".join(lines)


def format_delta(comparison: Mapping[str, Any]) -> str:
    """Readable before/after delta table."""
    rows = []
    for item in comparison.get("rows") or []:
        rows.append([
            "{} {}".format(item.get("provider"), item.get("model") or "?"),
            str(item.get("fixture")),
            _pct(item.get("latency_p95", {}).get("relative")),
            _pct(item.get("tokens_out", {}).get("relative")),
            str(item.get("success", {}).get("before")) + "→" + str(item.get("success", {}).get("after")),
        ])
    return "\n".join([
        "BENCHMARK DELTA: {} → {}".format(
            (comparison.get("before") or {}).get("run_id"),
            (comparison.get("after") or {}).get("run_id")),
        _table(["MODEL", "FIXTURE", "ΔP95", "ΔTOK", "SUCCESS"], rows),
    ])
