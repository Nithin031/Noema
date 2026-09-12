"""Canonical telemetry records for Noema observability.

Every AI/model invocation in the product produces an ``InvocationRecord``
(metadata only: provider, model, purpose, latency, token counts, outcome).
Raw prompts, raw responses, keys, and private activity text are NEVER
stored here. Unknown values stay NULL/unknown; estimates are labeled as
estimates and never presented as exact provider counts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional


class Purpose:
    """Why a model was invoked. Controlled vocabulary, not free text."""

    NORMAL_CLASSIFICATION = "NORMAL_CLASSIFICATION"
    FAST_DISTRACTION = "FAST_DISTRACTION"
    EMBEDDING = "EMBEDDING"
    MEME_GENERATION = "MEME_GENERATION"
    OTHER = "OTHER"

    ALL = {
        NORMAL_CLASSIFICATION,
        FAST_DISTRACTION,
        EMBEDDING,
        MEME_GENERATION,
        OTHER,
    }

    @staticmethod
    def normalize(value: Any) -> str:
        text = str(value or "").strip().upper()
        return text if text in Purpose.ALL else Purpose.OTHER


class Pipeline:
    """Where an invocation originated. Controlled vocabulary."""

    SEMANTIC_CLASSIFICATION = "SEMANTIC_CLASSIFICATION"
    REALTIME_DETECTION = "REALTIME_DETECTION"
    INTERVENTION = "INTERVENTION"
    EMBEDDING = "EMBEDDING"
    MEME = "MEME"
    OTHER = "OTHER"

    ALL = {
        SEMANTIC_CLASSIFICATION,
        REALTIME_DETECTION,
        INTERVENTION,
        EMBEDDING,
        MEME,
        OTHER,
    }

    @staticmethod
    def normalize(value: Any) -> str:
        text = str(value or "").strip().upper()
        return text if text in Pipeline.ALL else Pipeline.OTHER


class TokenBasis:
    """Provenance of a token count. Estimates are never exact."""

    EXACT = "EXACT"  # Reported by the provider for this call.
    ESTIMATED = "ESTIMATED"  # Length/tokenizer estimate, labeled as such.
    UNKNOWN = "UNKNOWN"  # No usable count; stored as NULL.


class CostBasis:
    UNKNOWN = "UNKNOWN"  # No pricing available; cost stays NULL.
    FREE_TIER = "FREE_TIER"  # Free-tier model: 0 provider charge, tokens still measured.
    ESTIMATED = "ESTIMATED"  # Estimated from configured pricing.
    ACTUAL = "ACTUAL"  # Reported by the provider.


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_request_id() -> str:
    import uuid

    return uuid.uuid4().hex[:16]


def invocation_id(request_id: str, provider: Any, model: Any, attempt: int,
                  timestamp_ms: int, kind: str = "attempt") -> str:
    """Deterministic-ish invocation key.

    Stable for a given logical attempt (same request, provider, model,
    attempt number, millisecond timestamp, record kind). A retried
    *recording* of the same attempt reuses the key so ``INSERT OR IGNORE``
    converges instead of duplicating; distinct attempts, kinds, or
    milliseconds always differ.
    """
    raw = "{}|{}|{}|{}|{}|{}".format(request_id, provider, model, attempt,
                                     timestamp_ms, kind)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class InvocationRecord:
    """One standardized model-invocation observation (metadata only)."""

    invocation_id: str
    request_id: str
    timestamp: str
    provider: str = "?"
    model: Optional[str] = None
    purpose: str = Purpose.OTHER
    pipeline: str = Pipeline.OTHER
    operation: str = ""
    kind: str = "attempt"  # "attempt" (one provider call) or "request" (logical).
    session_ids_json: str = "[]"
    batch_size: int = 0
    attempt_number: int = 0
    fallback_depth: int = 0
    fallback_from: Optional[str] = None
    fallback_reason: Optional[str] = None
    success: int = 0
    error_type: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    token_basis: str = TokenBasis.UNKNOWN
    latency_ms: Optional[float] = None
    ttft_ms: Optional[float] = None  # NULL: no provider exposes TTFT today.
    generation_ms: Optional[float] = None  # NULL: non-streaming transports.
    tps: Optional[float] = None
    tps_basis: Optional[str] = None  # "generation_time" | "approx_total_latency"
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    quota_scope: Optional[str] = None
    quota_before_json: str = "{}"
    quota_after_json: str = "{}"
    cost: Optional[float] = None
    cost_basis: str = CostBasis.UNKNOWN
    # Batch packing efficiency (batch operations only; NULL otherwise).
    batch_budget_tokens: Optional[int] = None
    batch_input_tokens: Optional[int] = None
    batch_utilization: Optional[float] = None
    # Request correlation across semantic levels (each level keeps its own
    # ID; these link them: verification → detection → intervention).
    detection_id: Optional[str] = None
    intervention_id: Optional[str] = None
    # Traceability: which prompt contract + classifier version produced the
    # call. NULL on rows written before versioning existed.
    prompt_version: Optional[str] = None
    classifier_version: Optional[str] = None
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "provider": self.provider,
            "model": self.model,
            "purpose": self.purpose,
            "pipeline": self.pipeline,
            "operation": self.operation,
            "kind": self.kind,
            "session_ids": json.loads(self.session_ids_json or "[]"),
            "batch_size": self.batch_size,
            "attempt_number": self.attempt_number,
            "fallback_depth": self.fallback_depth,
            "fallback_from": self.fallback_from,
            "fallback_reason": self.fallback_reason,
            "success": bool(self.success),
            "error_type": self.error_type,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "token_basis": self.token_basis,
            "latency_ms": self.latency_ms,
            "ttft_ms": self.ttft_ms,
            "generation_ms": self.generation_ms,
            "tps": self.tps,
            "tps_basis": self.tps_basis,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "quota_scope": self.quota_scope,
            "quota_before": json.loads(self.quota_before_json or "{}"),
            "quota_after": json.loads(self.quota_after_json or "{}"),
            "cost": self.cost,
            "cost_basis": self.cost_basis,
            "batch_budget_tokens": self.batch_budget_tokens,
            "batch_input_tokens": self.batch_input_tokens,
            "batch_utilization": self.batch_utilization,
            "detection_id": self.detection_id,
            "intervention_id": self.intervention_id,
            "prompt_version": self.prompt_version,
            "classifier_version": self.classifier_version,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_row(row: Mapping[str, Any]) -> "InvocationRecord":
        data = dict(row)
        record = InvocationRecord(
            invocation_id=str(data.get("invocation_id") or ""),
            request_id=str(data.get("request_id") or ""),
            timestamp=str(data.get("timestamp") or ""),
        )
        for name in (
            "provider", "model", "purpose", "pipeline", "operation", "kind",
            "fallback_from", "fallback_reason", "error_type", "error_code",
            "error_message", "token_basis", "tps_basis", "quota_scope",
            "cost_basis", "created_at", "prompt_version", "classifier_version",
        ):
            value = data.get(name)
            if value is not None:
                setattr(record, name, value)
        for name in ("batch_size", "attempt_number", "fallback_depth", "success"):
            value = data.get(name)
            if value is not None:
                try:
                    setattr(record, name, int(value))
                except (TypeError, ValueError):
                    pass
        for name in ("input_tokens", "output_tokens", "total_tokens",
                     "context_window", "max_output_tokens"):
            value = data.get(name)
            if value is None:
                setattr(record, name, None)
            else:
                try:
                    setattr(record, name, int(value))
                except (TypeError, ValueError):
                    setattr(record, name, None)
        for name in ("latency_ms", "tps", "cost", "batch_utilization"):
            value = data.get(name)
            if value is None:
                setattr(record, name, None)
            else:
                try:
                    setattr(record, name, float(value))
                except (TypeError, ValueError):
                    setattr(record, name, None)
        for name in ("batch_budget_tokens", "batch_input_tokens"):
            value = data.get(name)
            if value is None:
                setattr(record, name, None)
            else:
                try:
                    setattr(record, name, int(value))
                except (TypeError, ValueError):
                    setattr(record, name, None)
        for name in ("session_ids_json", "quota_before_json", "quota_after_json",
                       "detection_id", "intervention_id"):
            value = data.get(name)
            if value is not None:
                setattr(record, name, str(value))
        return record


@dataclass
class OperationRecord:
    """One timed non-model operation: worker run, DB stage, API request."""

    timestamp: str = field(default_factory=utcnow_iso)
    kind: str = ""  # worker_run | db | api_request | quota_skip | realtime_eval | batch_split
    name: str = ""
    duration_ms: Optional[float] = None
    success: int = 1
    error: Optional[str] = None
    rows_affected: Optional[int] = None
    metadata_json: str = "{}"

    def to_dict(self) -> Dict[str, Any]:
        try:
            metadata = json.loads(self.metadata_json or "{}")
        except ValueError:
            metadata = {}
        return {
            "timestamp": self.timestamp,
            "kind": self.kind,
            "name": self.name,
            "duration_ms": self.duration_ms,
            "success": bool(self.success),
            "error": self.error,
            "rows_affected": self.rows_affected,
            "metadata": metadata,
        }


@dataclass
class BenchmarkRun:
    run_id: str
    timestamp: str = field(default_factory=utcnow_iso)
    environment_json: str = "{}"
    test_set: str = ""
    purpose: str = Purpose.NORMAL_CLASSIFICATION
    iterations: int = 0
    duration_ms: Optional[float] = None
    status: str = "completed"
    note: str = ""
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        try:
            environment = json.loads(self.environment_json or "{}")
        except ValueError:
            environment = {}
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "environment": environment,
            "test_set": self.test_set,
            "purpose": self.purpose,
            "iterations": self.iterations,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "note": self.note,
            "created_at": self.created_at,
        }


@dataclass
class BenchmarkResult:
    run_id: str
    provider: str
    model: Optional[str] = None
    fixture: str = ""
    iterations: int = 0
    success_count: int = 0
    schema_valid_count: int = 0
    failure_count: int = 0
    mean_latency_ms: Optional[float] = None
    p50_latency_ms: Optional[float] = None
    p95_latency_ms: Optional[float] = None
    mean_out_tokens: Optional[float] = None
    mean_tps: Optional[float] = None
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "model": self.model,
            "fixture": self.fixture,
            "iterations": self.iterations,
            "success_count": self.success_count,
            "schema_valid_count": self.schema_valid_count,
            "failure_count": self.failure_count,
            "mean_latency_ms": self.mean_latency_ms,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "mean_out_tokens": self.mean_out_tokens,
            "mean_tps": self.mean_tps,
            "created_at": self.created_at,
        }
