"""Fast-model verification for distraction candidates.

The fast model is NOT a classifier: it never defines productive /
distractive / neutral. Its only job is to answer, given an
already-understood behavioral pattern (a compact feature summary, never
raw history):

    "Is this concerning enough to justify an intervention right now?"

Separate fast provider path: fast OpenRouter model → fast Gemini
fallback → Ollama when configured. Never the general ``openrouter/free``
router, never a second provider framework: all three legs reuse the
existing provider abstraction plus the chain's quota ledger.

Strict contract:

- input: compact feature summary + candidate score + top reasons only.
- output: exactly ``{concerning, severity 1-5, confidence, reason,
  recommended_intervention}``. Every field validated; malformed output
  fails safe to not-concerning (no intervention on garbage).
- AFK or unknown presence: no model call at all.
- 429 / 5xx / timeout / malformed / unavailable: next leg, then an
  explicit not-concerning result. Never raises for provider reasons.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

VERIFIER_VERSION = "1"

RECOMMENDED_INTERVENTIONS = (
    "meme",
    "goal_callback",
    "reframe",
    "micro_task",
    "none",
)

# Maps the verifier's recommendation onto the existing V1 intervention
# modes (HOLDOUT is recorded but never delivered, by engine design).
RECOMMENDATION_TO_MODE = {
    "meme": "MEME",
    "goal_callback": "NOTIFICATION",
    "reframe": "NOTIFICATION",
    "micro_task": "NOTIFICATION",
    "none": "HOLDOUT",
}

VERIFY_INSTRUCTIONS = (
    "You are a focus guardian. Given the behavioral pattern below, decide "
    "whether it is concerning enough to justify an intervention right now. "
    "Reply with a SINGLE JSON object and nothing else: "
    '{"concerning":true or false,"severity":1-5,'
    '"confidence":0.0-1.0,"reason":"under 20 words",'
    '"recommended_intervention":"meme|goal_callback|reframe|micro_task|none"}. '
    "No chain-of-thought. No prose."
)


@dataclass(frozen=True)
class VerificationConfig:
    """Verifier knobs. All changes are code-free config."""

    timeout_seconds: float = 20.0
    max_output_tokens: int = 300

    def __post_init__(self) -> None:
        if float(self.timeout_seconds) <= 0:
            raise ValueError("timeout_seconds must be positive")
        if int(self.max_output_tokens) < 1:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of one verification attempt (model call or skip)."""

    concerning: bool
    severity: int
    confidence: float
    reason: str
    recommended_intervention: str
    provider: Optional[str]
    model: Optional[str]
    latency_ms: Optional[float]
    model_calls: int
    attempts: Tuple[Tuple[str, Optional[str]], ...]
    skipped: bool
    verifier_version: str = VERIFIER_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concerning": self.concerning,
            "severity": self.severity,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "recommended_intervention": self.recommended_intervention,
            "intervention_mode": RECOMMENDATION_TO_MODE.get(
                self.recommended_intervention, "HOLDOUT"
            ),
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "model_calls": self.model_calls,
            "attempts": [
                {"provider": name, "model": model} for name, model in self.attempts
            ],
            "skipped": self.skipped,
            "verifier_version": self.verifier_version,
        }


def _validate_payload(payload: Any) -> Optional[Dict[str, Any]]:
    """Return a normalized verdict or None when malformed."""
    if not isinstance(payload, Mapping):
        return None
    concerning = payload.get("concerning")
    if isinstance(concerning, str):
        lowered = concerning.strip().lower()
        if lowered in {"true", "yes", "1"}:
            concerning = True
        elif lowered in {"false", "no", "0"}:
            concerning = False
    if isinstance(concerning, int) and not isinstance(concerning, bool):
        if concerning in (0, 1):
            concerning = bool(concerning)
    if not isinstance(concerning, bool):
        return None
    try:
        severity = int(payload.get("severity", 0))
    except (TypeError, ValueError):
        return None
    if severity not in (1, 2, 3, 4, 5):
        return None
    try:
        confidence = float(payload.get("confidence", -1.0))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    reason = payload.get("reason", "")
    if not isinstance(reason, str) or not reason.strip():
        return None
    recommendation = str(payload.get("recommended_intervention", "")).strip()
    if recommendation not in RECOMMENDED_INTERVENTIONS:
        return None
    return {
        "concerning": concerning,
        "severity": severity,
        "confidence": confidence,
        "reason": reason.strip()[:280],
        "recommended_intervention": recommendation,
    }


