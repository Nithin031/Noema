"""Read-side aggregations over invocation records and existing sources.

Every builder here is a pure function over rows plus small adapters over
structures the product already maintains (quota ledger, worker health,
detections, outcomes). No new measurement systems: one canonical source
per metric. Token sums use attempt records only (logical request records
carry no tokens, so nothing double-counts). Rates use logical request
records where they exist, attempts otherwise — each builder documents
its denominator.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from noema.observability.models import InvocationRecord, TokenBasis
from noema.observability.percentile import deltas, rate, summarize_latencies


def _day_floor(days_ago: int = 0) -> str:
    day = datetime.now(timezone.utc).date() - timedelta(days=days_ago)
    return day.isoformat()


def _in_window(timestamp: str, days: int) -> bool:
    try:
        return str(timestamp)[:10] >= _day_floor(days - 1)
    except (TypeError, ValueError):
        return False


def _tokens_of(record: InvocationRecord) -> tuple:
    total = record.total_tokens
    if total is None and record.input_tokens is not None and record.output_tokens is not None:
        total = record.input_tokens + record.output_tokens
    return (record.input_tokens, record.output_tokens, total)


def summarize_model_table(records: Sequence[InvocationRecord]) -> List[Dict[str, Any]]:
    """Per (provider, model, purpose) performance table.

    Token sums cover attempt records (each one spent tokens); success and
    latency cover logical request records when present, attempts otherwise.
    """
    attempts = [item for item in records if item.kind != "request"]
    requests = [item for item in records if item.kind == "request"]
    by_key: Dict[Any, List[InvocationRecord]] = defaultdict(list)
    by_key_req: Dict[Any, List[InvocationRecord]] = defaultdict(list)
    for item in attempts:
        by_key[(item.provider, item.model, item.purpose)].append(item)
    for item in requests:
        by_key_req[(item.provider, item.model, item.purpose)].append(item)
    rows = []
    for key in sorted(by_key, key=lambda item: (str(item[0]), str(item[1]), str(item[2]))):
        group = by_key[key]
        logical = by_key_req.get(key) or group
        ok_req = sum(1 for item in logical if item.success)
        lat = summarize_latencies([item.latency_ms for item in logical if item.latency_ms is not None])
        err_types: Dict[str, int] = defaultdict(int)
        for item in group:
            if item.error_type:
                err_types[item.error_type] += 1
        in_exact = sum(item.input_tokens or 0 for item in group if item.token_basis == TokenBasis.EXACT)
        out_exact = sum(item.output_tokens or 0 for item in group if item.token_basis == TokenBasis.EXACT)
        in_est = sum(item.input_tokens or 0 for item in group if item.token_basis == TokenBasis.ESTIMATED)
        tps_values = [item.tps for item in group if item.tps is not None]
        tps_basis = None
        for item in group:
            if item.tps is not None:
                tps_basis = item.tps_basis
                if tps_basis == "generation_time":
                    break
        rows.append({
            "provider": key[0],
            "model": key[1],
            "purpose": key[2],
            "requests": len(logical),
            "attempts": len(group),
            "successful_requests": ok_req,
            "failed_requests": len(logical) - ok_req,
            "success_rate": rate(ok_req, len(logical)),
            "retries": sum(1 for item in group if item.attempt_number > 0),
            "fallbacks": sum(1 for item in group if item.fallback_depth > 0),
            "latency": lat,
            "tokens_exact_in": in_exact,
            "tokens_exact_out": out_exact,
            "tokens_estimated_in": in_est,
            "tokens_unknown_calls": sum(1 for item in group if item.token_basis == TokenBasis.UNKNOWN),
            "mean_tps": (sum(tps_values) / len(tps_values)) if tps_values else None,
            "tps_basis": tps_basis,
            "tps_samples": len(tps_values),
            "errors": dict(err_types),
            "timeout_rate": rate(err_types.get("TIMEOUT", 0), len(group)),
            "rate_limit_rate": rate(err_types.get("RATE_LIMITED", 0), len(group)),
            "server_error_rate": rate(err_types.get("SERVER_ERROR", 0), len(group)),
            "malformed_rate": rate(err_types.get("MALFORMED", 0), len(group)),
        })
    return rows


def summarize_token_windows(records: Sequence[InvocationRecord]) -> Dict[str, Any]:
    """Input/output/total token usage for today, 7d, 30d (UTC days).

    Sums cover attempt records only. Basis is reported per bucket so an
    estimate is never mistaken for a provider count.
    """
    attempts = [item for item in records if item.kind != "request"]
    windows: Dict[str, Any] = {}
    for label, days in (("today", 1), ("7d", 7), ("30d", 30)):
        bucket = {"input": {"exact": 0, "estimated": 0, "unknown_calls": 0},
                  "output": {"exact": 0, "unknown_calls": 0},
                  "total": {"exact": 0}}
        for item in attempts:
            if not _in_window(item.timestamp, days):
                continue
            if item.token_basis == TokenBasis.EXACT:
                bucket["input"]["exact"] += item.input_tokens or 0
                bucket["output"]["exact"] += item.output_tokens or 0
                _, _, total = _tokens_of(item)
                bucket["total"]["exact"] += total or 0
            elif item.token_basis == TokenBasis.ESTIMATED:
                bucket["input"]["estimated"] += item.input_tokens or 0
                bucket["output"]["unknown_calls"] += 1
            else:
                bucket["input"]["unknown_calls"] += 1
                bucket["output"]["unknown_calls"] += 1
        windows[label] = bucket
    return {"timezone": "UTC", "windows": windows}


def summarize_retry_fallback(records: Sequence[InvocationRecord]) -> Dict[str, Any]:
    """Retry vs fallback overhead, separated from productive spend."""
    attempts = [item for item in records if item.kind != "request"]
    first = [item for item in attempts if item.attempt_number == 0 and item.fallback_depth == 0]
    retries = [item for item in attempts if item.attempt_number > 0]
    fallbacks = [item for item in attempts if item.fallback_depth > 0]

    def _spend(group: List[InvocationRecord]) -> Dict[str, Any]:
        tokens = sum((item.total_tokens or item.input_tokens or 0) for item in group)
        exact = sum(1 for item in group if item.token_basis == TokenBasis.EXACT)
        latency = sum(item.latency_ms or 0.0 for item in group)
        return {"calls": len(group), "tokens": tokens,
                "tokens_exact_calls": exact, "latency_ms": round(latency, 1)}

    pairs: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "reasons": defaultdict(int)})
    for item in fallbacks:
        edge = "{}:{} -> {}:{}".format(
            item.fallback_from or "?", "?", item.provider, item.model or "?")
        pairs[edge]["count"] += 1
        pairs[edge]["reasons"][item.fallback_reason or "unknown"] += 1
    edges = [{"edge": edge, "count": info["count"],
              "reasons": dict(info["reasons"])} for edge, info in sorted(pairs.items())]
    return {
        "initial_attempts": _spend(first),
        "retries": _spend(retries),
        "fallbacks": _spend(fallbacks),
        "fallback_edges": edges,
    }


def summarize_batches(records: Sequence[InvocationRecord],
                      operations: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Batch efficiency: sessions/request, tokens/session, utilization."""
    groups = [item for item in records
              if item.kind == "request" and item.operation == "BATCH_GROUP"]
    attempts = [item for item in records
                if item.kind != "request" and item.operation == "BATCH_GROUP"]
    singles = [item for item in records
               if item.kind == "request" and item.operation == "SINGLE_CLASSIFY"]
    sessions = sum(item.batch_size for item in groups)
    tokens = sum((item.total_tokens or item.input_tokens or 0) for item in attempts)
    latencies = [item.latency_ms for item in groups if item.latency_ms is not None]
    utils = [item.batch_utilization for item in groups
             if item.batch_utilization is not None]
    single_lat = [item.latency_ms for item in singles if item.latency_ms is not None]
    single_tokens = [(item.total_tokens or item.input_tokens or 0) for item in singles]
    single_tokens = [value for value in single_tokens if value]
    splits = [op for op in operations if op.get("kind") == "batch_split"]
    packs = [op for op in operations if op.get("kind") == "batch_pack"]
    pack_utils: List[float] = []
    pack_groups = 0
    pack_sheds = 0
    for op in packs:
        try:
            import json as _json
            metadata = op.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = _json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
        pack_groups += int(metadata.get("groups", 0) or 0)
        pack_sheds += int(metadata.get("tail_sheds", 0) or 0)
        for value in metadata.get("utilizations", []) or []:
            try:
                pack_utils.append(float(value))
            except (TypeError, ValueError):
                continue
    return {
        "batch_requests": len(groups),
        "batch_attempts": len(attempts),
        "sessions_per_request": (sessions / len(groups)) if groups else None,
        "tokens_per_request": (tokens / len(groups)) if groups else None,
        "tokens_per_session": (tokens / sessions) if sessions else None,
        "latency_per_request": summarize_latencies(latencies),
        "latency_per_session_ms": (
            (sum(latencies) / sessions) if latencies and sessions else None),
        "single_requests": len(singles),
        "single_latency": summarize_latencies(single_lat),
        "single_tokens_per_session": (
            (sum(single_tokens) / len(single_tokens)) if single_tokens else None),
        "utilization": summarize_latencies(utils) if utils else {
            "n": 0, "min": None, "max": None, "mean": None,
            "p50": None, "p95": None, "p99": None, "insufficient_samples": True},
        "pack_events": pack_groups,
        "tail_sheds": pack_sheds,
        "split_events": len(splits),
    }


