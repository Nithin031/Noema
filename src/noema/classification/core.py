"""Generation 2 session classification using local Ollama first."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from noema.domain.activity import coerce_timestamp
from noema.classification.policy import assess_policy
from noema.infrastructure.ollama import OllamaClient, OllamaError
from noema.domain.session_context import is_meaningful_session
from noema.domain.sessions import ActivitySession

from noema.infrastructure.providers import (
    ProviderChain, ModelProvider, OllamaProvider, ProviderError,
    WORKHORSE_MODEL, ESCALATION_MODEL, ESCALATION_MAX_PER_DAY,
    ESCALATION_MAX_CONFIDENCE, ESCALATION_MIN_DURATION_SECONDS,
    OUTPUT_TOKEN_RESERVE,
)
from noema.observability.models import (
    Pipeline,
    Purpose,
    new_request_id,
    utcnow_iso,
)
from noema.observability.provider_telemetry import (
    assemble_invocation,
    quota_scope_for,
    take_usage,
)


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.IGNORECASE | re.DOTALL)

_ALLOWED_CATEGORIES = {"productive", "distractive", "neutral"}
_LEGACY_CATEGORY_TO_PRODUCTIVITY = {
    "distracting": "distractive",
    "entertainment": "distractive",
    "media/social_media": "distractive",
    "shopping": "distractive",
    "productive": "productive",
    "development": "productive",
    "research": "productive",
    "communication": "productive",
    "learning": "productive",
    "work/programming": "productive",
    "work/writing": "productive",
    "work/research": "productive",
    "work/engineering": "productive",
    "work/data": "productive",
    "comms/im": "productive",
    "comms/meetings": "productive",
    "comms/email": "productive",
    "admin": "neutral",
    "other": "neutral",
}
_PRODUCTIVITY_VALUES = {"productive", "neutral", "distracting"}
_INVALID_SIGNAL = "Classifier response invalid or unavailable"
_OLLAMA_UNAVAILABLE_SIGNAL = "Ollama classification unavailable"

from noema.classification.contracts import PROMPT_VERSION, SYSTEM_PROMPT

# Context-window budget for the live Ollama path. llama3.2:3b has a
# 131072-token architecture window, but the daemon serves with
# num_ctx=8192 (see OllamaClient). Reserve ~700 tokens for the JSON
# answer and keep prompts inside ~24000 chars (~6000 tokens at ~4
# chars/token) so every request fits with margin. Larger evidence is
# chunked into several model calls and synthesized, never cut silently.
OLLAMA_EFFECTIVE_CONTEXT = 8192
RESERVED_OUTPUT_TOKENS = 600
SAFETY_MARGIN_TOKENS = 800
MAX_ACTIVITIES = 40
MAX_EVIDENCE_STRINGS = 40
MAX_RECENT_CONTEXT = 4
MAX_RAW_CONTEXT_ITEMS = 20
MAX_TEXT_FIELD_CHARS = 500
CHUNK_RAW_ITEMS = 12
CLASSIFY_MAX_ATTEMPTS = 3
CLASSIFY_RETRY_DELAYS = (0.2, 0.5)


# Classifier logic contract version, stamped alongside PROMPT_VERSION. Bump
# when the parsing/decision logic (not just the prompt text) changes so a
# cached in-memory verdict from an older logic version is never reused.
CLASSIFIER_VERSION = "1"

@dataclass(frozen=True)
class Classification:
    """Structured meaning assigned to one session."""

    session_id: str
    category: str = "other"
    topic: Optional[str] = None
    project: Optional[str] = None
    activity_type: str = "browsing"
    productivity: str = "neutral"
    confidence: float = 0.0
    provider: str = "ollama"
    model: Optional[str] = None
    subcategory: str = "other"
    activity: str = "Ambiguous activity"
    signal: str = "Evidence was insufficient to determine the activity."
    source: str = "pending"
    classification_status: str = "pending"
    classified_at: Optional[datetime] = None
    service: Optional[str] = None
    # Evidence never comes from the model trusting itself. Quality is the
    # deterministic session-observation layer; the model may note the same
    # limit but never invents strength.
    evidence_quality: Optional[str] = None
    evidence_quality_score: Optional[float] = None
    evidence_reason: Optional[str] = None
    intent_signal: Optional[str] = None
    last_error: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    classifier_version: str = "1"
    prompt_version: str = "1"
    retry_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", str(self.session_id))
        raw_category = str(self.category or "neutral").strip().lower()
        raw_productivity = str(self.productivity or "neutral").strip().lower()
        if raw_productivity not in _PRODUCTIVITY_VALUES:
            raw_productivity = "neutral"
        canonical_category = _LEGACY_CATEGORY_TO_PRODUCTIVITY.get(raw_category, raw_category)
        if canonical_category not in _ALLOWED_CATEGORIES:
            canonical_category = "distractive" if raw_productivity == "distracting" else raw_productivity
        object.__setattr__(self, "category", canonical_category)
        object.__setattr__(self, "productivity", raw_productivity)
        activity_type = str(self.activity_type or "browsing").strip().lower()
        object.__setattr__(self, "activity_type", activity_type or "browsing")
        for field_name in (
            "topic", "project", "model", "subcategory", "activity", "signal",
            "source", "service", "intent_signal", "last_error",
            "classifier_version", "prompt_version", "evidence_quality", "evidence_reason",
        ):
            value = getattr(self, field_name)
            if value:
                object.__setattr__(self, field_name, str(value).strip())
        object.__setattr__(self, "prompt_version", str(self.prompt_version or "1").strip() or "1")
        try:
            retry_count = int(self.retry_count or 0)
        except (TypeError, ValueError):
            retry_count = 0
        object.__setattr__(self, "retry_count", max(0, retry_count))
        if self.next_retry_at is not None:
            object.__setattr__(self, "next_retry_at", coerce_timestamp(self.next_retry_at))
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        object.__setattr__(self, "confidence", min(1.0, max(0.0, confidence)))
        try:
            eq_score = float(self.evidence_quality_score) if self.evidence_quality_score is not None else None
        except (TypeError, ValueError):
            eq_score = None
        if eq_score is not None:
            eq_score = min(1.0, max(0.0, eq_score))
        object.__setattr__(self, "evidence_quality_score", eq_score)
        signal = str(self.signal or _INVALID_SIGNAL).strip()
        if signal.casefold() in {self.category.casefold(), self.productivity.casefold()}:
            signal = _INVALID_SIGNAL
        object.__setattr__(self, "signal", signal)
        object.__setattr__(self, "activity", str(self.activity or "Ambiguous activity").strip())
        object.__setattr__(self, "subcategory", str(self.subcategory or "other").strip().lower().replace(" ", "_"))
        source = str(self.source or self.provider or "pending").strip().lower()
        object.__setattr__(self, "source", source)
        status = str(self.classification_status or "classified").strip().lower()
        if status not in {"pending", "classified", "classification_failed"}:
            status = "classified"
        if source in {"fallback", "validation"}:
            status = "classification_failed"
        object.__setattr__(self, "classification_status", status)
        if self.classified_at is not None:
            object.__setattr__(self, "classified_at", coerce_timestamp(self.classified_at))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "category": self.category,
            "topic": self.topic,
            "project": self.project,
            "activity_type": self.activity_type,
            "productivity": self.productivity,
            "confidence": self.confidence,
            "provider": self.provider,
            "model": self.model,
            "subcategory": self.subcategory,
            "activity": self.activity,
            "signal": self.signal,
            "source": self.source,
            "classification_status": self.classification_status,
            "classified_at": self.classified_at.isoformat().replace("+00:00", "Z") if self.classified_at else None,
            "service": self.service,
            "intent_signal": self.intent_signal,
            "evidence_quality": self.evidence_quality,
            "evidence_quality_score": self.evidence_quality_score,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "next_retry_at": self.next_retry_at.isoformat().replace("+00:00", "Z") if self.next_retry_at else None,
            "classifier_version": self.classifier_version,
            "prompt_version": self.prompt_version,
        }


@dataclass(frozen=True)
class ClassificationJobTelemetry:
    """Observable metrics for one bounded semantic-analysis job."""

    job_id: str
    started_at: datetime
    completed_at: datetime
    provider: str
    model: Optional[str]
    input_session_count: int
    successful_calls: int
    failed_calls: int
    timeout_count: int
    parse_failures: int
    latency_ms: float
    status: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "completed_at": self.completed_at.isoformat().replace("+00:00", "Z"),
            "provider": self.provider,
            "model": self.model,
            "input_session_count": self.input_session_count,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "timeout_count": self.timeout_count,
            "parse_failures": self.parse_failures,
            "latency_ms": self.latency_ms,
            "status": self.status,
        }


class Classifier:
    """Classify sessions with Ollama. Pending if Ollama is unavailable."""

    def __init__(
        self,
        client: Optional[OllamaClient] = None,
        provider: Optional[ModelProvider] = None,
        escalation_model: Optional[str] = ESCALATION_MODEL,
        escalation_max_per_day: int = ESCALATION_MAX_PER_DAY,
    ):
        if client is not None and provider is not None:
            raise ValueError("pass either client or provider, not both")
        self.provider = provider or OllamaProvider(client=client)
        # Selective escalation target (stronger model, explicit criteria
        # only — never an unconditional workhorse). None disables it.
        self.escalation_model = str(escalation_model).strip() if escalation_model else None
        self.escalation_max_per_day = max(0, int(escalation_max_per_day))
        # ``client`` remains as a compatibility attribute for callers that
        # supplied the pre-provider Ollama client interface.
        self.client = getattr(self.provider, "client", self.provider)
        # Observability hook, wired by daemon/cli.py. When None, no
        # telemetry is produced. The chain records its own attempts when
        # it carries an observer; this classifier records only direct
        # (non-chain) provider calls, so nothing is ever double-counted.
        self.observer: Optional[Any] = None
        self._classification_cache: Dict[str, Classification] = {}
        self._job_lock = threading.RLock()
        self._job_history: List[ClassificationJobTelemetry] = []
        self._last_failure_kind: Optional[str] = None
        self._last_call_attempted = False

    # -- observability (emits events, never changes routing) ---------------

    @staticmethod
    def _tele_context(telemetry: Optional[Mapping[str, Any]], operation: str,
                      purpose: str = Purpose.NORMAL_CLASSIFICATION,
                      pipeline: str = Pipeline.SEMANTIC_CLASSIFICATION) -> Dict[str, Any]:
        tele = dict(telemetry or {})
        tele.setdefault("purpose", purpose)
        tele.setdefault("pipeline", pipeline)
        tele["operation"] = operation
        if not tele.get("request_id"):
            tele["request_id"] = new_request_id()
        return tele

    def _chain_records(self) -> bool:
        """True when the provider records its own attempts (no doubles)."""
        return (isinstance(self.provider, ProviderChain)
                and getattr(self.provider, "observer", None) is not None)

    def _escalation_target(self) -> Optional[Any]:
        """The escalation provider inside the chain, if configured."""
        chain = self.provider
        if not isinstance(chain, ProviderChain) or not self.escalation_model:
            return None
        for candidate in list(getattr(chain, "providers", None) or []):
            if getattr(candidate, "model", None) == self.escalation_model:
                return candidate
        return None

    def _escalation_budget_left(self) -> bool:
        ledger = getattr(self.provider, "ledger", None)
        if ledger is None or not self.escalation_model:
            return ledger is None
        try:
            used = ledger.usage("escalation", self.escalation_model).get("requests", 0) or 0
        except (AttributeError, OSError, TypeError, ValueError):
            return True
        return int(used) < max(1, int(self.escalation_max_per_day))

    def _escalation_candidate(self, session: Any, result: Classification) -> bool:
        """Deterministic escalation criteria (explicit conditions only)."""
        if result.classification_status != "classified":
            return False
        try:
            confidence = float(result.confidence)
        except (TypeError, ValueError):
            return False
        if confidence >= ESCALATION_MAX_CONFIDENCE:
            return False
        quality = str(result.evidence_quality or "").strip().lower()
        if not quality:
            session_quality = getattr(session, "evidence_quality", None)
            quality = str(getattr(session_quality, "value", session_quality) or "").strip().lower()
        if quality not in {"weak", "absent"}:
            return False
        try:
            duration = float(getattr(session, "duration", 0) or 0)
        except (TypeError, ValueError):
            return False
        return duration >= ESCALATION_MIN_DURATION_SECONDS

    def _escalate_one(
        self,
        session: Any,
        result: Classification,
        context: Optional[Iterable[Mapping[str, Any]]] = None,
        telemetry: Optional[Mapping[str, Any]] = None,
    ) -> Classification:
        """Run one bounded escalation call; adopt only a better verdict."""
        target = self._escalation_target()
        if target is None or not self._escalation_budget_left():
            return result
        chain = self.provider
        state = (getattr(chain, "rate_limits", None) or {}).get(getattr(target, "model", None))
        prompt = self.build_prompt(session, context)
        counter = getattr(target, "count_tokens", None)
        try:
            billable = int(counter(prompt)) + OUTPUT_TOKEN_RESERVE if callable(counter) else OUTPUT_TOKEN_RESERVE
        except (AttributeError, OSError, TypeError, ValueError):
            billable = OUTPUT_TOKEN_RESERVE
        if state is not None:
            if not state.available(billable):
                return result
            try:
                if not chain.ledger.check("llm", target.model, billable, state.rpd_limit):
                    return result
            except (AttributeError, OSError, TypeError, ValueError):
                return result
        ledger = getattr(chain, "ledger", None)
        if ledger is not None:
            try:
                ledger.consume("escalation", target.model, billable)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        tele = self._tele_context(telemetry, "ESCALATION")
        quota_before = {
            "requests_today": int(getattr(state, "requests_today", 0) or 0),
            "attempts_today": int(getattr(state, "attempts_today", 0) or 0),
        } if state is not None else {}
        started = time.perf_counter()
        if state is not None:
            state.record_attempt()
        try:
            self._last_call_attempted = True
            payload = target.classify(session, prompt)
            call_error = None
            usage = take_usage(target)
        except (ProviderError, OSError, TimeoutError, ConnectionError) as exc:
            call_error = exc
            usage = take_usage(target)
            if state is not None:
                state.record_failure(exc)
                text = str(exc)
                if "429" in text or "resource_exhausted" in text.casefold():
                    state.record_rate_limited()
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        self._emit_invocation(
            request_id=tele.get("request_id"), timestamp_iso=utcnow_iso(),
            timestamp_ms=int(started * 1000),
            provider_name=getattr(target, "name", "escalation"),
            model=getattr(target, "model", None),
            purpose=tele.get("purpose"), pipeline=tele.get("pipeline"),
            operation="ESCALATION", kind="attempt",
            session_ids=[getattr(session, "id", None)],
            batch_size=0, attempt_number=0,
            fallback_depth=0, fallback_from=getattr(self.provider, "last_provider", None),
            fallback_reason="selective escalation: low-confidence weak-evidence verdict",
            usage=usage if call_error is None else None,
            estimated_input_tokens=billable,
            error=call_error, success_payload=call_error is None,
            latency_ms=latency_ms,
            context_window=None, max_output_tokens=800,
            quota_scope="model-specific",
            quota_before=quota_before, quota_after=quota_before,
            prompt_version=PROMPT_VERSION,
            classifier_version=getattr(result, "classifier_version", "1"),
        )
        if call_error is not None:
            return result
        if state is not None:
            state.record_success(billable)
            try:
                chain.ledger.consume("llm", target.model, billable)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        escalated = self._from_payload(
            session, payload,
            getattr(target, "name", "escalation"),
            getattr(target, "model", None),
        )
        if escalated.classification_status != "classified":
            return result
        try:
            better = float(escalated.confidence) >= float(result.confidence or 0.0)
        except (TypeError, ValueError):
            return result
        if not better:
            return result
        self._classification_cache[self._cache_key(session, context)] = escalated
        return escalated

    def _emit_invocation(self, **fields: Any) -> bool:
        observer = self.observer
        if observer is None:
            return False
        try:
            recorder = getattr(observer, "record_invocation", None)
            if not callable(recorder):
                return False
            return bool(recorder(assemble_invocation(**fields)))
        except Exception:
            return False

    def _emit_operation(self, kind: str, name: str, duration_ms: Optional[float] = None,
                        success: bool = True, error: Optional[str] = None,
                        metadata: Optional[Mapping[str, Any]] = None) -> bool:
        observer = self.observer
        if observer is None:
            return False
        try:
            recorder = getattr(observer, "record_operation", None)
            if not callable(recorder):
                return False
            return bool(recorder(kind, name, duration_ms=duration_ms,
                                 success=success, error=error,
                                 metadata=dict(metadata or {})))
        except Exception:
            return False

    @staticmethod
    def _now_pair() -> tuple:
        """(iso_timestamp, epoch_ms) from one clock read."""
        now = datetime.now(timezone.utc)
        return (now.isoformat().replace("+00:00", "Z"), int(now.timestamp() * 1000))

    @staticmethod
    def _cache_key(session: Any, recent_context: Optional[Iterable[Mapping[str, Any]]] = None) -> str:
        # session.id is included so that two different sessions with identical
        # observable fields (same app/title/domain or same project/topic on a
        # MeaningfulSession) never share a cache entry.  Without this,
        # MeaningfulSessions that lack app/title/domain all hash identically
        # when their primary_project/topic are also None, and the first
        # session's verdict contaminates every subsequent one.
        evidence = {
            "session_id": getattr(session, "id", None),
            "app": getattr(session, "app", None),
            "title": getattr(session, "title", None),
            "domain": getattr(session, "domain", None),
            "url": getattr(session, "url", None),
            "project": getattr(session, "primary_project", None),
            "topic": getattr(session, "primary_topic", None),
            "context": list(recent_context or []),
            # Semantic identity: a cached verdict is only valid for the exact
            # prompt + classifier logic that produced it. Bumping either
            # version invalidates every in-memory cache entry so a stale
            # verdict from an older contract can never be reused. (Goal
            # context is deliberately absent — classification describes the
            # observed activity independently of any goal; goal relevance is
            # a separate, downstream alignment computation.)
            "prompt_version": PROMPT_VERSION,
            "classifier_version": CLASSIFIER_VERSION,
        }
        return json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str).casefold()

    @staticmethod
    def _truncate_text(value: Any, limit: int = MAX_TEXT_FIELD_CHARS) -> Any:
        return value

    @staticmethod
    def _compact_context_item(item: Mapping[str, Any]) -> Dict[str, Any]:
        compact: Dict[str, Any] = {}
        for key in (
            "application", "app", "window_title", "title", "domain",
            "url", "duration_seconds", "device", "browser",
            "browser_window_id", "browser_tab_id", "event_count",
        ):
            if item.get(key) is not None:
                compact[key] = item.get(key)
        return compact

    @staticmethod
    def _observed_values(recent_context: Iterable[Mapping[str, Any]], keys: Iterable[str]) -> List[str]:
        seen: List[str] = []
        for item in recent_context or []:
            if not isinstance(item, Mapping):
                continue
            for key in keys:
                value = item.get(key)
                if isinstance(value, str) and value.strip() and value.strip() not in seen:
                    seen.append(value.strip())
        return seen

    @staticmethod
    def _fit_prompt(context: Dict[str, Any]) -> Dict[str, Any]:
        return dict(context)

    @staticmethod
    def build_evidence(session: Any, recent_context: Optional[Iterable[Mapping[str, Any]]] = None) -> Dict[str, Any]:
        """Build the evidence payload without the system prompt.

        Batch classification sends one shared instruction header plus one
        evidence object per session, so the system text is not repeated per
        item and the token budget holds far more sessions.
        """
        if is_meaningful_session(session):
            raw_recent = [Classifier._compact_context_item(item) for item in (recent_context or []) if isinstance(item, Mapping)]
            observed_apps = Classifier._observed_values(raw_recent, ("application", "app"))
            observed_titles = Classifier._observed_values(raw_recent, ("window_title", "title"))
            observed_domains = Classifier._observed_values(raw_recent, ("domain",))
            observed_urls = Classifier._observed_values(raw_recent, ("url",))
            try:
                active_seconds = float(
                    getattr(session, "active_duration_seconds", 0)
                    or session.duration)
            except (TypeError, ValueError):
                active_seconds = 0.0
            policy_prior = assess_policy(
                observed_apps[0] if observed_apps else None,
                observed_titles[0] if observed_titles else None,
                observed_domains[0] if observed_domains else None,
                observed_urls[0] if observed_urls else None,
                active_seconds,
                observed_apps,
            ).to_dict()
            context = {
                "session_type": "meaningful",
                "start_time": session.start_time.isoformat(),
                "end_time": session.end_time.isoformat(),
                "duration_seconds": session.duration,
                # Active (interacted) seconds the verdict must describe.
                # AFK time inside the span is excluded from classification.
                "active_duration_seconds": round(float(
                    getattr(session, "active_duration_seconds", 0) or 0), 1),
                "presence": "active",
                "device_set": list(session.device_set),
                "browser": session.browser,
                "browser_window_id": session.browser_window_id,
                "browser_tab_id": session.browser_tab_id,
                "raw_activity_session_ids": list(session.activity_session_ids),
                "primary_project": Classifier._truncate_text(session.primary_project),
                "primary_task": Classifier._truncate_text(session.primary_task),
                "primary_topic": Classifier._truncate_text(session.primary_topic),
                "activities": list(session.activities),
                "primary_app": getattr(session, "app", None) or (observed_apps[0] if observed_apps else None),
                "primary_domain": getattr(session, "domain", None) or (observed_domains[0] if observed_domains else None),
                "primary_title": Classifier._truncate_text(getattr(session, "title", None) or (observed_titles[0] if observed_titles else None)),
                "observed_applications": observed_apps,
                "observed_titles": observed_titles,
                "observed_domains": observed_domains,
                "observed_urls": observed_urls,
                "event_count": getattr(session, "event_count", 0),
                "device": getattr(session, "device", None),
                "source": getattr(session, "source", "meaningful_session"),
                "evidence": [Classifier._truncate_text(e) for e in list(session.evidence)],
                "evidence_quality": getattr(session, "evidence_quality", None).value if hasattr(session, 'evidence_quality') and session.evidence_quality else None,
                # Deterministic user policy prior (Class A/B/C + games).
                # Observed input only: the model still decides from the
                # full evidence, but policy priors are explicit and auditable.
                "policy_prior": policy_prior,
            }
            context["recent_context"] = raw_recent
            context["raw_activity_evidence"] = raw_recent
        else:
            context = {
                "session_type": "activity",
                "app": Classifier._truncate_text(session.app),
                "domain": session.domain,
                "title": Classifier._truncate_text(session.title),
                "url": Classifier._truncate_text(getattr(session, "url", None)),
                "device": session.device,
                "duration_seconds": session.duration,
                "event_count": session.event_count,
                "source": session.source,
            }
            recent_items = [item for item in (recent_context or []) if isinstance(item, Mapping)]
            # Member-of-episode envelope (see NoemaService._member_context):
            # the group this item was tokenized from, stated explicitly so
            # the model never has to guess group boundaries. Episode keys
            # are reserved here and never treated as neighboring activity.
            episode = next((item for item in recent_items if "episode_task" in item), None)
            if episode is not None:
                context["episode"] = {
                    "task": Classifier._truncate_text(episode.get("episode_task")),
                    "topic": Classifier._truncate_text(episode.get("episode_topic")),
                    "project": Classifier._truncate_text(episode.get("episode_project")),
                }
            context["recent_context"] = [
                Classifier._compact_context_item(item)
                for item in recent_items if "episode_task" not in item
            ]
            
        context = Classifier._fit_prompt(context)
        return context

    @staticmethod
    def build_prompt(session: Any, recent_context: Optional[Iterable[Mapping[str, Any]]] = None) -> str:
        context = Classifier.build_evidence(session, recent_context)
        return SYSTEM_PROMPT + "\nCONTEXT PAYLOAD:\n" + json.dumps(context, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _payload_from_response(payload: Mapping[str, Any]) -> Dict[str, Any]:
        data = dict(payload)
        # Some compatible local endpoints return a fenced JSON string even when
        # asked for format=json. Accept it without weakening the schema.
        if len(data) == 1 and isinstance(data.get("response"), str):
            raw = _FENCE_RE.sub(r"\1", data["response"]).strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, dict):
                return parsed
        return data

    @staticmethod
    def _from_payload(session: Any, payload: Mapping[str, Any], provider: str, model: Optional[str]) -> Classification:
        data = Classifier._payload_from_response(payload)
        raw_category = str(data.get("category", "") or "").strip().lower()
        productivity = str(data.get("productivity", "neutral") or "neutral").strip().lower()
        raw_productivity = productivity
        if productivity not in _PRODUCTIVITY_VALUES:
            productivity = "neutral"
        # Provider responses have a strict public contract. Legacy labels are
        # normalized when old rows are loaded into Classification, but
        # a fresh Ollama/Gemini response must use one of the three categories.
        category = raw_category if raw_category in _ALLOWED_CATEGORIES else _LEGACY_CATEGORY_TO_PRODUCTIVITY.get(raw_category)
        invalid = category not in _ALLOWED_CATEGORIES or (
            "productivity" in data and raw_productivity not in _PRODUCTIVITY_VALUES
        )
        activity = data.get("activity")
        signal = data.get("signal")
        if not isinstance(activity, str) or not activity.strip() or not isinstance(signal, str) or not signal.strip():
            invalid = True
        # Provenance is assigned by the transport boundary, never trusted from
        # a model payload. Heuristic output must not look like model output.
        if invalid:
            source = "pending"
        elif provider in {"ollama", "gemini", "openrouter"}:
            source = provider
        elif provider == "heuristic":
            source = "heuristic"
        else:
            source = "pending"
        if invalid:
            category = "neutral"
            productivity = "neutral"
            return Classification(
                session_id=session.id, category=category, subcategory="ambiguous",
                activity="Ambiguous activity", signal=_INVALID_SIGNAL, confidence=0.0,
                provider=provider, model=model, source=source,
                classification_status="classification_failed",
                classified_at=datetime.now(timezone.utc),
                prompt_version=PROMPT_VERSION,
            )
        return Classification(
            session_id=session.id,
            category=category,
            topic=data.get("topic"),
            project=data.get("project"),
            activity_type=data.get("activity_type", "browsing"),
            productivity=productivity,
            confidence=data.get("confidence", 0.0),
            provider=provider,
            model=model,
            subcategory=data.get("subcategory", data.get("category", "other")),
            activity=activity,
            signal=signal,
            source=source,
            classification_status="classified",
            classified_at=datetime.now(timezone.utc),
            service=data.get("service"),
            intent_signal=data.get("intent_signal"),
            evidence_quality=data.get("evidence_quality"),
            evidence_quality_score=data.get("evidence_quality_score"),
            prompt_version=PROMPT_VERSION,
        )

    @staticmethod
    def _raw_evidence_items(recent_context: Optional[Iterable[Mapping[str, Any]]]) -> List[Mapping[str, Any]]:
        """Return the per-window evidence dicts (raw + previous sessions)."""
        items: List[Mapping[str, Any]] = []
        for item in recent_context or []:
            if not isinstance(item, Mapping):
                continue
            if any(key in item for key in ("url", "domain", "title", "window_title", "application", "app")):
                items.append(item)
        return items

    def _call_with_retry(self, session: Any, prompt: str,
                           tele: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        """Call the model, retrying transient transport failures.

        Every attempt is Ollama-only. If all attempts fail the caller keeps
        the session pending; no label is ever synthesized locally.

        Telemetry: attempts (and one logical request record) are emitted
        here only when the provider does not record itself (direct
        providers). Chain providers with an observer cover their own calls.
        """
        last_exc: Optional[BaseException] = None
        counter = getattr(self.provider, "count_tokens", None)
        estimated: Optional[int] = None
        if callable(counter):
            try:
                input_tokens = counter(prompt)
            except AttributeError:
                input_tokens = None
            if input_tokens is not None:
                estimated = input_tokens
                usable_budget = OLLAMA_EFFECTIVE_CONTEXT - RESERVED_OUTPUT_TOKENS - SAFETY_MARGIN_TOKENS
                if input_tokens > usable_budget:
                    raise ProviderError(
                        "request exceeds token budget: {} > {}".format(input_tokens, usable_budget)
                    )
        record_direct = not self._chain_records()
        tele = self._tele_context(tele, "SINGLE_CLASSIFY")
        session_ids = [getattr(session, "id", None)] if getattr(session, "id", None) else []
        provider_name = getattr(self.provider, "name", "?")
        provider_model = getattr(self.provider, "model", None)
        request_started = time.perf_counter()
        attempts_made = 0
        for attempt in range(CLASSIFY_MAX_ATTEMPTS):
            try:
                self._last_call_attempted = True
                started = time.perf_counter()
                try:
                    payload = self.provider.classify(session, prompt)
                    call_error = None
                except (ProviderError, OllamaError, OSError) as exc:
                    call_error = exc
                    payload = {}
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                attempts_made += 1
                if record_direct:
                    stamp_iso, stamp_ms = self._now_pair()
                    self._emit_invocation(
                        request_id=tele["request_id"], timestamp_iso=stamp_iso,
                        timestamp_ms=stamp_ms, provider_name=provider_name,
                        model=provider_model, purpose=tele["purpose"],
                        pipeline=tele["pipeline"], operation="SINGLE_CLASSIFY",
                        kind="attempt", session_ids=session_ids,
                        batch_size=0, attempt_number=attempt,
                        fallback_depth=0, fallback_from=None, fallback_reason=None,
                        usage=take_usage(self.provider) if call_error is None else None,
                        estimated_input_tokens=estimated,
                        error=call_error, success_payload=bool(payload),
                        latency_ms=latency_ms,
                        context_window=getattr(self.provider, "model_context_tokens", None),
                        max_output_tokens=None,
                        quota_scope=quota_scope_for(provider_name),
                        quota_before={}, quota_after={},
                    )
                if call_error is not None:
                    raise call_error
                if record_direct:
                    stamp_iso, stamp_ms = self._now_pair()
                    self._emit_invocation(
                        request_id=tele["request_id"], timestamp_iso=stamp_iso,
                        timestamp_ms=stamp_ms, provider_name=provider_name,
                        model=provider_model, purpose=tele["purpose"],
                        pipeline=tele["pipeline"], operation="SINGLE_CLASSIFY",
                        kind="request", session_ids=session_ids,
                        batch_size=0, attempt_number=0,
                        fallback_depth=0, fallback_from=None, fallback_reason=None,
                        usage=None, estimated_input_tokens=None,
                        error=None, success_payload=True,
                        latency_ms=round((time.perf_counter() - request_started) * 1000, 1),
                        context_window=getattr(self.provider, "model_context_tokens", None),
                        max_output_tokens=None,
                        quota_scope=quota_scope_for(provider_name),
                        quota_before={}, quota_after={},
                    )
                return payload
            except (ProviderError, OllamaError, OSError) as exc:
                last_exc = exc
                if attempt < CLASSIFY_MAX_ATTEMPTS - 1:
                    time.sleep(CLASSIFY_RETRY_DELAYS[min(attempt, len(CLASSIFY_RETRY_DELAYS) - 1)])
        if record_direct:
            stamp_iso, stamp_ms = self._now_pair()
            self._emit_invocation(
                request_id=tele["request_id"], timestamp_iso=stamp_iso,
                timestamp_ms=stamp_ms, provider_name=provider_name,
                model=provider_model, purpose=tele["purpose"],
                pipeline=tele["pipeline"], operation="SINGLE_CLASSIFY",
                kind="request", session_ids=session_ids,
                batch_size=0, attempt_number=0,
                fallback_depth=0, fallback_from=None, fallback_reason=None,
                usage=None, estimated_input_tokens=None,
                error=last_exc, success_payload=False,
                latency_ms=round((time.perf_counter() - request_started) * 1000, 1),
                context_window=getattr(self.provider, "model_context_tokens", None),
                max_output_tokens=None,
                quota_scope=quota_scope_for(provider_name),
                quota_before={}, quota_after={},
            )
        assert last_exc is not None
        raise last_exc

    def _classify_chunks(self, session: Any, items: List[Mapping[str, Any]],
                           tele: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        """Classify oversized evidence in chunks, then synthesize with Ollama.

        Each chunk gets its own provisional model classification; a final
        model call aggregates those provisionals into one session verdict.
        Chunk labels and the final label all come from the model.
        """
        chunks: List[List[Mapping[str, Any]]] = []
        current: List[Mapping[str, Any]] = []
        counter = getattr(self.provider, "count_tokens", None)
        budget = OLLAMA_EFFECTIVE_CONTEXT - RESERVED_OUTPUT_TOKENS - SAFETY_MARGIN_TOKENS
        for item in items:
            candidate = current + [item]
            candidate_prompt = self._chunk_prompt(session, candidate, 1, 1)
            fits = True
            if callable(counter):
                try:
                    fits = counter(candidate_prompt) <= budget
                except AttributeError:
                    pass
            if current and not fits:
                chunks.append(current)
                current = [item]
            elif fits:
                current = candidate
            else:
                raise ProviderError("single activity exceeds token budget")
        if current:
            chunks.append(current)
        provisionals: List[Dict[str, Any]] = []
        for chunk_index, chunk in enumerate(chunks):
            sub_prompt = self._chunk_prompt(session, chunk, chunk_index + 1, len(chunks))
            try:
                payload = dict(self._call_with_retry(session, sub_prompt, tele))
            except (ProviderError, OllamaError, OSError):
                continue
            if isinstance(payload, Mapping) and str(payload.get("category", "")).strip().lower() in _ALLOWED_CATEGORIES:
                item_duration = sum(float(item.get("duration_seconds", 0) or 0) for item in chunk)
                result = dict(payload)
                result["_duration_seconds"] = item_duration
                provisionals.append(result)
        if not provisionals:
            raise ProviderError("all evidence chunks failed")
        synthesis_prompt = (
            SYSTEM_PROMPT
            + "\nSYNTHESIS: the evidence was too large for one request, so each chunk was classified separately below. Produce the single final session classification in the same JSON schema, weighing the chunk results by their confidence and recency (later chunks are more recent). If the chunks disagree or are thin, return neutral with low confidence. Never invent evidence.\nCHUNK PROVISIONAL RESULTS:\n"
            + json.dumps(provisionals, ensure_ascii=False, sort_keys=True, default=str)
        )
        try:
            return self._call_with_retry(session, synthesis_prompt, tele)
        except (ProviderError, OllamaError, OSError):
            return self._aggregate_chunk_results(provisionals)

    @staticmethod
    def _aggregate_chunk_results(results: List[Mapping[str, Any]]) -> Dict[str, Any]:
        """Aggregate model outputs with duration as the contribution weight."""
        if not results:
            raise ProviderError("no chunk results to aggregate")
        weights = [max(0.0, float(item.get("_duration_seconds", 0) or 0)) for item in results]
        if not any(weights):
            weights = [1.0] * len(results)
        total = sum(weights)
        representative = dict(max(zip(results, weights), key=lambda pair: pair[1])[0])
        for field in ("category", "productivity"):
            scores: Dict[str, float] = {}
            for item, weight in zip(results, weights):
                value = str(item.get(field, "neutral")).strip().lower()
                scores[value] = scores.get(value, 0.0) + weight
            representative[field] = max(scores, key=scores.get)
        representative["confidence"] = sum(
            max(0.0, min(1.0, float(item.get("confidence", 0.0) or 0))) * weight
            for item, weight in zip(results, weights)
        ) / total
        representative.pop("_duration_seconds", None)
        return representative

    @staticmethod
    def _chunk_prompt(session: Any, chunk: List[Mapping[str, Any]], index: int, total: int) -> str:
        return (
            SYSTEM_PROMPT
            + "\nEVIDENCE CHUNK {}/{}: classify this chronological activity chunk. Return the same JSON schema.\nCHUNK PAYLOAD:\n".format(index, total)
            + json.dumps({
                "session_id": getattr(session, "id", None),
                "foreground_app": getattr(session, "app", None),
                "foreground_title": getattr(session, "title", None),
                "foreground_domain": getattr(session, "domain", None),
                "foreground_url": getattr(session, "url", None),
                "chunk": [dict(item) for item in chunk],
            }, ensure_ascii=False, sort_keys=True, default=str)
        )

    def classify(self, session: Any, recent_context: Optional[Iterable[Mapping[str, Any]]] = None,
                 telemetry: Optional[Mapping[str, Any]] = None) -> Classification:
        if not isinstance(session, ActivitySession) and not is_meaningful_session(session):
            raise TypeError("Classifier expects an ActivitySession or MeaningfulSession")
        cache_key = self._cache_key(session, recent_context)
        if cache_key in self._classification_cache:
            return self._classification_cache[cache_key]
        try:
            provider_name = getattr(self.provider, "name", "validation")
            tele = self._tele_context(telemetry, "SINGLE_CLASSIFY")
            if provider_name in {"ollama", "gemini", "hosted_chain"}:
                items = self._raw_evidence_items(recent_context)
                prompt = self.build_prompt(session, recent_context)
                counter = getattr(self.provider, "count_tokens", None)
                oversized = False
                if callable(counter):
                    try:
                        oversized = counter(prompt) > OLLAMA_EFFECTIVE_CONTEXT - RESERVED_OUTPUT_TOKENS - SAFETY_MARGIN_TOKENS
                    except AttributeError:
                        pass
                if items and (len(items) > MAX_RAW_CONTEXT_ITEMS or oversized):
                    payload = self._classify_chunks(session, items or [{}], tele)
                else:
                    payload = self._call_with_retry(session, prompt, tele)
            else:
                payload = self._call_with_retry(session, self.build_prompt(session, recent_context), tele)
            result_provider = getattr(self.provider, "last_provider", provider_name)
            result_model = getattr(self.provider, "last_model", getattr(self.provider, "model", None))
            result = self._from_payload(
                session,
                payload,
                result_provider,
                result_model,
            )
            if result.classification_status == "classified" and self._escalation_candidate(session, result):
                result = self._escalate_one(session, result, recent_context, tele)
            if result.classification_status == "classification_failed":
                self._last_failure_kind = "parse"
                return result
            self._classification_cache[cache_key] = result
            return result
        except (ProviderError, OllamaError, OSError) as exc:
            self._last_failure_kind = "timeout" if "timeout" in str(exc).casefold() else "provider"
            # Failures are deliberately NOT cached: a retryable session must
            # be re-attempted on a later cycle instead of serving a stale
            # failure forever. The scheduler gates retry frequency.
            return self._pending_result(session, error=str(exc))
        except (ValueError, TypeError) as exc:
            self._last_failure_kind = "parse"
            return Classification(
                session_id=session.id, category="neutral", subcategory="ambiguous",
                activity="Ambiguous activity", signal=_INVALID_SIGNAL,
                confidence=0.0, provider=getattr(self.provider, "name", "ollama"), model=getattr(self.provider, "model", None), source="pending",
                classification_status="classification_failed",
                classified_at=datetime.now(timezone.utc),
                last_error=str(exc)[:300] or _INVALID_SIGNAL,
                prompt_version=PROMPT_VERSION,
            )

    def _pending_result(self, session: Any, error: Optional[str] = None) -> Classification:
        return Classification(
            session_id=session.id,
            category="neutral",
            subcategory="pending",
            activity="Pending semantic analysis",
            signal=_OLLAMA_UNAVAILABLE_SIGNAL,
            confidence=0.0,
            provider=getattr(self.provider, "name", "ollama"),
            model=getattr(self.provider, "model", None),
            source="pending",
            classification_status="pending",
            classified_at=datetime.now(timezone.utc),
            last_error=(str(error)[:300] if error else _OLLAMA_UNAVAILABLE_SIGNAL),
            prompt_version=PROMPT_VERSION,
        )

    def classify_many(
        self,
        sessions: Iterable[Any],
        context_by_session: Optional[Mapping[str, Iterable[Mapping[str, Any]]]] = None,
        telemetry: Optional[Mapping[str, Any]] = None,
        sealed_context_ids: Optional[Iterable[str]] = None,
    ) -> List[Classification]:
        original = list(sessions)
        started = datetime.now(timezone.utc)
        started_perf = time.perf_counter()
        tele = self._tele_context(telemetry, "BATCH")
        provider_name = str(getattr(self.provider, "name", "ollama"))
        provider_model = getattr(self.provider, "model", None)
        successful_calls = failed_calls = timeout_count = parse_failures = 0
        attempted_calls = 0
        ordered = sorted(original, key=lambda item: getattr(item, "start", getattr(item, "start_time", datetime.min)))
        results = {}
        context_by_session = context_by_session or {}
        # Sealed items arrive with complete explicit context (e.g. episode
        # members carrying their group envelope plus siblings). Skipping the
        # automatic previous-items append keeps group boundaries exact:
        # temporal neighbors from other groups must not corroborate.
        sealed = {str(item) for item in (sealed_context_ids or ())}
        session_contexts = {}
        for index, session in enumerate(ordered):
            context = list(context_by_session.get(session.id, []))
            if str(session.id) not in sealed:
                for previous in ordered[max(0, index - 4):index]:
                    context.append({
                        "application": getattr(previous, "app", None),
                        "window_title": getattr(previous, "title", None),
                        "domain": getattr(previous, "domain", None),
                        "duration_seconds": getattr(previous, "duration", 0),
                    })
            session_contexts[session.id] = context
        # Batch path: one shared instruction header plus one evidence object
        # per uncached session. Providers without batch support (local
        # Ollama, test doubles) transparently use the single-call loop.
        batch_payloads: Dict[str, Any] = {}
        batch_attribution: Dict[str, Any] = {}
        batcher = getattr(self.provider, "classify_batch", None)
        uncached = [session for session in ordered
                    if self._cache_key(session, session_contexts[session.id]) not in self._classification_cache]
        if callable(batcher) and len(uncached) > 1:
            try:
                entries = [{
                    "session": session,
                    "evidence": self.build_evidence(session, session_contexts[session.id]),
                    "prompt": self.build_prompt(session, session_contexts[session.id])}
                    for session in uncached
                ]
                if isinstance(self.provider, ProviderChain):
                    batch_payloads = dict(batcher(entries, tele))
                else:
                    batch_payloads = dict(batcher(entries) or {})
                batch_attribution = dict(getattr(self.provider, "last_batch", {}) or {})
            except (ProviderError, OllamaError, OSError, ValueError, TypeError):
                batch_payloads = {}
                batch_attribution = {}
        for session in ordered:
            self._last_failure_kind = None
            self._last_call_attempted = False
            context = session_contexts[session.id]
            cache_key = self._cache_key(session, context)
            if cache_key in self._classification_cache:
                result = self._classification_cache[cache_key]
            elif session.id in batch_payloads:
                self._last_call_attempted = True
                attributed = batch_attribution.get(session.id, (None, None))
                result = self._from_payload(
                    session,
                    batch_payloads[session.id],
                    attributed[0] or getattr(self.provider, "last_provider",
                                             getattr(self.provider, "name", "validation")),
                    attributed[1] if len(attributed) > 1 else getattr(self.provider, "last_model",
                                             getattr(self.provider, "model", None)),
                )
                if result.classification_status == "classification_failed":
                    self._last_failure_kind = "parse"
                else:
                    self._classification_cache[cache_key] = result
            else:
                result = self.classify(session, context, tele)
            results[session.id] = result
            attempted_calls += int(self._last_call_attempted)
            if result.classification_status == "classified":
                successful_calls += 1
            elif result.classification_status == "classification_failed":
                failed_calls += 1
                if self._last_failure_kind == "timeout":
                    timeout_count += 1
                if self._last_failure_kind == "parse":
                    parse_failures += 1
            elif result.source == "pending" and self._last_failure_kind:
                failed_calls += 1
                if self._last_failure_kind == "timeout":
                    timeout_count += 1
                if self._last_failure_kind == "parse":
                    parse_failures += 1
        completed = datetime.now(timezone.utc)
        # Selective escalation pass: low-confidence weak-evidence verdicts
        # get one bounded call on the escalation model under explicit
        # criteria only. Adopted verdicts replace the workhorse result;
        # call counts above already reflect the workhorse attempts.
        for session in ordered:
            current = results.get(session.id)
            if current is not None and self._escalation_candidate(session, current):
                results[session.id] = self._escalate_one(
                    session, current, session_contexts.get(session.id), tele)
        if not original:
            status = "success"
        elif failed_calls == 0 and (successful_calls > 0 or not attempted_calls):
            status = "success"
        elif successful_calls:
            status = "partial"
        else:
            status = "failed"
        telemetry = ClassificationJobTelemetry(
            job_id=uuid.uuid4().hex,
            started_at=started,
            completed_at=completed,
            provider=provider_name,
            model=provider_model,
            input_session_count=len(original),
            successful_calls=successful_calls,
            failed_calls=failed_calls,
            timeout_count=timeout_count,
            parse_failures=parse_failures,
            latency_ms=round((time.perf_counter() - started_perf) * 1000, 1),
            status=status,
        )
        with self._job_lock:
            self._job_history.append(telemetry)
            self._job_history = self._job_history[-200:]
        # Observability rollup: one operation record per job with session
        # counts (tokens live on the underlying invocations, never here, so
        # nothing double-counts). Shares the batch request_id.
        self._emit_operation(
            "classify_job", provider_name,
            duration_ms=round((time.perf_counter() - started_perf) * 1000, 1),
            success=status != "failed",
            metadata={
                "request_id": tele["request_id"],
                "sessions": len(original),
                "successful": successful_calls,
                "failed": failed_calls,
                "batch_sessions": len(batch_payloads),
                "attempted_calls": attempted_calls,
            })
        # Reuse the daemon logger so this record reaches the existing console
        # and rotating daemon.log handlers instead of disappearing into an
        # unconfigured child logger.
        logging.getLogger("noema.runtime").info(
            "[OLLAMA] %s", json.dumps(telemetry.to_dict(), ensure_ascii=False)
        )
        return [results[session.id] for session in original]

    def classification_availability(self) -> Dict[str, Any]:
        """Explicit availability of the classification tier.

        ``classification_unavailable`` means the last terminal attempt
        exhausted every tier model. It is a routing state for operators
        and UI — never a semantic verdict, never a substitute category.
        Sessions keep their honest pending/failed rows regardless.
        """
        provider = getattr(self, "provider", None)
        availability = getattr(provider, "availability", None)
        if callable(availability):
            try:
                return dict(availability())
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        return {"status": "unknown", "failure_kind": None,
                "tiers": [], "models": []}

    def last_job_telemetry(self) -> Optional[Dict[str, Any]]:
        with self._job_lock:
            return self._job_history[-1].to_dict() if self._job_history else None

    def debug_telemetry(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._job_lock:
            jobs = [item.to_dict() for item in self._job_history if (now - item.completed_at).total_seconds() <= 86400]
        provider_metrics = self.provider.telemetry() if hasattr(self.provider, "telemetry") else {}
        ollama_jobs = sum(1 for item in jobs if item["provider"] == "ollama")
        return {
            "configured_model": getattr(self.provider, "model", None),
            "last_job": jobs[-1] if jobs else None,
            "last_success": next((item for item in reversed(jobs) if item["successful_calls"] > 0), None),
            "last_failure": next((item for item in reversed(jobs) if item["failed_calls"] or item["timeout_count"] or item["parse_failures"]), None),
            "last_latency_ms": jobs[-1]["latency_ms"] if jobs else provider_metrics.get("last_latency_ms"),
            "jobs_last_24h": len(jobs),
            "ollama_jobs": ollama_jobs,
            "parse_failures": sum(item["parse_failures"] for item in jobs),
            "provider_metrics": provider_metrics,
            "jobs": jobs,
        }