def build_verify_prompt(summary: Mapping[str, Any], candidate_score: float,
                        reasons: List[str]) -> str:
    """Assemble the compact verification prompt (feature summary only)."""
    body = {
        "pattern": dict(summary),
        "candidate_score": round(float(candidate_score), 3),
        "top_signals": list(reasons)[:3],
    }
    return VERIFY_INSTRUCTIONS + "\nPATTERN:\n" + json.dumps(
        body, ensure_ascii=False, sort_keys=True, default=str)


class FastModelVerifier:
    """Verify candidates through the separate fast provider path."""

    def __init__(self, chain: Any = None,
                 fast_openrouter: Any = None,
                 fast_gemini: Any = None,
                 config: Optional[VerificationConfig] = None,
                 observer: Any = None):
        self.chain = chain
        self.fast_openrouter = fast_openrouter
        self.fast_gemini = fast_gemini
        self.config = config or VerificationConfig()
        self.attempts: List[Tuple[str, Optional[str]]] = []
        # Explicit telemetry hook; falls back to the chain's observer so
        # daemon wiring (chain carries it) needs no extra plumbing.
        self._observer = observer

    @property
    def _ollama(self) -> Any:
        return getattr(self.chain, "ollama", None) if self.chain is not None else None

    def _legs(self) -> List[Any]:
        legs = []
        if self.fast_openrouter is not None:
            legs.append(self.fast_openrouter)
        if self.fast_gemini is not None:
            legs.append(self.fast_gemini)
        ollama = self._ollama
        include = bool(getattr(self.chain, "include_ollama", False)) if self.chain else False
        if include and ollama is not None:
            legs.append(ollama)
        return legs

    def _quota_state(self, provider: Any) -> Any:
        states = getattr(self.chain, "rate_limits", None) if self.chain else None
        if not isinstance(states, dict):
            return None
        return states.get(getattr(provider, "model", None))

    def _quota_snapshot(self, provider: Any) -> Dict[str, int]:
        state = self._quota_state(provider)
        if state is None:
            return {}
        try:
            return {
                "requests_today": int(getattr(state, "requests_today", 0) or 0),
                "tokens_this_minute": int(getattr(state, "tokens_this_minute", 0) or 0),
            }
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _now_pair() -> tuple:
        """(iso_timestamp, epoch_ms) from one clock read."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        return (now.isoformat().replace("+00:00", "Z"), int(now.timestamp() * 1000))

    @staticmethod
    def _context_window(model: Any) -> Optional[int]:
        try:
            from noema.infrastructure.providers import MODEL_CONTEXT_WINDOWS

            window = MODEL_CONTEXT_WINDOWS.get(str(model))
            return int(window) if window else None
        except (TypeError, ValueError):
            return None

    def _usable(self, provider: Any, prompt: str) -> int:
        """Billable tokens if a call may proceed, else -1."""
        if self.chain is None or getattr(provider, "name", "") == "ollama":
            return 600
        usable = getattr(self.chain, "_model_usable", None)
        if not callable(usable):
            return 600
        try:
            return int(usable(provider, prompt))
        except Exception:
            return -1

    def _call(self, provider: Any, prompt: str) -> Mapping[str, Any]:
        name = str(getattr(provider, "name", ""))
        if name == "ollama":
            return provider.classify(None, prompt)
        complete = getattr(provider, "complete_json", None)
        if not callable(complete):
            from noema.infrastructure.providers import ProviderError

            raise ProviderError("provider has no JSON completion path")
        return complete(prompt, self.config.max_output_tokens)

    @property
    def observer(self) -> Any:
        """Telemetry recorder: explicit hook first, chain's hook second."""
        if self._observer is not None:
            return self._observer
        return getattr(self.chain, "observer", None) if self.chain is not None else None

    def _emit_invocation(self, **fields: Any) -> bool:
        observer = self.observer
        if observer is None:
            return False
        try:
            from noema.observability.provider_telemetry import assemble_invocation

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

    def verify(self, summary: Mapping[str, Any], candidate_score: float,
               reasons: Optional[List[str]] = None,
               presence_state: str = "unknown",
               telemetry: Optional[Mapping[str, Any]] = None) -> VerificationResult:
        """Verify one candidate pattern. Never raises for provider reasons."""
        from noema.observability.models import (
            Pipeline,
            Purpose,
            new_request_id,
            utcnow_iso,
        )
        from noema.observability.provider_telemetry import (
            quota_scope_for,
            take_usage,
        )

        tele = dict(telemetry or {})
        tele.setdefault("purpose", Purpose.FAST_DISTRACTION)
        tele.setdefault("pipeline", Pipeline.REALTIME_DETECTION)
        tele["operation"] = "VERIFY"
        if not tele.get("request_id"):
            tele["request_id"] = new_request_id()
        detection_id = tele.get("detection_id")
        request_started = time.perf_counter()
        state = str(presence_state or "unknown").strip().lower()
        if state == "afk":
            return VerificationResult(
                concerning=False, severity=1, confidence=0.0,
                reason="presence is afk; no verification without an active user",
                recommended_intervention="none", provider=None, model=None,
                latency_ms=None, model_calls=0, attempts=(), skipped=True,
            )
        if not summary:
            return VerificationResult(
                concerning=False, severity=1, confidence=0.0,
                reason="empty feature summary; nothing to verify",
                recommended_intervention="none", provider=None, model=None,
                latency_ms=None, model_calls=0, attempts=(), skipped=True,
            )
        prompt = build_verify_prompt(summary, candidate_score, reasons or [])
        legs = self._legs()
        if not legs:
            return VerificationResult(
                concerning=False, severity=1, confidence=0.0,
                reason="no fast verification provider is configured",
                recommended_intervention="none", provider=None, model=None,
                latency_ms=None, model_calls=0, attempts=(), skipped=True,
            )
        self.attempts = []
        previous: Optional[tuple] = None
        for provider in legs:
            name = str(getattr(provider, "name", ""))
            model = getattr(provider, "model", None)
            self.attempts.append((name, model))
            billable = self._usable(provider, prompt)
            if billable < 0:
                self._emit_operation(
                    "quota_skip", str(model),
                    metadata={"request_id": tele["request_id"], "provider": name,
                              "billable_tokens": billable, "operation": "VERIFY"})
                continue
            quota_before = self._quota_snapshot(provider)
            started = time.perf_counter()
            try:
                payload = self._call(provider, prompt)
                call_error = None
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
            except Exception as exc:
                call_error = exc
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                payload = {}
                state_obj = self._quota_state(provider)
                if state_obj is not None:
                    try:
                        state_obj.record_failure(exc)
                    except Exception:
                        pass
            stamp_iso, stamp_ms = self._now_pair()
            self._emit_invocation(
                request_id=tele["request_id"], timestamp_iso=stamp_iso,
                timestamp_ms=stamp_ms, provider_name=name,
                model=model, purpose=tele["purpose"],
                pipeline=tele["pipeline"], operation="VERIFY",
                kind="attempt", session_ids=[],
                batch_size=0, attempt_number=0,
                fallback_depth=max(0, len(self.attempts) - 1),
                fallback_from=previous[0] if previous else None,
                fallback_reason=str(previous[1])[:300] if previous else None,
                usage=take_usage(provider) if call_error is None else None,
                estimated_input_tokens=billable,
                error=call_error,
                success_payload=(call_error is None and _validate_payload(payload) is not None),
                latency_ms=latency_ms,
                context_window=self._context_window(model),
                max_output_tokens=self.config.max_output_tokens,
                quota_scope=quota_scope_for(name),
                quota_before=quota_before,
                quota_after=self._quota_snapshot(provider),
                detection_id=detection_id,
            )
            if call_error is not None:
                previous = (name, call_error)
                continue
            verdict = _validate_payload(payload)
            if verdict is None:
                state_obj = self._quota_state(provider)
                if state_obj is not None:
                    try:
                        from noema.infrastructure.providers import ProviderError

                        state_obj.record_failure(
                            ProviderError("fast verification returned malformed output"))
                    except Exception:
                        pass
                previous = (name, "malformed verification output")
                continue
            state_obj = self._quota_state(provider)
            ledger = getattr(self.chain, "ledger", None) if self.chain else None
            if state_obj is not None:
                try:
                    state_obj.record_success(billable)
                except Exception:
                    pass
                if ledger is not None:
                    try:
                        ledger.consume("llm", model, billable)
                    except Exception:
                        pass
            stamp_iso, stamp_ms = self._now_pair()
            self._emit_invocation(
                request_id=tele["request_id"], timestamp_iso=stamp_iso,
                timestamp_ms=stamp_ms, provider_name=name,
                model=model, purpose=tele["purpose"],
                pipeline=tele["pipeline"], operation="VERIFY",
                kind="request", session_ids=[],
                batch_size=0, attempt_number=0,
                fallback_depth=max(0, len(self.attempts) - 1),
                fallback_from=previous[0] if previous else None,
                fallback_reason=str(previous[1])[:300] if previous else None,
                usage=None, estimated_input_tokens=None,
                error=None, success_payload=True,
                latency_ms=round((time.perf_counter() - request_started) * 1000, 1),
                context_window=self._context_window(model),
                max_output_tokens=self.config.max_output_tokens,
                quota_scope=quota_scope_for(name),
                quota_before=quota_before,
                quota_after=self._quota_snapshot(provider),
                detection_id=detection_id,
            )
            # The model verifies concern; it never writes classifications.
            return VerificationResult(
                concerning=verdict["concerning"], severity=verdict["severity"],
                confidence=verdict["confidence"], reason=verdict["reason"],
                recommended_intervention=verdict["recommended_intervention"],
                provider=name, model=model, latency_ms=latency_ms,
                model_calls=1, attempts=tuple(self.attempts), skipped=False,
            )
        stamp_iso, stamp_ms = self._now_pair()
        self._emit_invocation(
            request_id=tele["request_id"], timestamp_iso=stamp_iso,
            timestamp_ms=stamp_ms, provider_name="fast_path",
            model=None, purpose=tele["purpose"],
            pipeline=tele["pipeline"], operation="VERIFY",
            kind="request", session_ids=[],
            batch_size=0, attempt_number=0,
            fallback_depth=max(0, len(self.attempts)),
            fallback_from=previous[0] if previous else None,
            fallback_reason=str(previous[1])[:300] if previous else "all legs failed or throttled",
            usage=None, estimated_input_tokens=None,
            error=previous[1] if isinstance(previous, tuple) and isinstance(previous[1], BaseException) else None,
            success_payload=False,
            latency_ms=round((time.perf_counter() - request_started) * 1000, 1),
            context_window=None, max_output_tokens=self.config.max_output_tokens,
            quota_scope=None, quota_before={}, quota_after={},
            detection_id=detection_id,
        )
        return VerificationResult(
            concerning=False, severity=1, confidence=0.0,
            reason="all fast verification legs failed or were throttled; no confirmation",
            recommended_intervention="none", provider=None, model=None,
            latency_ms=None, model_calls=len(self.attempts),
            attempts=tuple(self.attempts), skipped=False,
        )
