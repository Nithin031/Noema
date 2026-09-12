"""Provider boundary for semantic classification.

The classifier consumes one small provider contract.  Provider-specific
transport, authentication, and response extraction stay in this module so
the rest of Noema only sees ``Classification`` results.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - very old interpreters
    ZoneInfo = None  # type: ignore[assignment]
from typing import Any, Dict, List, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from noema.infrastructure.ollama import OllamaClient
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


class ProviderError(ConnectionError):
    """A provider could not return a usable classification payload."""


logger = logging.getLogger("noema.providers")


def _looks_like_rate_limited(error: BaseException) -> bool:
    """True when a provider error is a quota/rate-limit refusal.

    Deliberately narrower than quota-error detection: only an explicit 429
    (or RESOURCE_EXHAUSTED) counts, because only provider refusals feed the
    429 counter. Local budget skips and context overflows are not 429s.
    """
    text = str(error)
    return "429" in text or "resource_exhausted" in text.casefold()


def _http_status(error: BaseException) -> Optional[int]:
    """Extract an HTTP status code from a provider error message, if present."""
    match = re.search(r"HTTP (\d{3})", str(error))
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None
    return None


def _looks_like_format_error(error: BaseException) -> bool:
    """True when a 400 plausibly rejects structured-output parameters.

    Several OpenRouter free models (notably the Gemma family) answer
    ``response_format: {type: json_object}`` with HTTP 400 even though the
    model ID itself is valid. The prompt already instructs JSON-only
    output, so retrying the identical request without ``response_format``
    is safe and preserves the classification contract.
    """
    if _http_status(error) != 400:
        return False
    text = str(error).casefold()
    return any(token in text for token in (
        "response_format", "response format", "json_object", "json mode",
        "structured output", "structured_output"))


# Canonical OpenRouter free-model registry (single source of truth).
#
# Note: Free models from Google Gemma and NVIDIA Nemotron have been removed
# from this registry due to reliability issues (404s, rate limit instability,
# inconsistent classification quality). For reliable Google-only classification,
# use the HOSTED_MODELS list which targets Gemini models directly.
OPENROUTER_MODEL_REGISTRY = (
    {
        "id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "display_name": "Nemotron 3 Nano Omni 30B Reasoning (free)",
        "vendor": "nvidia",
        "provider": "openrouter",
        "role": "fast",
        "purpose": "real-time distraction advisory and first batch attempt",
        "capabilities": ("reasoning", "text"),
        "context_window": 256000,
        "max_output": 65536,
        "normal_classification": True,
        "fast_path": True,
        "enabled": True,
        "priority": 1,
        "limits": (10, 100000, 50),
        "excludes": ("embeddings", "media_playback_detection"),
    },
)

# The fast distraction advisory path always uses the highest-priority
# enabled fast-path model in the registry: the smallest reasoning model,
# so a live check costs one cheap call. Derived, never hardcoded twice.
FAST_DISTRACTION_MODEL = next(
    entry["id"]
    for entry in sorted(OPENROUTER_MODEL_REGISTRY, key=lambda item: item["priority"])
    if entry.get("fast_path") and entry.get("enabled")
)

# Semantic routing roles. The workhorse serves every eligible episode; the
# escalation model serves only explicit escalation calls (episodes meeting
# the deterministic ambiguity criteria, or user-requested reevaluation).
# Never route unconditionally to a 20-RPD model.
WORKHORSE_MODEL = "gemini-3.5-flash-lite"
ESCALATION_MODEL = "gemini-3.5-flash"
# Max selective-escalation calls per quota day. Bounded so escalation can
# never eat the fallback reserve on the 20-RPD escalation model.
ESCALATION_MAX_PER_DAY = 10
# Escalate a classified verdict only below this confidence...
ESCALATION_MAX_CONFIDENCE = 0.55
# ...and only for episodes with at least this much active time.
ESCALATION_MIN_DURATION_SECONDS = 120.0
# Google RPD accounting resets on Pacific time; the local ledger keeps the
# product calendar day instead. Both are reported explicitly so the two are
# never silently mixed.
PROVIDER_QUOTA_RESET_TZ = "America/Los_Angeles"


def openrouter_registry() -> List[Dict[str, Any]]:
    """Return a copy of the canonical registry for quotas/telemetry output."""
    return [dict(entry) for entry in OPENROUTER_MODEL_REGISTRY]


MODEL_LIMITS = {
    # Hard API quotas per hosted model: (requests/minute, tokens/minute,
    # requests/day). Source: the provider limit table supplied by the
    # operator. Unknown hosted models fall back to the most conservative
    # row so a new model name can never silently exceed its quota. Local
    # Ollama has no quota and is always the final fallback.
    "gemma-4-31b-it": (30, 16000, 14400),
    "gemma-4-26b-a4b-it": (30, 16000, 14400),
    "gemini-3.1-flash-lite": (15, 250000, 500),
    "gemini-3.5-flash-lite": (15, 250000, 500),
    "gemini-2.5-flash": (5, 250000, 20),
    "gemini-3-flash": (5, 250000, 20),
    "gemini-3.5-flash": (5, 250000, 20),
    "gemini-3.6-flash": (5, 250000, 20),
    "gemini-3.7-flash": (5, 250000, 20),
    "gemini-3.8-flash": (5, 250000, 20),
    "gemini-embedding-001": (100, 30000, 1000),
    "gemini-embedding-002": (100, 30000, 1000),
    # OpenRouter free tier: conservative guards (free accounts are heavily
    # rate-limited). Live 429s are still handled with cooldown + fallback,
    # so these bounds can only ever under-use, never over-use.
    **{entry["id"]: entry["limits"] for entry in OPENROUTER_MODEL_REGISTRY},
}
DEFAULT_HOSTED_LIMITS = (5, 250000, 20)

# Input context windows (tokens) per hosted model. A batch request can never
# usefully exceed min(window, per-minute tokens); the packing budget below
# enforces exactly that so the full window is exploited but never crossed.
MODEL_CONTEXT_WINDOWS = {
    "gemini-2.5-flash": 1048576,
    "gemini-3.5-flash-lite": 1048576,
    "gemini-3.1-flash-lite": 1048576,
    "gemini-3.6-flash": 1048576,
    "gemini-3.7-flash": 1048576,
    "gemini-3-flash": 1048576,
    "gemini-3.5-flash": 1048576,
    "gemini-3.8-flash": 1048576,
    "gemma-4-31b-it": 131072,
    "gemma-4-26b-a4b-it": 131072,
    **{entry["id"]: entry["context_window"] for entry in OPENROUTER_MODEL_REGISTRY},
}
DEFAULT_CONTEXT_WINDOW = 32768
BATCH_SAFETY_MARGIN = 4000

# Output-token reserve added to every pre-call quota check. Classification
# answers are capped at ~600 tokens, so a request is only attempted when
# prompt + reserve fits the per-minute token budget.
OUTPUT_TOKEN_RESERVE = 600

# Batch-classification budget. Many sessions ride in ONE request; groups are
# packed by measured text length so input + output reserve stays around
# 200K tokens — deliberately below the 250K TPM ceiling. Never a fixed
# session count: long evidence means smaller groups and vice versa.
BATCH_TOKEN_BUDGET = 200000
BATCH_OUTPUT_RESERVE_PER_ITEM = 500
BATCH_MAX_OUTPUT_TOKENS = 65536
BATCH_INSTRUCTIONS = (
    "BATCH REQUEST: classify EACH of the following {count} desktop activity sessions independently, "
    "in the given order. Sessions are independent observations; never merge them into one verdict. "
    "Reason per session in this order: (1) application/container, (2) actual session title, "
    "(3) special application policy, (4) duration and continuity, (5) neighboring activity, "
    "(6) semantic activity, (7) evidence quality, (8) productive/distractive/neutral, "
    "(9) alignment only if a goal exists. Never start from 'is this productive?'. "
    "POLICY: browsers (firefox, chrome, edge, ...) are NEUTRAL containers — inspect the title, "
    "never judge the executable; a bare 'Mozilla Firefox'/'New Tab' is insufficiently specified: "
    "neutral, low evidence. YouTube: inspect the title — technical/learning titles indicate learning, "
    "entertainment titles indicate leisure; never invent video content. WhatsApp: inspect "
    "title/duration/context — a genuinely continuous episode over 10 minutes is strong distractive "
    "evidence, short checks are not automatically distractive. Instagram, Netflix, Reddit, TikTok: "
    "distractive by user policy, still citing observed telemetry. Games (Rocket League, Chess, "
    "Valorant, Counter-Strike, GTA, Minecraft, Fortnite, Apex, League of Legends, Dota, Overwatch, "
    "...): actual gameplay is distractive, but technical work about games ('Rocket League API "
    "documentation', 'Chess engine implementation') is not gameplay — inspect the activity. "
    "Never invent domains, URLs, search queries, video content, goals, or intents. "
    "Return a single JSON array with exactly {count} objects in the SAME order as the input. "
    "Each object must copy its input \"session_id\" exactly and follow this schema: "
    "{{\"session_id\":\"...\",\"category\":\"productive | distractive | neutral\","
    "\"subcategory\":\"short_snake_case\",\"activity\":\"short description\","
    "\"signal\":\"why, citing observed evidence\",\"topic\":\"short topic or null\","
    "\"project\":\"specific project or null\",\"service\":\"website or app service\","
    "\"intent_signal\":\"what the user appears to be trying to do\","
    "\"activity_type\":\"coding | research | technical_research | studying | reading | writing | watching | "
    "browsing | sports_browsing | gaming | messaging | email | meeting | shopping | engineering | data_analysis | "
    "administration\",\"productivity\":\"productive | neutral | distracting\",\"confidence\":0.0,"
    "\"evidence_quality\":\"strong | moderate | weak | absent\"}}. "
    "If one session's evidence is thin (weak/absent), give THAT object category neutral with "
    "confidence 0.0-0.59; never drop it, never invent evidence. Never label thin evidence as "
    "distractive. Neutral is not distraction and does not mean failure. "
    "Output ONLY the JSON array."
)

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.IGNORECASE | re.DOTALL)


class QuotaLedger:
    """Persistent per-day usage ledger for hosted API calls.

    Tracks ``(kind, model)`` pairs — ``kind`` is ``"llm"`` or
    ``"embedding"`` — counting requests and tokens against the current
    quota day in ``day_timezone`` (default Asia/Kolkata, matching the
    product timezone, so counters refresh at the user's 12am). State is
    kept in a JSON file (atomically replaced on write) so a daemon restart
    cannot lose the daily counters and accidentally cross RPD. With
    ``path=None`` the ledger is memory-only (tests, throwaway processes).
    All methods are thread-safe.
    """

    def __init__(self, path: Optional[Any] = None, day_timezone: str = "Asia/Kolkata"):
        self.path = str(Path(path).expanduser()) if path is not None else None
        try:
            self._zone = ZoneInfo(day_timezone) if ZoneInfo is not None else timezone.utc
        except Exception:
            self._zone = timezone.utc
        self.day_timezone = str(day_timezone)
        self._lock = threading.RLock()
        self._day = ""
        self._counts: Dict[str, Dict[str, int]] = {}
        self._load()

    def _today(self) -> str:
        return datetime.now(self._zone).date().isoformat()

    @property
    def day(self) -> str:
        """Current quota day; rolls over on next access after midnight."""
        with self._lock:
            self._rollover_locked()
            return self._day

    def _blank(self) -> None:
        self._day = self._today()
        self._counts = {}

    def _load(self) -> None:
        with self._lock:
            self._blank()
            if not self.path:
                return
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError):
                return
            if not isinstance(payload, dict):
                return
            day = payload.get("day")
            counts = payload.get("counts")
            if not isinstance(day, str) or not isinstance(counts, dict):
                return
            if day != self._today():
                return  # Stale day: start fresh, do not carry counts over.
            cleaned: Dict[str, Dict[str, int]] = {}
            for key, value in counts.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    continue
                try:
                    cleaned[key] = {
                        "requests": max(0, int(value.get("requests", 0))),
                        "tokens": max(0, int(value.get("tokens", 0))),
                    }
                except (TypeError, ValueError):
                    continue
            self._day = day
            self._counts = cleaned

    def _save_locked(self) -> None:
        if not self.path:
            return
        try:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump({"day": self._day, "counts": self._counts}, handle)
            os.replace(tmp_path, self.path)
        except OSError:
            pass  # Ledger persistence is best-effort; memory still guards.

    def _rollover_locked(self) -> None:
        if self._day != self._today():
            self._blank()
            self._save_locked()

    @staticmethod
    def _key(kind: str, model: Any) -> str:
        return "{}:{}".format(kind, model)

    def usage(self, kind: str, model: Any) -> Dict[str, int]:
        """Return today's ``{"requests": n, "tokens": m}`` for one entry."""
        with self._lock:
            self._rollover_locked()
            entry = self._counts.get(self._key(kind, model), {})
            return {"requests": int(entry.get("requests", 0)), "tokens": int(entry.get("tokens", 0))}

    def check(self, kind: str, model: Any, tokens: int, rpd_limit: int) -> bool:
        """True if one more call of ``tokens`` tokens stays within RPD."""
        with self._lock:
            self._rollover_locked()
            entry = self._counts.get(self._key(kind, model))
            used = int(entry.get("requests", 0)) if entry else 0
            return used < rpd_limit

    def consume(self, kind: str, model: Any, tokens: int, requests: int = 1) -> Dict[str, int]:
        """Record usage and persist. Always records; callers check first."""
        with self._lock:
            self._rollover_locked()
            key = self._key(kind, model)
            entry = self._counts.get(key, {"requests": 0, "tokens": 0})
            entry = {
                "requests": int(entry.get("requests", 0)) + max(0, requests),
                "tokens": int(entry.get("tokens", 0)) + max(0, int(tokens)),
            }
            self._counts[key] = entry
            self._save_locked()
            return dict(entry)

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        """Today's full usage table for health/telemetry output."""
        with self._lock:
            self._rollover_locked()
            return {key: dict(value) for key, value in sorted(self._counts.items())}


@dataclass
class QuotaState:
    provider: str
    model: str
    rpm_limit: int = 60
    tpm_limit: int = 100000
    rpd_limit: int = 1000
    requests_this_minute: int = 0
    tokens_this_minute: int = 0
    requests_today: int = 0
    # Provider attempts are tracked separately from successful logical
    # classifications: one HTTP/model request is one attempt, whether or
    # not it produced a usable verdict. RPM guards run on attempts.
    attempts_this_minute: int = 0
    attempts_today: int = 0
    # 429s are tracked separately from generic failures so quota
    # exhaustion is visible as its own signal.
    rate_limited_count: int = 0
    last_rate_limited_at: Optional[str] = None
    minute_window_start: float = field(default_factory=time.monotonic)
    day_window_start: float = field(default_factory=time.monotonic)
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    last_success: Optional[str] = None

    def available(self, tokens: int = 0, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        if current - self.minute_window_start >= 60:
            self.requests_this_minute = self.tokens_this_minute = 0
            self.attempts_this_minute = 0
            self.minute_window_start = current
        if current - self.day_window_start >= 86400:
            self.requests_today = 0
            self.attempts_today = 0
            self.day_window_start = current
        return (
            current >= self.cooldown_until
            and self.attempts_this_minute < self.rpm_limit
            and self.tokens_this_minute + tokens <= self.tpm_limit
            and self.requests_today < self.rpd_limit
        )

    def record_attempt(self) -> None:
        self.attempts_this_minute += 1
        self.attempts_today += 1

    def record_rate_limited(self) -> None:
        self.rate_limited_count += 1
        self.last_rate_limited_at = datetime.now(timezone.utc).isoformat()

    def record_success(self, tokens: int) -> None:
        self.requests_this_minute += 1
        self.tokens_this_minute += tokens
        self.requests_today += 1
        self.consecutive_failures = 0
        self.last_error = None
        self.last_success = datetime.now(timezone.utc).isoformat()

    def record_failure(self, error: BaseException) -> None:
        self.consecutive_failures += 1
        self.last_error = "{}: {}".format(type(error).__name__, error)
        self.cooldown_until = time.monotonic() + min(300.0, 2.0 ** min(self.consecutive_failures, 8))

    def record_not_found(self, error: BaseException) -> None:
        """Park a 404'd model: the ID is wrong, not the network.

        A 404 never heals on retry, so the model steps aside for 30
        minutes instead of burning one request per cooldown cycle.
        A success (after a config fix + restart) resets the counter.
        """
        self.consecutive_failures += 1
        self.last_error = "{}: {}".format(type(error).__name__, error)
        self.cooldown_until = time.monotonic() + 1800.0


class ModelProvider(Protocol):
    """The transport-neutral classifier provider contract."""

    name: str
    model: Optional[str]

    def classify(self, session: Any, prompt: str) -> Mapping[str, Any]:
        """Return an untrusted provider payload for one session."""


class OllamaProvider:
    """Adapt the existing local Ollama client to ``ModelProvider``."""

    name = "ollama"

    def __init__(self, client: Optional[OllamaClient] = None):
        self.client = client or OllamaClient()
        self._telemetry_lock = threading.RLock()
        self._calls = 0
        self._successful_calls = 0
        self._failed_calls = 0
        self._timeout_count = 0
        self._last_latency_ms = None
        self._last_error = None
        # Mirrors the wrapped client's last-response usage for
        # observability (read via take_usage).
        self._last_usage: Optional[Dict[str, Any]] = None

    @property
    def model(self) -> Optional[str]:
        return getattr(self.client, "model", None)

    def classify(self, session: Any, prompt: str) -> Mapping[str, Any]:
        started = time.perf_counter()
        with self._telemetry_lock:
            self._calls += 1
        try:
            result = self.client.generate_json(prompt)
            with self._telemetry_lock:
                self._successful_calls += 1
                self._last_error = None
                self._last_usage = getattr(self.client, "_last_usage", None)
            return result
        except Exception as exc:
            with self._telemetry_lock:
                self._failed_calls += 1
                self._last_error = "{}: {}".format(type(exc).__name__, exc)
                self._last_usage = None
                if isinstance(exc, TimeoutError) or "timeout" in str(exc).casefold():
                    self._timeout_count += 1
            raise
        finally:
            with self._telemetry_lock:
                self._last_latency_ms = round((time.perf_counter() - started) * 1000, 1)

    def telemetry(self) -> Dict[str, Any]:
        with self._telemetry_lock:
            return {
                "calls": self._calls,
                "successful_calls": self._successful_calls,
                "failed_calls": self._failed_calls,
                "timeout_count": self._timeout_count,
                "last_latency_ms": self._last_latency_ms,
                "last_error": self._last_error,
            }

    def count_tokens(self, prompt: str) -> int:
        counter = getattr(self.client, "count_tokens", None)
        if not callable(counter):
            raise AttributeError("configured Ollama client has no tokenizer")
        return counter(prompt)


class GeminiProvider:
    """Call Gemini through the official Google GenAI SDK.

    The API key is read only from ``api_key_env`` at request time.  It is
    never stored in the daemon config, provider representation, health data,
    SQLite, logs, or browser messages.
    """

    name = "gemini"

    def __init__(
        self,
        model: str = "gemini-3.5-flash",
        api_key_env: str = "GEMINI_API_KEY",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 15.0,
        urlopen_fn: Optional[Callable[..., Any]] = None,
        client: Optional[Any] = None,
        thinking_level: str = "low",
    ):
        self._model = str(model).strip()
        self.api_key_env = str(api_key_env).strip() or "GEMINI_API_KEY"
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        level = str(thinking_level or "low").strip().lower()
        self.thinking_level = level if level in {"low", "medium", "high"} else "low"
        if not self._model:
            raise ValueError("Gemini model cannot be empty")
        if self.timeout <= 0:
            raise ValueError("Gemini timeout must be positive")
        self._urlopen = urlopen_fn or urlopen
        self._client = client
        # Last-response usage for observability (read via take_usage).
        # Reset at every call start so failures never inherit stale counts.
        self._last_usage: Optional[Dict[str, Any]] = None

    @staticmethod
    def _thinking_config(types: Any, level: Optional[str]) -> Any:
        """Best-effort ThinkingConfig; None when unsupported."""
        if not level:
            return None
        try:
            thinking_level = types.ThinkingLevel[str(level).upper()]
        except Exception:
            return None
        try:
            return types.ThinkingConfig(thinking_level=thinking_level)
        except Exception:
            return None

    def _generate_config(self, types: Any, max_output_tokens: int, thinking: Optional[str]) -> Any:
        """Compose a JSON-mode config, adding thinking only when supported."""
        kwargs: Dict[str, Any] = {
            "response_mime_type": "application/json",
            "temperature": 0,
            "max_output_tokens": max_output_tokens,
        }
        thinking_config = self._thinking_config(types, thinking)
        if thinking_config is not None:
            kwargs["thinking_config"] = thinking_config
        try:
            return types.GenerateContentConfig(**kwargs)
        except Exception:
            kwargs.pop("thinking_config", None)
            return types.GenerateContentConfig(**kwargs)

    def _sdk_complete(self, prompt: str, thinking: Optional[str],
                      max_output_tokens: int = 800) -> Mapping[str, Any]:
        from google import genai
        from google.genai import types

        self._last_usage = None
        client = self._client or genai.Client(api_key=self._api_key())
        config = self._generate_config(types, max_output_tokens, thinking)
        try:
            response = client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            if thinking and "thinking" in str(exc).casefold():
                # Model/API without thinking support: retry bare once
                # instead of failing over to the next provider.
                plain = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                    max_output_tokens=800,
                )
                try:
                    response = client.models.generate_content(
                        model=self._model,
                        contents=prompt,
                        config=plain,
                    )
                except Exception as inner:
                    raise ProviderError("Gemini request failed: {}".format(inner)) from inner
            else:
                raise ProviderError("Gemini request failed: {}".format(exc)) from exc
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("Gemini response contained no text")
        # Stash exact usage for observability; never affects the payload.
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            prompt_tokens = getattr(usage, "prompt_token_count", None)
            output_tokens = getattr(usage, "candidates_token_count", None)
            total_tokens = getattr(usage, "total_token_count", None)
            if isinstance(prompt_tokens, int) or isinstance(output_tokens, int):
                if total_tokens is not None and not isinstance(total_tokens, int):
                    total_tokens = None
                if (total_tokens is None and isinstance(prompt_tokens, int)
                        and isinstance(output_tokens, int)):
                    total_tokens = prompt_tokens + output_tokens
                self._last_usage = {
                    "input_tokens": prompt_tokens if isinstance(prompt_tokens, int) else None,
                    "output_tokens": output_tokens if isinstance(output_tokens, int) else None,
                    "total_tokens": total_tokens,
                    "exact": True,
                }
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("Gemini returned non-JSON classification text") from exc
        if not isinstance(parsed, Mapping):
            raise ProviderError("Gemini classification must be a JSON object")
        return dict(parsed)

    def complete_json(self, prompt: str, max_output_tokens: int = 800,
                      thinking: Optional[str] = None) -> Mapping[str, Any]:
        """One generic JSON-mode completion without the ambiguity retry.

        Used by the real-time verifier. Classification behavior is
        unchanged: ``classify`` and ``classify_batch`` never call this.
        """
        return self._sdk_complete(prompt, thinking, max_output_tokens=max_output_tokens)

    @property
    def model(self) -> Optional[str]:
        return self._model

    def _api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ProviderError("{} is not set".format(self.api_key_env))
        return key

    @staticmethod
    def _response_text(payload: Any) -> str:
        if not isinstance(payload, Mapping):
            raise ProviderError("Gemini response was not an object")
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError("Gemini response contained no candidates")
        candidate = candidates[0]
        content = candidate.get("content") if isinstance(candidate, Mapping) else None
        parts = content.get("parts") if isinstance(content, Mapping) else None
        if not isinstance(parts, list):
            raise ProviderError("Gemini response contained no content parts")
        for part in parts:
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                return part["text"].strip()
        raise ProviderError("Gemini response contained no text")

    @staticmethod
    def _is_ambiguous(payload: Mapping[str, Any]) -> bool:
        try:
            confidence = float(payload.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        return str(payload.get("category", "")).strip().lower() == "neutral" and confidence < 0.55

    def classify(self, session: Any, prompt: str) -> Mapping[str, Any]:
        if self._urlopen is urlopen:
            try:
                from google import genai  # noqa: F401
            except ImportError as exc:
                raise ProviderError("google-genai is not installed") from exc
            try:
                payload = self._sdk_complete(prompt, self.thinking_level)
            except ProviderError:
                raise
            except Exception as exc:
                raise ProviderError("Gemini request failed: {}".format(exc)) from exc
            if self.thinking_level == "low" and self._is_ambiguous(payload):
                # One bounded medium-thinking retry for genuinely ambiguous
                # evidence; a second failure keeps the first verdict rather
                # than failing the whole classification.
                try:
                    return self._sdk_complete(prompt, "medium")
                except (ProviderError, OSError, TimeoutError, ValueError, TypeError):
                    return payload
            return payload

        # Injectable transport retained for deterministic unit tests and
        # compatibility with existing callers that supplied urlopen_fn.
        request_payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        request = Request(
            "{}/models/{}:generateContent".format(self.base_url, quote(self._model, safe="")),
            data=json.dumps(request_payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key(),
            },
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise ProviderError("Gemini request failed with HTTP status {}".format(exc.code)) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError("could not reach Gemini API") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError("Gemini returned invalid JSON") from exc

        text = self._response_text(response_payload)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("Gemini returned non-JSON classification text") from exc
        if not isinstance(parsed, Mapping):
            raise ProviderError("Gemini classification must be a JSON object")
        return dict(parsed)

    def count_tokens(self, prompt: str) -> int:
        try:
            from google import genai
        except ImportError as exc:
            raise ProviderError("google-genai is not installed") from exc
        try:
            client = self._client or genai.Client(api_key=self._api_key())
            result = client.models.count_tokens(model=self._model, contents=prompt)
            total = getattr(result, "total_tokens", None)
            if not isinstance(total, int):
                raise ProviderError("Gemini token count was unavailable")
            return total
        except ProviderError:
            raise
        except Exception as exc:
            # SDK/API errors (e.g. 404 for a model without countTokens
            # support) must degrade to skip-and-fallback, never crash the
            # caller out of the provider chain.
            raise ProviderError("Gemini token counting failed: {}".format(exc)) from exc

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Conservative token estimate from text length (no API call)."""
        if not text:
            return 0
        return max(1, len(text) // 3)

    def build_batch_prompt(self, session_ids: List[str], evidences: List[Mapping[str, Any]]) -> str:
        """Assemble one multi-session prompt; caller enforces the budget."""
        items = [
            {"session_id": session_id, "evidence": dict(evidence)}
            for session_id, evidence in zip(session_ids, evidences)
        ]
        return (
            BATCH_INSTRUCTIONS.format(count=len(items))
            + "\nSESSIONS:\n"
            + json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
        )

    def classify_batch(self, session_ids: List[str], evidences: List[Mapping[str, Any]],
                       timeout_ms: int = 600000) -> List[Mapping[str, Any]]:
        """Classify many sessions in ONE model request.

        Returns one payload per input, in input order. Raises ProviderError
        on any structural problem (wrong count, bad JSON, missing ids) so
        the caller can split the group or fall back to single calls. Batch
        needs the GenAI SDK transport; test doubles using urlopen_fn are
        refused so fakes keep exercising the single-call path.
        """
        if self._urlopen is not urlopen:
            raise ProviderError("batch classification requires the GenAI SDK transport")
        if not session_ids or len(session_ids) != len(evidences):
            raise ProviderError("batch needs matching non-empty sessions and evidence")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ProviderError("google-genai is not installed") from exc
        prompt = self.build_batch_prompt(session_ids, evidences)
        try:
            client = self._client or genai.Client(
                api_key=self._api_key(), http_options={"timeout": int(timeout_ms)})
            # Batches stay on low thinking: one bounded retry per ambiguous
            # session would multiply huge requests, so ambiguous batch items
            # simply carry low confidence instead.
            config = self._generate_config(
                types, min(BATCH_MAX_OUTPUT_TOKENS, 600 * len(session_ids)), "low")
            response = client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=config,
            )
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                raise ProviderError("Gemini batch response contained no text")
            parsed = json.loads(_FENCE_RE.sub(r"\1", text).strip())
            if not isinstance(parsed, list) or len(parsed) != len(session_ids):
                raise ProviderError(
                    "Gemini batch returned {} items for {} sessions".format(
                        len(parsed) if isinstance(parsed, list) else "non-list", len(session_ids)))
            results = []
            for item in parsed:
                if not isinstance(item, Mapping):
                    raise ProviderError("Gemini batch item was not an object")
                results.append(dict(item))
            return results
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("Gemini batch request failed: {}".format(exc)) from exc


class OpenRouterProvider:
    """Call any OpenRouter-hosted model through the chat completions API.

    The API key is read only from ``api_key_env`` at request time. It is
    never stored on the instance, never logged, never persisted, and never
    returned in telemetry or health payloads — only the env var NAME travels
    with the provider. Transport is stdlib urllib (no new dependencies).
    """

    name = "openrouter"

    def __init__(
        self,
        model: str,
        api_key_env: str = "OPENROUTER_API_KEY",
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 60.0,
        title: str = "Noema",
        referer: Optional[str] = None,
        urlopen_fn: Optional[Callable[..., Any]] = None,
    ):
        self._model = str(model).strip()
        self.api_key_env = str(api_key_env).strip() or "OPENROUTER_API_KEY"
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self.title = str(title or "Noema")
        self.referer = str(referer or "").strip() or None
        if not self._model:
            raise ValueError("OpenRouter model cannot be empty")
        if self.timeout <= 0:
            raise ValueError("OpenRouter timeout must be positive")
        self._urlopen = urlopen_fn or urlopen
        # Last-response usage for observability (read via take_usage).
        # Reset at every call start so failures never inherit stale counts.
        self._last_usage: Optional[Dict[str, Any]] = None

    @property
    def model(self) -> Optional[str]:
        return self._model

    def _api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise ProviderError("{} is not set".format(self.api_key_env))
        return key

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer {}".format(self._api_key()),
        }
        if self.referer:
            headers["HTTP-Referer"] = self.referer
        if self.title:
            headers["X-Title"] = self.title
        return headers

    @staticmethod
    def _extract_text(payload: Any) -> str:
        if not isinstance(payload, Mapping):
            raise ProviderError("OpenRouter response was not an object")
        error = payload.get("error")
        if isinstance(error, Mapping):
            raise ProviderError("OpenRouter error {}: {}".format(
                error.get("code", "?"), str(error.get("message", ""))[:200]))
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("OpenRouter response contained no choices")
        message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
        text = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("OpenRouter response contained no text")
        return text.strip()

    def _post(self, body: Mapping[str, Any]) -> Any:
        """POST a JSON body; returns the decoded payload (object or array).

        Retry policy (bounded, no hammering):
        * 400/401/403/404/429: never retried here. 4xx means the request
          or credentials or model ID is wrong (retrying changes nothing);
          a 429 means the shared free-tier quota is hot (the chain falls
          through to the next model immediately and this model's quota
          state cools down exponentially instead).
        * 5xx / timeout / network: one retry after a short sleep, then
          the error propagates so the chain can fall back.
        The provider error message always carries the response body
        (truncated), so a 400/404 can be diagnosed from logs alone.
        """
        self._last_usage = None
        payload_body = json.dumps(body).encode("utf-8")
        attempts = 0
        while True:
            attempts += 1
            request = Request(
                self.base_url + "/chat/completions",
                data=payload_body,
                headers=self._headers(),
                method="POST",
            )
            try:
                with self._urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", "replace")[:500]
                except Exception:
                    detail = ""
                suffix = " {}".format(detail) if detail else ""
                if exc.code in (401, 403):
                    raise ProviderError(
                        "OpenRouter authentication failed (HTTP {}){}".format(exc.code, suffix)) from exc
                if exc.code == 404:
                    raise ProviderError(
                        "OpenRouter model unavailable (HTTP 404){}".format(suffix)) from exc
                if exc.code == 400:
                    raise ProviderError(
                        "OpenRouter bad request (HTTP 400){}".format(suffix)) from exc
                if exc.code == 429:
                    raise ProviderError(
                        "OpenRouter rate limited (HTTP 429){}".format(suffix)) from exc
                if 500 <= exc.code <= 599:
                    if attempts < 2:
                        # Transient upstream failure: one brief retry,
                        # then fall back to the next model.
                        time.sleep(1.0)
                        continue
                    raise ProviderError(
                        "OpenRouter temporary failure (HTTP {}){}".format(exc.code, suffix)) from exc
                raise ProviderError(
                    "OpenRouter request failed (HTTP {}){}".format(exc.code, suffix)) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempts < 2:
                    # Transient transport failure: one brief retry.
                    time.sleep(1.0)
                    continue
                raise ProviderError("could not reach OpenRouter API") from exc
            except json.JSONDecodeError as exc:
                raise ProviderError("OpenRouter returned invalid JSON") from exc
            break
        if not isinstance(payload, (Mapping, list)):
            raise ProviderError("OpenRouter response was not an object")
        if isinstance(payload, Mapping):
            # Stash exact usage for observability; never affects the payload.
            usage = payload.get("usage")
            if isinstance(usage, Mapping):
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
                total_tokens = usage.get("total_tokens")
                if isinstance(prompt_tokens, int) or isinstance(completion_tokens, int):
                    if total_tokens is not None and not isinstance(total_tokens, int):
                        total_tokens = None
                    if (total_tokens is None and isinstance(prompt_tokens, int)
                            and isinstance(completion_tokens, int)):
                        total_tokens = prompt_tokens + completion_tokens
                    choices = payload.get("choices")
                    finish = None
                    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
                        finish = choices[0].get("finish_reason")
                    self._last_usage = {
                        "input_tokens": prompt_tokens if isinstance(prompt_tokens, int) else None,
                        "output_tokens": completion_tokens if isinstance(completion_tokens, int) else None,
                        "total_tokens": total_tokens,
                        "exact": True,
                        "finish_reason": finish if isinstance(finish, str) else None,
                        "provider_request_id": payload.get("id") if isinstance(payload.get("id"), str) else None,
                    }
        return payload

    def _post_json_object(self, body: Mapping[str, Any]) -> Any:
        """POST with ``response_format: json_object``, degrading gracefully.

        Some OpenRouter free models reject structured-output parameters
        with HTTP 400 even though the model ID is valid. The prompt
        already demands JSON-only output, so on a format-related 400 the
        identical request is retried once WITHOUT ``response_format``.
        Any other error propagates untouched with its body attached.
        """
        try:
            return self._post(body)
        except ProviderError as exc:
            if "response_format" in body and _looks_like_format_error(exc):
                logger.info(
                    "RETRY_BARE model=%s reason=response_format_400", self._model)
                bare = {key: value for key, value in body.items()
                        if key != "response_format"}
                return self._post(bare)
            raise

    def classify(self, session: Any, prompt: str) -> Mapping[str, Any]:
        payload = self._post_json_object({
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 800,
            "response_format": {"type": "json_object"},
        })
        try:
            parsed = json.loads(self._extract_text(payload))
        except json.JSONDecodeError as exc:
            raise ProviderError("OpenRouter returned non-JSON classification text") from exc
        if not isinstance(parsed, Mapping):
            raise ProviderError("OpenRouter classification must be a JSON object")
        return dict(parsed)

    def complete_json(self, prompt: str, max_tokens: int = 300) -> Mapping[str, Any]:
        """One generic JSON-mode completion (real-time verifier path).

        Classification behavior is unchanged: ``classify`` and
        ``classify_batch`` never call this.
        """
        payload = self._post_json_object({
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max(1, int(max_tokens)),
            "response_format": {"type": "json_object"},
        })
        try:
            parsed = json.loads(self._extract_text(payload))
        except json.JSONDecodeError as exc:
            raise ProviderError("OpenRouter returned non-JSON verification text") from exc
        if not isinstance(parsed, Mapping):
            raise ProviderError("OpenRouter verification must be a JSON object")
        return dict(parsed)

    def classify_batch(self, session_ids: List[str], evidences: List[Mapping[str, Any]],
                       timeout_ms: int = 600000) -> List[Mapping[str, Any]]:
        """Classify many sessions in ONE model request (same contract as Gemini)."""
        if not session_ids or len(session_ids) != len(evidences):
            raise ProviderError("batch needs matching non-empty sessions and evidence")
        prompt = (
            BATCH_INSTRUCTIONS.format(count=len(session_ids))
            + "\nSESSIONS:\n"
            + json.dumps(
                [{"session_id": session_id, "evidence": dict(evidence)}
                 for session_id, evidence in zip(session_ids, evidences)],
                ensure_ascii=False, sort_keys=True, default=str)
        )
        saved_timeout, self.timeout = self.timeout, max(self.timeout, timeout_ms / 1000.0)
        try:
            payload = self._post_json_object({
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": min(65536, 800 * len(session_ids)),
                "response_format": {"type": "json_object"},
            })
        finally:
            self.timeout = saved_timeout
        try:
            parsed = json.loads(_FENCE_RE.sub(r"\1", self._extract_text(payload)).strip())
        except json.JSONDecodeError as exc:
            raise ProviderError("OpenRouter batch returned non-JSON text") from exc
        if not isinstance(parsed, list) or len(parsed) != len(session_ids):
            raise ProviderError(
                "OpenRouter batch returned {} items for {} sessions".format(
                    len(parsed) if isinstance(parsed, list) else "non-list", len(session_ids)))
        results = []
        for item in parsed:
            if not isinstance(item, Mapping):
                raise ProviderError("OpenRouter batch item was not an object")
            results.append(dict(item))
        return results

    def count_tokens(self, prompt: str) -> int:
        """Length-based estimate: OpenRouter exposes no tokenizer endpoint."""
        return GeminiProvider._estimate_tokens(prompt)

    def build_batch_prompt(self, session_ids: List[str], evidences: List[Mapping[str, Any]]) -> str:
        """Assemble one multi-session prompt; caller enforces the budget."""
        items = [
            {"session_id": session_id, "evidence": dict(evidence)}
            for session_id, evidence in zip(session_ids, evidences)
        ]
        return (
            BATCH_INSTRUCTIONS.format(count=len(items))
            + "\nSESSIONS:\n"
            + json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
        )


class ProviderChain:
    """Try providers in strict priority order: OpenRouter, hosted, Ollama."""

    name = "hosted_chain"
    model = None
    # Default Gemini order when the daemon passes no explicit list: the
    # high-frequency workhorse first, then the escalation-capable and
    # fallback models. Google-only classification with no fallback to
    # OpenRouter or Ollama.
    HOSTED_MODELS = (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.8-flash",
    )

    # Default OpenRouter free-model order (fast + reasoning first).
    # Override the ORDER with OPENROUTER_FREE_MODELS. Canonical IDs, windows,
    # and limits live in OPENROUTER_MODEL_REGISTRY above; unknown IDs fail
    # fast with a 404 and are skipped automatically — a stale ID can never
    # wedge the chain. Only enabled, normal-classification generative chat
    # models enter the chain: embeddings/rerankers/safety models are never
    # eligible even if added to the registry later.
    OPENROUTER_MODELS = tuple(
        entry["id"]
        for entry in sorted(OPENROUTER_MODEL_REGISTRY, key=lambda item: item["priority"])
        if entry.get("enabled") and entry.get("normal_classification")
    )

    def __init__(self, hosted: Optional[list] = None, ollama: Optional[OllamaProvider] = None,
                 usage_path: Optional[Any] = None, include_ollama: bool = False,
                 day_timezone: str = "Asia/Kolkata", openrouter: Optional[list] = None,
                 observer: Optional[Any] = None):
        self.providers = list(openrouter or []) + list(
            hosted or [GeminiProvider(model=model) for model in self.HOSTED_MODELS])
        self.ollama = ollama or OllamaProvider()
        # Gemini-only mode: the local tail is never consulted, so a live
        # Gemini deployment classifies exclusively through hosted models
        # and failed sessions stay pending instead of falling back.
        self.include_ollama = bool(include_ollama)
        # Observability hook. The chain emits standardized invocation
        # events to ``observer`` (a TelemetryRecorder); when None, no
        # telemetry is produced and routing behavior is unchanged.
        self.observer = observer
        self.last_provider = None
        self.last_model = None
        self.attempts = []
        self.model_latency_ms: Dict[str, float] = {}
        # Packing diagnostics for the latest classify_batch call, one entry
        # per group: {group_index, size, budget, measured, tail_sheds}.
        self._last_pack_stats: list = []
        self.rate_limits = {}
        for provider in self.providers:
            rpm, tpm, rpd = MODEL_LIMITS.get(str(provider.model), DEFAULT_HOSTED_LIMITS)
            self.rate_limits[provider.model] = QuotaState(
                provider.name, str(provider.model),
                rpm_limit=rpm, tpm_limit=tpm, rpd_limit=rpd,
            )
        self.ledger = QuotaLedger(usage_path, day_timezone=day_timezone)
        for provider in self.providers:
            state = self.rate_limits[provider.model]
            # A restart must not forget today's spend: seed the in-memory
            # daily counter from the persisted ledger.
            state.requests_today = self.ledger.usage("llm", provider.model)["requests"]

    # -- observability (emits events, never changes routing) ---------------

    @staticmethod
    def _tele_context(telemetry: Optional[Mapping[str, Any]], operation: str,
                      purpose: str, pipeline: str) -> Dict[str, Any]:
        tele = dict(telemetry or {})
        tele.setdefault("purpose", purpose)
        tele.setdefault("pipeline", pipeline)
        tele["operation"] = operation
        if not tele.get("request_id"):
            tele["request_id"] = new_request_id()
        return tele

    @staticmethod
    def _quota_snapshot(state: Any) -> Dict[str, int]:
        if state is None:
            return {}
        try:
            return {
                "requests_today": int(getattr(state, "requests_today", 0) or 0),
                "tokens_this_minute": int(getattr(state, "tokens_this_minute", 0) or 0),
                "attempts_today": int(getattr(state, "attempts_today", 0) or 0),
                "attempts_this_minute": int(getattr(state, "attempts_this_minute", 0) or 0),
                "rate_limited_count": int(getattr(state, "rate_limited_count", 0) or 0),
            }
        except (TypeError, ValueError):
            return {}

    def _emit_invocation(self, **fields: Any) -> bool:
        observer = self.observer
        if observer is None:
            return False
        try:
            record = assemble_invocation(**fields)
            recorder = getattr(observer, "record_invocation", None)
            if not callable(recorder):
                return False
            return bool(recorder(record))
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

    def _estimate_tokens(self, provider: Any, prompt: str) -> int:
        """Prompt tokens; 0 when the provider offers no usable counter."""
        counter = getattr(provider, "count_tokens", None)
        if not callable(counter):
            return 0
        try:
            return int(counter(prompt))
        except Exception:
            # Any counter failure (auth, 404, transport) means "unknown",
            # never a chain-crashing exception. Quota checks below then run
            # on the output reserve alone.
            return 0

    def _model_usable(self, provider: Any, prompt: str) -> int:
        """Return billable tokens if the model may be called, else -1.

        Both the per-minute guard and the persisted daily (RPD) ledger must
        pass; otherwise the model is skipped without any API call.
        """
        state = self.rate_limits.get(provider.model)
        if state is None:
            return 0
        try:
            prompt_tokens = self._estimate_tokens(provider, prompt)
        except (ProviderError, OSError, TimeoutError, ConnectionError):
            return -1
        billable = prompt_tokens + OUTPUT_TOKEN_RESERVE
        if not state.available(billable):
            return -1
        if not self.ledger.check("llm", provider.model, billable, state.rpd_limit):
            return -1
        return billable

    def _chain(self) -> list:
        tail = [self.ollama] if self.include_ollama and self.ollama is not None else []
        return [*self.providers, *tail]

    @staticmethod
    def _batch_budget(model: Any) -> int:
        """Usable batch tokens for one request to ``model``.

        The binding constraint is ``min(context window, per-minute tokens,
        absolute cap)`` minus a safety margin, so a request can never be
        built that the model could not legally serve in one minute. Flash
        models land near ~196K (TPM-bound, never pushed to 250K);
        small-window models far lower.
        """
        window = MODEL_CONTEXT_WINDOWS.get(str(model), DEFAULT_CONTEXT_WINDOW)
        _, tpm, _ = MODEL_LIMITS.get(str(model), DEFAULT_HOSTED_LIMITS)
        return max(4000, min(int(window), int(tpm), BATCH_TOKEN_BUDGET) - BATCH_SAFETY_MARGIN)

    def _pack_batches(self, entries: List[Mapping[str, Any]], provider: Any,
                      budget: int) -> List[tuple]:
        """Group sessions to exploit the model's full usable window.

        Fill order is chronological (synthesis weighs recency). Item sizes
        come from text length first (chars//4, one accurate-ish estimate),
        then each assembled group is measured EXACTLY once with the model's
        real tokenizer; oversized groups shed their tail until they fit.
        Groups are only ever split for size — never pre-split small.
        Returns ``[(group, measured_tokens_or_None)]``.
        """
        build = getattr(provider, "build_batch_prompt", None)
        counter = getattr(provider, "count_tokens", None)

        def assemble(group: List[Mapping[str, Any]]) -> str:
            ids = [entry["session"].id for entry in group]
            evs = [entry["evidence"] for entry in group]
            if callable(build):
                try:
                    return build(ids, evs)
                except Exception:
                    pass
            return BATCH_INSTRUCTIONS.format(count=len(group)) + "\nSESSIONS:\n" + json.dumps(
                [{"session_id": sid, "evidence": ev} for sid, ev in zip(ids, evs)],
                ensure_ascii=False, sort_keys=True, default=str)

        def measure(prompt: str) -> Optional[int]:
            if not callable(counter):
                return None
            try:
                return int(counter(prompt))
            except Exception:
                return None

        def item_estimate(entry: Mapping[str, Any]) -> int:
            return len(json.dumps(entry.get("evidence", {}), ensure_ascii=False,
                                  sort_keys=True, default=str)) // 4 + BATCH_OUTPUT_RESERVE_PER_ITEM

        header_est = len(BATCH_INSTRUCTIONS.format(count=0)) // 4 + 500
        groups: List[tuple] = []
        pack_stats: List[Dict[str, Any]] = []
        remaining = list(entries)
        group_index = 0
        while remaining:
            # Greedy chronological fill on the cheap length estimate.
            group: List[Mapping[str, Any]] = []
            est = header_est
            while remaining and (not group or est + item_estimate(remaining[0]) <= budget):
                entry = remaining.pop(0)
                group.append(entry)
                est += item_estimate(entry)
            # Exact tokenizer verification: shed the tail back onto the
            # queue until the assembled request truly fits. Singletons are
            # always kept here; the send path splits only on real errors.
            sheds = 0
            measured = measure(assemble(group))
            while measured is not None and measured > budget and len(group) > 1:
                remaining.insert(0, group.pop())
                sheds += 1
                measured = measure(assemble(group))
            groups.append((group, measured))
            pack_stats.append({
                "group_index": group_index,
                "size": len(group),
                "budget": int(budget),
                "measured": measured,
                "tail_sheds": sheds,
                "utilization": (round(measured / budget, 4)
                                if measured and budget else None),
            })
            group_index += 1
        self._last_pack_stats = pack_stats
        return groups

    @staticmethod
    def _valid_item(payload: Any, session_id: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False
        if str(payload.get("session_id", "")) != str(session_id):
            return False
        category = str(payload.get("category", "")).strip().lower()
        productivity = str(payload.get("productivity", "neutral")).strip().lower()
        return (
            category in {"productive", "distractive", "neutral", "development", "research"}
            and productivity in {"productive", "distracting", "neutral"}
            and isinstance(payload.get("activity"), str)
            and bool(payload.get("activity", "").strip())
            and isinstance(payload.get("signal"), str)
            and bool(payload.get("signal", "").strip())
        )

    @staticmethod
    def _looks_like_length_error(exc: BaseException) -> bool:
        text = str(exc).casefold()
        return any(token in text for token in (
            "too long", "too large", "maximum context", "context length",
            "token limit", "exceeds", "out of range", "invalid argument"))

    @staticmethod
    def _looks_like_quota_error(exc: BaseException) -> bool:
        text = str(exc).casefold()
        return any(token in text for token in (
            "429", "resource_exhausted", "resource exhausted", "quota",
            "rate limit", "rate_limit", "tokens per minute", "tpm"))

    def _can_serve_smaller(self, billable: int) -> bool:
        """Is there a smaller-window batch provider with daily room left?

        Splitting a group only helps when such a provider exists; otherwise
        the items correctly fall through to single calls or pending.
        """
        for provider in self.providers:
            if not callable(getattr(provider, "classify_batch", None)):
                continue
            state = self.rate_limits.get(provider.model)
            if state is None:
                continue
            if self._batch_budget(provider.model) >= billable:
                continue
            if self.ledger.check("llm", provider.model, 0, state.rpd_limit):
                return True
        return False

    def _pack_provider(self) -> Any:
        """First chain provider with daily room: packing target."""
        for provider in self.providers:
            if not callable(getattr(provider, "classify_batch", None)):
                continue
            state = self.rate_limits.get(provider.model)
            if state is None:
                return provider
            if self.ledger.check("llm", provider.model, 0, state.rpd_limit):
                return provider
        return self.providers[0] if self.providers else None

    @staticmethod
    def _now_pair() -> tuple:
        """(iso_timestamp, epoch_ms) from one clock read."""
        now = datetime.now(timezone.utc)
        return (now.isoformat().replace("+00:00", "Z"), int(now.timestamp() * 1000))

    def classify_batch(self, entries: List[Mapping[str, Any]],
                       telemetry: Optional[Mapping[str, Any]] = None) -> Dict[str, Mapping[str, Any]]:
        """Classify many sessions with as few requests as quota allows.

        Entries are ``{"session", "evidence", "prompt"}`` mappings. Groups
        are packed to exploit the serving model's full usable window and
        sent whole; they split only when a model reports overflow. Returns
        ``{session_id: payload}`` for every item a hosted model served. Items
        no batch call could serve are simply absent: the classifier falls
        them back to single calls. Providers without batch support (local
        Ollama) are skipped here and remain reachable through that path.
        """
        results: Dict[str, Mapping[str, Any]] = {}
        self.last_batch = {}
        self.last_batch_size = 0
        if not entries:
            return results
        tele = self._tele_context(telemetry, "BATCH", Purpose.NORMAL_CLASSIFICATION,
                                  Pipeline.SEMANTIC_CLASSIFICATION)
        pack_provider = self._pack_provider()
        if pack_provider is None:
            return results
        budget = self._batch_budget(getattr(pack_provider, "model", None))
        packed = self._pack_batches(entries, pack_provider, budget)
        utils = [stat["utilization"] for stat in self._last_pack_stats
                 if stat.get("utilization") is not None]
        self._emit_operation(
            "batch_pack", str(getattr(pack_provider, "model", "?")),
            metadata={
                "request_id": tele["request_id"],
                "groups": len(packed),
                "sessions": len(entries),
                "budget": int(budget),
                "tail_sheds": sum(stat["tail_sheds"] for stat in self._last_pack_stats),
                "utilizations": utils,
                "mean_utilization": (round(sum(utils) / len(utils), 4) if utils else None),
            })
        for group, measured in packed:
            for session_id, payload in self._classify_group(
                    group, tele, measured, budget).items():
                results[session_id] = payload
        return results

    def _classify_group(self, group: List[Mapping[str, Any]],
                        tele: Optional[Mapping[str, Any]] = None,
                        measured: Optional[int] = None,
                        budget: Optional[int] = None,
                        _split_depth: int = 0) -> Dict[str, Mapping[str, Any]]:
        session_ids = [entry["session"].id for entry in group]
        evidences = [entry["evidence"] for entry in group]
        tele = self._tele_context(tele, "BATCH_GROUP", Purpose.NORMAL_CLASSIFICATION,
                                  Pipeline.SEMANTIC_CLASSIFICATION)
        group_started = time.perf_counter()
        size_skipped = False
        previous: Optional[tuple] = None
        utilization = None
        if measured and budget:
            try:
                utilization = round(measured / budget, 4)
            except (TypeError, ValueError, ZeroDivisionError):
                utilization = None
        for provider in self._chain():
            batcher = getattr(provider, "classify_batch", None)
            if not callable(batcher):
                continue
            self.attempts.append((provider.name, provider.model))
            if previous is not None:
                logger.info(
                    "FALLBACK model=%s from=%s reason=%s",
                    provider.model, previous[0], str(previous[1])[:150])
            state = self.rate_limits.get(provider.model)
            billable = sum(
                GeminiProvider._estimate_tokens(json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True, default=str))
                + BATCH_OUTPUT_RESERVE_PER_ITEM for evidence in evidences)
            billable += GeminiProvider._estimate_tokens(
                BATCH_INSTRUCTIONS.format(count=len(group))) + 500
            if state is not None:
                if billable > self._batch_budget(provider.model):
                    # Fits a bigger-window model but not this one: never
                    # burn this provider's quota state on an impossible send.
                    size_skipped = True
                    continue
                if not state.available(billable):
                    self._emit_operation(
                        "quota_skip", str(provider.model),
                        metadata={"request_id": tele["request_id"], "provider": provider.name,
                                  "billable_tokens": billable, "reason": "per-minute guard"})
                    continue
                if not self.ledger.check("llm", provider.model, billable, state.rpd_limit):
                    self._emit_operation(
                        "quota_skip", str(provider.model),
                        metadata={"request_id": tele["request_id"], "provider": provider.name,
                                  "billable_tokens": billable, "reason": "daily ledger guard"})
                    continue
            quota_before = self._quota_snapshot(state)
            started = time.perf_counter()
            if state is not None:
                state.record_attempt()
            try:
                items = batcher(session_ids, evidences)
                call_error = None
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                self.model_latency_ms[str(provider.model)] = latency_ms
            except (ProviderError, OSError, TimeoutError, ConnectionError) as exc:
                call_error = exc
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                self.model_latency_ms[str(provider.model)] = latency_ms
                items = []
            stamp_iso, stamp_ms = self._now_pair()
            self._emit_invocation(
                request_id=tele["request_id"], timestamp_iso=stamp_iso,
                timestamp_ms=stamp_ms, provider_name=provider.name,
                model=provider.model, purpose=tele["purpose"],
                pipeline=tele["pipeline"], operation="BATCH_GROUP",
                kind="attempt", session_ids=session_ids,
                batch_size=len(group), attempt_number=0,
                fallback_depth=max(0, len(self.attempts) - 1),
                fallback_from=previous[0] if previous else None,
                fallback_reason=str(previous[1])[:300] if previous else None,
                usage=take_usage(provider) if call_error is None else None,
                estimated_input_tokens=billable,
                error=call_error, success_payload=bool(items),
                latency_ms=latency_ms,
                context_window=MODEL_CONTEXT_WINDOWS.get(str(provider.model)),
                max_output_tokens=None,
                quota_scope=quota_scope_for(provider.name),
                quota_before=quota_before,
                quota_after=self._quota_snapshot(state),
                batch_budget_tokens=budget, batch_input_tokens=measured,
                batch_utilization=utilization,
            )
            if call_error is not None:
                exc = call_error
                splittable = (
                    len(group) > 1
                    and (self._looks_like_length_error(exc) or (
                        self._looks_like_quota_error(exc)
                        and self._can_serve_smaller(billable)))
                )
                if splittable:
                    # Packing overshot what the serving tier accepts, or the
                    # tier throttled a group a smaller model could take.
                    # Split without cooldown so halves retry immediately.
                    self._emit_operation(
                        "batch_split", str(provider.model),
                        duration_ms=latency_ms, success=True,
                        metadata={"request_id": tele["request_id"],
                                  "parent_size": len(group),
                                  "reason": str(exc)[:200],
                                  "split_depth": _split_depth})
                    mid = len(group) // 2
                    merged = self._classify_group(group[:mid], tele, None, budget, _split_depth + 1)
                    merged.update(self._classify_group(group[mid:], tele, None, budget, _split_depth + 1))
                    return merged
                if state is not None:
                    status = _http_status(exc)
                    logger.warning(
                        "MODEL_FAILED model=%s status=%s error=%s",
                        provider.model,
                        status if status is not None else "?",
                        str(exc)[:200])
                    if status == 404:
                        state.record_not_found(exc)
                    else:
                        state.record_failure(exc)
                        if _looks_like_rate_limited(exc):
                            state.record_rate_limited()
                previous = (provider.name, exc)
                continue
            valid = {str(item.get("session_id")): item for item in items
                     if self._valid_item(item, item.get("session_id"))}
            matched = {session_id: valid[str(session_id)]
                       for session_id in session_ids if str(session_id) in valid}
            if not matched:
                if state is not None:
                    state.record_failure(ProviderError("batch returned no usable items"))
                previous = (provider.name, ProviderError("batch returned no usable items"))
                continue
            if state is not None:
                state.record_success(billable)
                self.ledger.consume("llm", provider.model, billable)
            self.last_provider = provider.name
            self.last_model = provider.model
            self.last_batch_size += len(matched)
            for session_id, item in matched.items():
                self.last_batch[session_id] = (provider.name, provider.model)
            group_latency = round((time.perf_counter() - group_started) * 1000, 1)
            stamp_iso, stamp_ms = self._now_pair()
            self._emit_invocation(
                request_id=tele["request_id"], timestamp_iso=stamp_iso,
                timestamp_ms=stamp_ms, provider_name=provider.name,
                model=provider.model, purpose=tele["purpose"],
                pipeline=tele["pipeline"], operation="BATCH_GROUP",
                kind="request", session_ids=list(matched),
                batch_size=len(group), attempt_number=0,
                fallback_depth=max(0, len(self.attempts) - 1),
                fallback_from=previous[0] if previous else None,
                fallback_reason=str(previous[1])[:300] if previous else None,
                usage=None, estimated_input_tokens=None,
                error=None, success_payload=True,
                latency_ms=group_latency,
                context_window=MODEL_CONTEXT_WINDOWS.get(str(provider.model)),
                max_output_tokens=None,
                quota_scope=quota_scope_for(provider.name),
                quota_before=quota_before,
                quota_after=self._quota_snapshot(state),
                batch_budget_tokens=budget, batch_input_tokens=measured,
                batch_utilization=utilization,
            )
            return matched
        if size_skipped and len(group) > 1 and self._can_serve_smaller(
                sum(GeminiProvider._estimate_tokens(json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True, default=str))
                    + BATCH_OUTPUT_RESERVE_PER_ITEM for evidence in evidences)):
            # Packed for a big-window model, but only smaller-window models
            # have room: split down until they fit instead of degrading all
            # the way to single calls.
            self._emit_operation(
                "batch_split", "?",
                metadata={"request_id": tele["request_id"],
                          "parent_size": len(group),
                          "reason": "size_skipped",
                          "split_depth": _split_depth})
            mid = len(group) // 2
            merged = self._classify_group(group[:mid], tele, None, budget, _split_depth + 1)
            merged.update(self._classify_group(group[mid:], tele, None, budget, _split_depth + 1))
            return merged
        stamp_iso, stamp_ms = self._now_pair()
        self._emit_invocation(
            request_id=tele["request_id"], timestamp_iso=stamp_iso,
            timestamp_ms=stamp_ms, provider_name="chain",
            model=None, purpose=tele["purpose"],
            pipeline=tele["pipeline"], operation="BATCH_GROUP",
            kind="request", session_ids=session_ids,
            batch_size=len(group), attempt_number=0,
            fallback_depth=max(0, len(self.attempts)),
            fallback_from=previous[0] if previous else None,
            fallback_reason=str(previous[1])[:300] if previous else "no provider could serve group",
            usage=None, estimated_input_tokens=None,
            error=previous[1] if previous else ProviderError("no provider could serve group"),
            success_payload=False,
            latency_ms=round((time.perf_counter() - group_started) * 1000, 1),
            context_window=None, max_output_tokens=None,
            quota_scope=None, quota_before={}, quota_after={},
            batch_budget_tokens=budget, batch_input_tokens=measured,
            batch_utilization=utilization,
        )
        return {}

    def classify(self, session: Any, prompt: str,
                 telemetry: Optional[Mapping[str, Any]] = None) -> Mapping[str, Any]:
        tele = self._tele_context(telemetry, "SINGLE_CLASSIFY",
                                  Purpose.NORMAL_CLASSIFICATION,
                                  Pipeline.SEMANTIC_CLASSIFICATION)
        request_started = time.perf_counter()
        session_ids = [getattr(session, "id", None)] if getattr(session, "id", None) else []
        self.attempts = []
        previous: Optional[tuple] = None
        for provider in self._chain():
            self.attempts.append((provider.name, provider.model))
            if previous is not None:
                logger.info(
                    "FALLBACK model=%s from=%s reason=%s",
                    provider.model, previous[0], str(previous[1])[:150])
            state = self.rate_limits.get(provider.model)
            billable = 0
            estimated: Optional[int] = None
            if state is not None:
                billable = self._model_usable(provider, prompt)
                if billable < 0:
                    self._emit_operation(
                        "quota_skip", str(provider.model),
                        metadata={"request_id": tele["request_id"], "provider": provider.name,
                                  "billable_tokens": billable, "reason": "quota guard"})
                    continue
                estimated = billable
            else:
                # Local tail has no quota state; use the tokenizer estimate
                # when it yields a real count, UNKNOWN otherwise.
                counted = self._estimate_tokens(provider, prompt)
                estimated = counted if counted > 0 else None
            quota_before = self._quota_snapshot(state)
            started = time.perf_counter()
            if state is not None:
                state.record_attempt()
            try:
                payload = provider.classify(session, prompt)
                call_error = None
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                self.model_latency_ms[str(provider.model)] = latency_ms
            except (ProviderError, OSError, TimeoutError, ConnectionError) as exc:
                call_error = exc
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                self.model_latency_ms[str(provider.model)] = latency_ms
                payload = {}
            valid = call_error is None and self._valid_payload(payload)
            if not valid and call_error is None:
                call_error = ProviderError("provider returned an invalid classification schema")
            stamp_iso, stamp_ms = self._now_pair()
            self._emit_invocation(
                request_id=tele["request_id"], timestamp_iso=stamp_iso,
                timestamp_ms=stamp_ms, provider_name=provider.name,
                model=provider.model, purpose=tele["purpose"],
                pipeline=tele["pipeline"], operation="SINGLE_CLASSIFY",
                kind="attempt", session_ids=session_ids,
                batch_size=0, attempt_number=0,
                fallback_depth=max(0, len(self.attempts) - 1),
                fallback_from=previous[0] if previous else None,
                fallback_reason=str(previous[1])[:300] if previous else None,
                usage=take_usage(provider) if call_error is None else None,
                estimated_input_tokens=(
                    estimated if call_error is None
                    else max(0, (estimated or 0) - OUTPUT_TOKEN_RESERVE)),
                error=call_error, success_payload=valid,
                latency_ms=latency_ms,
                context_window=MODEL_CONTEXT_WINDOWS.get(str(provider.model)),
                max_output_tokens=800,
                quota_scope=quota_scope_for(provider.name),
                quota_before=quota_before,
                quota_after=self._quota_snapshot(state),
            )
            if call_error is not None:
                status = _http_status(call_error)
                logger.warning(
                    "MODEL_FAILED model=%s status=%s error=%s",
                    provider.model,
                    status if status is not None else "?",
                    str(call_error)[:200])
                if state is not None:
                    if status == 404:
                        # Wrong model ID: park it for 30 minutes instead
                        # of burning one request per cooldown cycle.
                        state.record_not_found(call_error)
                    else:
                        # 429/5xx/timeout: exponential cooldown; the
                        # fallback above already moved to the next model.
                        state.record_failure(call_error)
                        if _looks_like_rate_limited(call_error):
                            state.record_rate_limited()
                previous = (provider.name, call_error)
                continue
            if state is not None:
                state.record_success(billable)
                self.ledger.consume("llm", provider.model, billable)
            self.last_provider = provider.name
            self.last_model = provider.model
            stamp_iso, stamp_ms = self._now_pair()
            self._emit_invocation(
                request_id=tele["request_id"], timestamp_iso=stamp_iso,
                timestamp_ms=stamp_ms, provider_name=provider.name,
                model=provider.model, purpose=tele["purpose"],
                pipeline=tele["pipeline"], operation="SINGLE_CLASSIFY",
                kind="request", session_ids=session_ids,
                batch_size=0, attempt_number=0,
                fallback_depth=max(0, len(self.attempts) - 1),
                fallback_from=previous[0] if previous else None,
                fallback_reason=str(previous[1])[:300] if previous else None,
                usage=None, estimated_input_tokens=None,
                error=None, success_payload=True,
                latency_ms=round((time.perf_counter() - request_started) * 1000, 1),
                context_window=MODEL_CONTEXT_WINDOWS.get(str(provider.model)),
                max_output_tokens=800,
                quota_scope=quota_scope_for(provider.name),
                quota_before=quota_before,
                quota_after=self._quota_snapshot(state),
            )
            return payload
        stamp_iso, stamp_ms = self._now_pair()
        final_error = previous[1] if previous else ProviderError(
            "all hosted models and Ollama fallback failed" if self.include_ollama
            else "all hosted Gemini models failed (Gemini-only mode)")
        self._emit_invocation(
            request_id=tele["request_id"], timestamp_iso=stamp_iso,
            timestamp_ms=stamp_ms, provider_name="chain",
            model=None, purpose=tele["purpose"],
            pipeline=tele["pipeline"], operation="SINGLE_CLASSIFY",
            kind="request", session_ids=session_ids,
            batch_size=0, attempt_number=0,
            fallback_depth=max(0, len(self.attempts)),
            fallback_from=previous[0] if previous else None,
            fallback_reason=str(previous[1])[:300] if previous else "all providers failed",
            usage=None, estimated_input_tokens=None,
            error=final_error, success_payload=False,
            latency_ms=round((time.perf_counter() - request_started) * 1000, 1),
            context_window=None, max_output_tokens=None,
            quota_scope=None, quota_before={}, quota_after={},
        )
        if self.include_ollama:
            raise ProviderError("all hosted models and Ollama fallback failed")
        raise ProviderError("all hosted Gemini models failed (Gemini-only mode)")

    def embedding_allowed(self, model: str, tokens: int) -> bool:
        """True if an embedding call of ``tokens`` tokens fits today's quota."""
        _, _, rpd = MODEL_LIMITS.get(str(model), DEFAULT_HOSTED_LIMITS)
        return self.ledger.check("embedding", model, tokens, rpd)

    def track_embedding(self, model: str, tokens: int) -> bool:
        """Record an embedding call made by us. Returns False if over quota.

        Embedding calls happen outside this chain, so callers check first
        via :meth:`embedding_allowed` and record afterwards with this
        method; both consult the same persisted ledger, so TPM/RPD can
        never be crossed silently.
        """
        _, _, rpd = MODEL_LIMITS.get(str(model), DEFAULT_HOSTED_LIMITS)
        if not self.ledger.check("embedding", model, tokens, rpd):
            return False
        self.ledger.consume("embedding", model, tokens)
        return True

    def usage(self) -> Dict[str, Dict[str, int]]:
        """Today's persisted usage table (llm:* and embedding:* rows)."""
        return self.ledger.snapshot()

    def count_tokens(self, prompt: str) -> int:
        """Count against the highest-priority usable model before sending."""
        for provider in self._chain():
            try:
                return provider.count_tokens(prompt)
            except (AttributeError, ProviderError, OSError, TimeoutError, ConnectionError):
                continue
        raise ProviderError("no provider supplied a pre-request token count")

    @staticmethod
    def _valid_payload(payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False
        category = str(payload.get("category", "")).strip().lower()
        productivity = str(payload.get("productivity", "neutral")).strip().lower()
        return (
            category in {"productive", "distractive", "neutral", "development", "research"}
            and productivity in {"productive", "distracting", "neutral"}
            and isinstance(payload.get("activity"), str)
            and bool(payload.get("activity", "").strip())
            and isinstance(payload.get("signal"), str)
            and bool(payload.get("signal", "").strip())
        )

    def telemetry(self) -> Dict[str, Any]:
        return {
            "provider_chain": [
                {"provider": provider.name, "model": provider.model}
                for provider in self._chain()
            ],
            "last_provider": self.last_provider,
            "last_model": self.last_model,
            "attempts": list(self.attempts),
            "rate_limits": {
                model: state.__dict__.copy() for model, state in self.rate_limits.items()
            },
            "usage_today": self.usage(),
            "model_latency_ms": dict(self.model_latency_ms),
        }


__all__ = [
    "FAST_DISTRACTION_MODEL",
    "GeminiProvider",
    "ProviderChain",
    "ModelProvider",
    "OllamaProvider",
    "OpenRouterProvider",
    "OPENROUTER_MODEL_REGISTRY",
    "ProviderError",
    "openrouter_registry",
]
