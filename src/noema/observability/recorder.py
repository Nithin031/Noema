"""Recording entry point for product code.

``TelemetryRecorder`` is the single object product code talks to. It never
raises: every public method guards persistence so a metrics failure can
never break classification, detection, or intervention. Attach it as
``chain.observer`` / ``service.telemetry``; when absent (None), all
instrumentation call sites no-op and behavior is byte-identical to
uninstrumented runs.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Mapping, Optional

from noema.observability.models import (
    InvocationRecord,
    OperationRecord,
    new_request_id,  # noqa: F401 - re-exported for existing importers
    utcnow_iso,
)
from noema.observability.provider_telemetry import (
    ProviderResponseMetadata,
    take_usage,
)
from noema.observability.repository import TelemetryRepository


def _safe_json(value: Any, limit: int = 2000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return "{}"
    return text[:limit]


class _ModelCallHandle:
    """Mutable handle filled by the ``observe_model_call`` block."""

    def __init__(self) -> None:
        self.usage: ProviderResponseMetadata = ProviderResponseMetadata()
        self.estimated_input_tokens: Optional[int] = None
        self.session_ids: list = []
        self.batch_size: int = 0
        self.context_window: Optional[int] = None
        self.max_output_tokens: Optional[int] = None
        self.quota_scope: Optional[str] = None
        self.quota_before: Optional[Dict[str, Any]] = None
        self.quota_after: Optional[Dict[str, Any]] = None
        self.error: Optional[BaseException] = None
        self.success_payload: bool = False


class TelemetryRecorder:
    """Thread-safe invocation/operation recorder. Never raises."""

    def __init__(self, store: Any = None, repository: Optional[TelemetryRepository] = None):
        self._lock = threading.RLock()
        if repository is not None:
            self.repository = repository
        elif store is not None:
            self.repository = TelemetryRepository(store)
        else:
            self.repository = None

    # -- low-level writes (all guarded) ------------------------------------

    def record_invocation(self, record: InvocationRecord) -> bool:
        try:
            if self.repository is None:
                return False
            with self._lock:
                return bool(self.repository.insert_invocation(record))
        except Exception:
            return False

    def record_operation(self, kind: str, name: str,
                         duration_ms: Optional[float] = None,
                         success: bool = True,
                         error: Optional[str] = None,
                         rows_affected: Optional[int] = None,
                         metadata: Optional[Mapping[str, Any]] = None) -> bool:
        try:
            if self.repository is None:
                return False
            record = OperationRecord(
                kind=str(kind), name=str(name), duration_ms=duration_ms,
                success=1 if success else 0,
                error=str(error)[:300] if error else None,
                rows_affected=rows_affected,
                metadata_json=_safe_json(dict(metadata or {})),
            )
            with self._lock:
                self.repository.insert_operation(record)
            return True
        except Exception:
            return False

    # -- model-call observation ---------------------------------------------

    @contextmanager
    def observe_model_call(self, provider: Any, model: Any, purpose: str,
                           pipeline: str, operation: str, request_id: str,
                           attempt_number: int = 0, fallback_depth: int = 0,
                           fallback_from: Optional[str] = None,
                           fallback_reason: Optional[str] = None,
                           kind: str = "attempt") -> Iterator[_ModelCallHandle]:
        """Time one provider call and persist its invocation record.

        The wrapped block performs the actual provider call and fills
        ``handle.usage`` (via ``take_usage``), estimates, quota snapshots,
        and ``handle.error`` on failure. Success requires BOTH no error AND
        ``handle.success_payload`` True, because a 200 with an unusable body
        is still a failed invocation for our purposes.
        """
        handle = _ModelCallHandle()
        started_perf = time.perf_counter()
        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        timestamp_iso = utcnow_iso()
        error: Optional[BaseException] = None
        try:
            yield handle
        except BaseException as exc:  # noqa: BLE001 - record, then re-raise
            error = exc
            handle.error = exc
            raise
        finally:
            try:
                from noema.observability.provider_telemetry import (
                    assemble_invocation,
                )

                latency_ms = round((time.perf_counter() - started_perf) * 1000, 1)
                record = assemble_invocation(
                    request_id=str(request_id),
                    timestamp_iso=timestamp_iso,
                    timestamp_ms=timestamp_ms,
                    provider_name=getattr(provider, "name", "?"),
                    model=model,
                    purpose=purpose,
                    pipeline=pipeline,
                    operation=operation,
                    kind=kind,
                    session_ids=list(handle.session_ids),
                    batch_size=int(handle.batch_size or 0),
                    attempt_number=int(attempt_number or 0),
                    fallback_depth=int(fallback_depth or 0),
                    fallback_from=fallback_from,
                    fallback_reason=fallback_reason,
                    usage=handle.usage,
                    estimated_input_tokens=handle.estimated_input_tokens,
                    error=error if error is not None else handle.error,
                    success_payload=bool(handle.success_payload),
                    latency_ms=latency_ms,
                    context_window=handle.context_window,
                    max_output_tokens=handle.max_output_tokens,
                    quota_scope=handle.quota_scope,
                    quota_before=handle.quota_before,
                    quota_after=handle.quota_after,
                )
                self.record_invocation(record)
            except Exception:
                pass

    @contextmanager
    def observe_operation(self, kind: str, name: str,
                          metadata: Optional[Mapping[str, Any]] = None,
                          rows_affected: Optional[int] = None) -> Iterator[Dict[str, Any]]:
        """Time a non-model operation (worker run, DB stage, API request)."""
        state: Dict[str, Any] = {"error": None, "rows_affected": rows_affected,
                                 "metadata": dict(metadata or {})}
        started_perf = time.perf_counter()
        try:
            yield state
        except BaseException as exc:  # noqa: BLE001 - record, then re-raise
            state["error"] = "{}: {}".format(type(exc).__name__, str(exc)[:200])
            raise
        finally:
            try:
                duration_ms = round((time.perf_counter() - started_perf) * 1000, 1)
                self.record_operation(
                    kind, name, duration_ms=duration_ms,
                    success=state.get("error") is None,
                    error=state.get("error"),
                    rows_affected=state.get("rows_affected"),
                    metadata=state.get("metadata") or {},
                )
            except Exception:
                pass
