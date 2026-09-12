"""Provider-specific telemetry extraction behind one standard shape.

Each provider stashes its last response usage on ``_last_usage`` (set to
None at call start, filled only on a parsed response, so failures never
inherit stale counts). This module reads that stash into
``ProviderResponseMetadata`` without leaking provider response structures
into the rest of the application.

Error classification maps transport/provider exceptions to a controlled
(error_type, error_code) pair so 429s, 5xx, timeouts, and malformed
payloads are countable separately from generic failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from noema.infrastructure.ollama import OllamaError

try:
    from noema.infrastructure.providers import ProviderError
except Exception:  # pragma: no cover - import-time safety only
    ProviderError = ConnectionError  # type: ignore[assignment]


@dataclass
class ProviderResponseMetadata:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    exact: bool = False  # True only when the provider reported the counts.
    finish_reason: Optional[str] = None
    provider_request_id: Optional[str] = None
    # Decomposed durations (ms) when the transport exposes them. Ollama
    # reports eval/total durations; hosted non-streaming APIs report
    # neither, so these stay NULL rather than invented.
    generation_ms: Optional[float] = None
    total_duration_ms: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "exact": self.exact,
            "finish_reason": self.finish_reason,
            "provider_request_id": self.provider_request_id,
            "generation_ms": self.generation_ms,
            "total_duration_ms": self.total_duration_ms,
        }


def _as_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def take_usage(provider: Any) -> ProviderResponseMetadata:
    """Read (not clear) the provider's stashed last-response usage."""
    stash = getattr(provider, "_last_usage", None)
    if not isinstance(stash, Mapping):
        return ProviderResponseMetadata()
    return ProviderResponseMetadata(
        input_tokens=_as_int(stash.get("input_tokens")),
        output_tokens=_as_int(stash.get("output_tokens")),
        total_tokens=_as_int(stash.get("total_tokens")),
        exact=bool(stash.get("exact")),
        finish_reason=stash.get("finish_reason"),
        provider_request_id=stash.get("provider_request_id"),
        generation_ms=_as_float(stash.get("generation_ms")),
        total_duration_ms=_as_float(stash.get("total_duration_ms")),
    )


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number if number >= 0 else None


def classify_error(exc: BaseException) -> Tuple[str, Optional[str], str]:
    """Return (error_type, error_code, safe_message) for a call failure.

    The message is the exception class plus a truncated detail; prompts,
    responses, keys, and activity text never flow through exceptions here
    because providers only raise transport/schema errors.
    """
    text = str(exc)[:300]
    lowered = text.casefold()
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or "timeout" in lowered or "timed out" in lowered:
        return ("TIMEOUT", None, "{}: {}".format(name, text))
    if "429" in text or "rate limit" in lowered or "rate_limit" in lowered or "resource_exhausted" in lowered:
        return ("RATE_LIMITED", "429", "{}: {}".format(name, text))
    if "401" in text or "403" in text or "unauthorized" in lowered or "forbidden" in lowered or "api key" in lowered:
        return ("AUTH", None, "{}: {}".format(name, text))
    for code in ("500", "502", "503", "504"):
        if code in text or "overloaded" in lowered or "unavailable" in lowered:
            return ("SERVER_ERROR", code if code in text else None, "{}: {}".format(name, text))
    if "invalid json" in lowered or "non-json" in lowered or "not valid json" in lowered or isinstance(exc, ValueError):
        return ("MALFORMED", None, "{}: {}".format(name, text))
    if "quota" in lowered or "exhausted" in lowered:
        return ("QUOTA_DENIED", None, "{}: {}".format(name, text))
    if ("token limit" in lowered or "context length" in lowered
            or "maximum context" in lowered or "too long" in lowered
            or "too large" in lowered or "out of range" in lowered):
        return ("CONTEXT_OVERFLOW", None, "{}: {}".format(name, text))
    return ("PROVIDER_ERROR", None, "{}: {}".format(name, text))


def cost_for_model(model: Any) -> Tuple[Optional[float], str]:
    """Cost for one call when known without inventing pricing.

    Free-tier OpenRouter models (``:free`` suffix) carry an explicit 0
    provider charge while token usage is still measured. Everything else
    without provider-reported cost is UNKNOWN (NULL), never "$0".
    """
    from noema.observability.models import CostBasis

    if isinstance(model, str) and model.strip().lower().endswith(":free"):
        return (0.0, CostBasis.FREE_TIER)
    return (None, CostBasis.UNKNOWN)


def quota_scope_for(provider_name: Any) -> Optional[str]:
    """Quota scope label matching the quotas payload vocabulary."""
    name = str(provider_name or "").strip().lower()
    if name == "openrouter":
        return "shared-openrouter-guards"
    if name == "ollama":
        return "local"
    if name == "gemini":
        return "model-specific"
    return None