def summarize_realtime(invocation_records: Sequence[InvocationRecord],
                       operations: Sequence[Mapping[str, Any]],
                       detections: Sequence[Mapping[str, Any]],
                       outcomes: Sequence[Mapping[str, Any]],
                       interventions: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Realtime funnel + latencies from detections, outcomes, interventions.

    Recovery and continuation come from the outcome table (behavioral
    outcome statistics), never from invented labels. Rates without ground
    truth are reported as funnel conversion, not accuracy.
    """
    evals = [op for op in operations if op.get("kind") == "realtime_eval"]
    eval_lat = [op.get("duration_ms") for op in evals if op.get("duration_ms") is not None]
    decisions = [item.get("decision") for item in detections]
    verified = [item for item in detections if item.get("decision") in
                ("verified_concerning", "verified_not_concerning")]
    concerning = [item for item in detections if item.get("decision") == "verified_concerning"]
    triggered = [item for item in detections if item.get("decision") == "intervention_triggered"]
    verify_calls = [item for item in invocation_records
                    if item.kind != "request" and item.purpose == "FAST_DISTRACTION"]
    verify_lat = [item.latency_ms for item in verify_calls if item.latency_ms is not None]
    verify_tokens = [(item.total_tokens or item.input_tokens or 0) for item in verify_calls]
    outcomes_by_intervention = {}
    for item in outcomes:
        key = item.get("intervention_id")
        if key:
            outcomes_by_intervention[key] = item.get("recovery_status")
    recovered = sum(1 for status in outcomes_by_intervention.values() if status == "RECOVERED")
    continued = sum(1 for status in outcomes_by_intervention.values() if status == "NOT_RECOVERED")
    unknown = sum(1 for status in outcomes_by_intervention.values()
                  if status not in ("RECOVERED", "NOT_RECOVERED"))
    by_type: Dict[str, Dict[str, int]] = {}
    for item in interventions:
        mode = item.get("mode") or "UNKNOWN"
        entry = by_type.setdefault(mode, {"interventions": 0, "recovered": 0})
        entry["interventions"] += 1
        if outcomes_by_intervention.get(item.get("id")) == "RECOVERED":
            entry["recovered"] += 1
    for mode, entry in by_type.items():
        entry["recovery_rate"] = rate(entry["recovered"], entry["interventions"])
    detect_to_intervene = None
    return {
        "evaluations": len(evals),
        "evaluation_latency": summarize_latencies(eval_lat),
        "candidates": sum(1 for decision in decisions if decision in
                          ("candidate_detected", "verified_concerning",
                           "verified_not_concerning", "intervention_triggered")),
        "verifications": len(verify_calls),
        "verification_latency": summarize_latencies(verify_lat),
        "verification_tokens_total": sum(verify_tokens),
        "verification_tokens_mean": (sum(verify_tokens) / len(verify_tokens)) if verify_tokens else None,
        "verification_success_rate": rate(
            sum(1 for item in verify_calls if item.success), len(verify_calls)),
        "confirmed": len(concerning),
        "confirmation_rate": rate(len(concerning), len(verified)),
        "interventions_triggered": len(triggered),
        "intervention_rate": rate(len(triggered), len(concerning)),
        "recovered": recovered,
        "continued_distraction": continued,
        "unknown_outcome": unknown,
        "recovery_rate": rate(recovered, recovered + continued),
        "by_intervention_type": by_type,
        "detection_to_intervention_ms": detect_to_intervene,
    }


def summarize_workers(worker_health: Mapping[str, Any],
                      operations: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Worker timings: existing health plus measured run durations."""
    workers = {}
    runs_by_worker: Dict[str, List[float]] = defaultdict(list)
    for op in operations:
        if op.get("kind") != "worker_run" or op.get("duration_ms") is None:
            continue
        try:
            runs_by_worker[str(op.get("name"))].append(float(op["duration_ms"]))
        except (TypeError, ValueError):
            continue
    for name, info in dict(worker_health or {}).items():
        durations = runs_by_worker.get(str(name), [])
        workers[str(name)] = {
            "status": info.get("status"),
            "run_count": info.get("run_count", 0),
            "error_count": info.get("error_count", 0),
            "last_started_at": info.get("last_started_at"),
            "last_success_at": info.get("last_success_at"),
            "last_error": info.get("last_error"),
            "measured_runs": len(durations),
            "duration": summarize_latencies(durations),
        }
    return workers


def summarize_api(operations: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Per-route request counts and latency (no payloads ever stored)."""
    by_route: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for op in operations:
        if op.get("kind") != "api_request":
            continue
        by_route[str(op.get("name"))].append(op)
    rows = []
    for route in sorted(by_route):
        group = by_route[route]
        ok = sum(1 for item in group if item.get("success"))
        lat = summarize_latencies([item.get("duration_ms") for item in group
                                   if item.get("duration_ms") is not None])
        rows.append({
            "route": route,
            "requests": len(group),
            "success": ok,
            "errors": len(group) - ok,
            "error_rate": rate(len(group) - ok, len(group)),
            "latency": lat,
        })
    return rows


def summarize_quota(chain: Any, operations: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Quota posture from the existing ledger; no second ledger is built."""
    if chain is None:
        return {"configured": False, "models": [], "note": "no provider chain attached"}
    try:
        usage = dict(chain.usage() or {})
    except Exception:
        usage = {}
    states = getattr(chain, "rate_limits", None) or {}
    denied = sum(1 for op in operations if op.get("kind") == "quota_skip")
    models = []
    for name, state in sorted(states.items(), key=lambda item: str(item[0])):
        try:
            used_day = int(getattr(state, "requests_today", 0) or 0)
            rpd = int(getattr(state, "rpd_limit", 0) or 0)
            tokens_min = int(getattr(state, "tokens_this_minute", 0) or 0)
            tpm = int(getattr(state, "tpm_limit", 0) or 0)
            cooling = float(getattr(state, "cooldown_until", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        models.append({
            "model": str(name),
            "requests_today": used_day,
            "requests_limit": rpd,
            "request_utilization": rate(used_day, rpd),
            "tokens_this_minute": tokens_min,
            "tokens_limit": tpm,
            "cooling_down": cooling > 0,
            "consecutive_failures": int(getattr(state, "consecutive_failures", 0) or 0),
            "last_error": getattr(state, "last_error", None),
        })
    return {
        "configured": True,
        "day": getattr(getattr(chain, "ledger", None), "day", None),
        "models": models,
        "quota_denied_calls": denied,
        "ledger_entries": len(usage),
    }


def summarize_cost(records: Sequence[InvocationRecord]) -> Dict[str, Any]:
    """Cost posture without inventing prices."""
    free_tokens = 0
    unknown_calls = 0
    estimated_calls = 0
    actual_total = 0.0
    actual_calls = 0
    for item in records:
        if item.kind == "request":
            continue
        if item.cost_basis == "FREE_TIER":
            free_tokens += item.total_tokens or item.input_tokens or 0
        elif item.cost_basis == "ACTUAL" and item.cost is not None:
            actual_total += item.cost
            actual_calls += 1
        elif item.cost_basis == "ESTIMATED":
            estimated_calls += 1
        else:
            unknown_calls += 1
    return {
        "free_tier_tokens": free_tokens,
        "actual_cost_total": round(actual_total, 6) if actual_calls else None,
        "actual_cost_calls": actual_calls,
        "estimated_cost_calls": estimated_calls,
        "unknown_cost_calls": unknown_calls,
        "note": "Free-tier usage carries 0 provider charge with measured tokens; "
                "unknown pricing is NULL, never $0.",
    }


def summarize_runtime(store: Any, daemon: Any = None) -> Dict[str, Any]:
    """Cheap process/database stats. No CPU/RAM sampling (no psutil
    dependency, no 1s sampler): those stay honestly unavailable."""
    import threading
    from pathlib import Path

    db_size_bytes = None
    try:
        path = str(getattr(store, "path", "") or "")
        if path and path != ":memory:":
            size = Path(path).expanduser().stat().st_size
            db_size_bytes = int(size)
    except (OSError, TypeError, ValueError):
        db_size_bytes = None
    uptime_seconds = None
    if daemon is not None:
        try:
            uptime_seconds = (daemon.health_dict() or {}).get("uptime_seconds")
        except Exception:
            uptime_seconds = None
    try:
        thread_count = threading.active_count()
    except Exception:
        thread_count = None
    return {
        "db_size_bytes": db_size_bytes,
        "thread_count": thread_count,
        "uptime_seconds": uptime_seconds,
        "cpu_percent": None,
        "ram_bytes": None,
        "note": "CPU/RAM are not sampled (no profiler dependency); "
                "worker, API, and DB-stage durations are measured instead.",
    }


def _in_days(timestamp: str, days: int) -> bool:
    try:
        return str(timestamp)[:10] >= _day_floor(max(1, int(days)) - 1)
    except (TypeError, ValueError):
        return False


def build_metrics_bundle(repository: Any, store: Any, chain: Any = None,
                         daemon: Any = None, days: int = 30) -> Dict[str, Any]:
    """Assemble every metrics view from canonical sources in one pass."""
    days = max(1, int(days or 30))
    invocations = repository.query_invocations(limit=100000)
    operations = repository.query_operations(limit=100000)
    recent = [item for item in invocations if _in_days(item.timestamp, days)]
    recent_ops = [dict(op) for op in operations if _in_days(str(op.get("timestamp") or ""), days)]
    try:
        detections = [item.to_dict() if hasattr(item, "to_dict") else dict(item)
                      for item in store.query_detections(limit=100000)]
    except Exception:
        detections = []
    try:
        outcomes = [item.to_dict() if hasattr(item, "to_dict") else dict(item)
                    for item in store.query_outcomes(limit=100000)]
    except Exception:
        outcomes = []
    try:
        raw_interventions = store.query_interventions(limit=100000)
        interventions = []
        for item in raw_interventions:
            if hasattr(item, "to_dict"):
                interventions.append(item.to_dict())
            else:
                interventions.append({
                    "id": getattr(item, "id", None),
                    "mode": str(getattr(getattr(item, "mode", None), "value",
                                        getattr(item, "mode", None)) or "UNKNOWN"),
                })
    except Exception:
        interventions = []
    try:
        memes = store.query_memes(limit=100000)
    except Exception:
        memes = []
    try:
        classifications = store.query_classifications(limit=100000)
    except Exception:
        classifications = []
    workers_health: Dict[str, Any] = {}
    if daemon is not None:
        try:
            workers_health = (daemon.health_dict() or {}).get("workers", {}) or {}
        except Exception:
            workers_health = {}
    model_table = summarize_model_table(recent)
    token_windows = summarize_token_windows(invocations)
    retry_fallback = summarize_retry_fallback(recent)
    batches = summarize_batches(recent, recent_ops)
    realtime = summarize_realtime(recent, recent_ops, detections, outcomes, interventions)
    workers = summarize_workers(workers_health, recent_ops)
    api_rows = summarize_api(recent_ops)
    quota = summarize_quota(chain, recent_ops)
    cost = summarize_cost(recent)
    efficiency = _summarize_efficiency(recent, classifications, memes, interventions)
    try:
        retention = repository.count_tables()
    except Exception:
        retention = {}
    attempts = [item for item in recent if item.kind != "request"]
    requests = [item for item in recent if item.kind == "request"]
    ok_requests = sum(1 for item in requests if item.success)
    return {
        "window_days": days,
        "recording": bool(invocations or operations),
        "runtime": summarize_runtime(store, daemon),
        "invocations": len(invocations),
        "requests": len(requests),
        "attempts": len(attempts),
        "success_rate": rate(ok_requests, len(requests)),
        "models": model_table,
        "tokens": dict(token_windows, efficiency=efficiency, batches=batches,
                       retry_fallback=retry_fallback),
        "latency": {
            "models": [{"provider": row["provider"], "model": row["model"],
                        "purpose": row["purpose"], "latency": row["latency"]}
                       for row in model_table],
            "workers": workers,
            "api": api_rows,
            "realtime": {
                "evaluation_latency": realtime["evaluation_latency"],
                "verification_latency": realtime["verification_latency"],
                "stage_means_ms": _realtime_stage_means(recent_ops),
            },
            "batch_latency_per_session_ms": batches["latency_per_session_ms"],
        },
        "realtime": realtime,
        "workers": workers,
        "api": api_rows,
        "quota": quota,
        "cost": cost,
        "retention": retention,
    }


def _summarize_efficiency(recent: Sequence[InvocationRecord], classifications: Sequence[Any],
                          memes: Sequence[Any], interventions: Sequence[Any]) -> Dict[str, Any]:
    """Tokens per successful unit of work, split by purpose."""
    normal = [item for item in recent
              if item.kind != "request" and item.purpose == "NORMAL_CLASSIFICATION"]
    normal_tokens = sum((item.total_tokens or item.input_tokens or 0) for item in normal)
    classified = sum(1 for item in classifications
                     if getattr(item, "classification_status", None) == "classified")
    fast = [item for item in recent
            if item.kind != "request" and item.purpose == "FAST_DISTRACTION"]
    fast_tokens = sum((item.total_tokens or item.input_tokens or 0) for item in fast)
    meme_calls = [item for item in recent
                  if item.kind != "request" and item.purpose == "MEME_GENERATION"]
    meme_tokens = sum((item.total_tokens or item.input_tokens or 0) for item in meme_calls)
    return {
        "tokens_per_classified_session": (normal_tokens / classified) if classified else None,
        "classification_tokens_total": normal_tokens,
        "classified_sessions": classified,
        "tokens_per_verification": (fast_tokens / len(fast)) if fast else None,
        "verification_tokens_total": fast_tokens,
        "verifications": len(fast),
        "tokens_per_meme": (meme_tokens / len(memes)) if memes else None,
        "meme_tokens_total": meme_tokens,
        "memes": len(memes),
        "interventions": len(interventions),
    }


def _realtime_stage_means(operations: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Mean feature/verification/policy stage times from eval operations."""
    buckets: Dict[str, List[float]] = {
        "presence_ms": [], "feature_ms": [], "detection_ms": [],
        "verification_ms": [], "policy_ms": [],
    }
    for op in operations:
        if op.get("kind") != "realtime_eval":
            continue
        metadata = op.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                import json as _json

                metadata = _json.loads(metadata)
            except (TypeError, ValueError):
                continue
        if not isinstance(metadata, Mapping):
            continue
        for key in buckets:
            value = metadata.get(key)
            try:
                if value is not None:
                    buckets[key].append(float(value))
            except (TypeError, ValueError):
                continue
    return {key: (sum(values) / len(values) if values else None)
            for key, values in buckets.items()}