def assemble_invocation(
    *,
    request_id: str,
    timestamp_iso: str,
    timestamp_ms: int,
    provider_name: Any,
    model: Any,
    purpose: str,
    pipeline: str,
    operation: str,
    kind: str = "attempt",
    session_ids: Any = None,
    batch_size: int = 0,
    attempt_number: int = 0,
    fallback_depth: int = 0,
    fallback_from: Optional[str] = None,
    fallback_reason: Optional[str] = None,
    usage: Optional[ProviderResponseMetadata] = None,
    estimated_input_tokens: Optional[int] = None,
    error: Optional[BaseException] = None,
    success_payload: bool = False,
    latency_ms: Optional[float] = None,
    context_window: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    quota_scope: Optional[str] = None,
    quota_before: Optional[Mapping[str, Any]] = None,
    quota_after: Optional[Mapping[str, Any]] = None,
    batch_budget_tokens: Optional[int] = None,
    batch_input_tokens: Optional[int] = None,
    batch_utilization: Optional[float] = None,
    detection_id: Optional[str] = None,
    intervention_id: Optional[str] = None,
    prompt_version: Optional[str] = None,
    classifier_version: Optional[str] = None,
) -> "InvocationRecord":
    """Build one invocation record from primitives (single canonical path).

    Success requires BOTH no error AND a usable payload: a 200 with an
    unusable body is still a failed invocation. Token counts are EXACT
    only when the provider reported them; otherwise a length estimate is
    labeled ESTIMATED, or the fields stay NULL (UNKNOWN).
    """
    import json as _json

    from noema.observability.models import (
        InvocationRecord,
        Pipeline,
        Purpose,
        TokenBasis,
        invocation_id,
    )

    usage = usage or ProviderResponseMetadata()
    if error is not None:
        error_type, error_code, message = classify_error(error)
        success = False
    elif not success_payload:
        error_type, error_code, message = (
            "MALFORMED", None, "Malformed: provider returned no usable payload")
        success = False
    else:
        error_type, error_code, message = None, None, None
        success = True
    if usage.exact and usage.input_tokens is not None:
        in_tokens = usage.input_tokens
        out_tokens = usage.output_tokens
        total = usage.total_tokens
        if total is None and in_tokens is not None and out_tokens is not None:
            total = in_tokens + out_tokens
        basis = TokenBasis.EXACT
    elif estimated_input_tokens is not None:
        in_tokens = estimated_input_tokens
        out_tokens = None
        total = None
        basis = TokenBasis.ESTIMATED
    else:
        in_tokens = out_tokens = total = None
        basis = TokenBasis.UNKNOWN
    generation_ms = None
    if usage.exact and usage.generation_ms and usage.generation_ms > 0:
        generation_ms = usage.generation_ms
    tps, tps_basis = None, None
    if (usage.exact and usage.output_tokens is not None
            and generation_ms and generation_ms > 0):
        tps = round(usage.output_tokens / (generation_ms / 1000.0), 2)
        tps_basis = "generation_time"
    elif out_tokens is not None and latency_ms and latency_ms > 0:
        tps = round(out_tokens / (latency_ms / 1000.0), 2)
        tps_basis = "approx_total_latency"
    cost, cost_basis = cost_for_model(model)
    try:
        session_json = _json.dumps(list(session_ids or []), ensure_ascii=False, default=str)[:4000]
    except (TypeError, ValueError):
        session_json = "[]"
    try:
        before_json = _json.dumps(dict(quota_before or {}), ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        before_json = "{}"
    try:
        after_json = _json.dumps(dict(quota_after or {}), ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        after_json = "{}"
    return InvocationRecord(
        invocation_id=invocation_id(
            str(request_id), provider_name, model, attempt_number, timestamp_ms,
            str(kind or "attempt")),
        request_id=str(request_id),
        timestamp=timestamp_iso,
        provider=str(provider_name or "?"),
        model=str(model) if model is not None else None,
        purpose=Purpose.normalize(purpose),
        pipeline=Pipeline.normalize(pipeline),
        operation=str(operation or ""),
        kind=str(kind or "attempt"),
        session_ids_json=session_json,
        batch_size=int(batch_size or 0),
        attempt_number=int(attempt_number or 0),
        fallback_depth=int(fallback_depth or 0),
        fallback_from=fallback_from,
        fallback_reason=(str(fallback_reason)[:300] if fallback_reason else None),
        success=1 if success else 0,
        error_type=error_type,
        error_code=error_code,
        error_message=message,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        total_tokens=total,
        token_basis=basis,
        latency_ms=latency_ms,
        ttft_ms=None,
        generation_ms=generation_ms,
        tps=tps,
        tps_basis=tps_basis,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        quota_scope=quota_scope,
        quota_before_json=before_json,
        quota_after_json=after_json,
        cost=cost,
        cost_basis=cost_basis,
        batch_budget_tokens=batch_budget_tokens,
        batch_input_tokens=batch_input_tokens,
        batch_utilization=batch_utilization,
        detection_id=str(detection_id) if detection_id else None,
        intervention_id=str(intervention_id) if intervention_id else None,
        prompt_version=str(prompt_version) if prompt_version else None,
        classifier_version=str(classifier_version) if classifier_version else None,
    )
