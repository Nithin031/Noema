"""External LLM lanes for Noema: a web-LLM hub and a dedicated Claude Code.

Provider order (strict priority, Gemini stays last and lives in providers.py)::

    web LLM (your hub, Noema's *own* instance)  ->  Claude Code (Sonnet, low effort)  ->  Gemini

Design rules
------------
* Noema never shares state with other apps. It talks to its own hub instance
  (own port, own browser profile, own "last conversation") and spawns its own
  short-lived ``claude -p`` process per call, in its own empty working directory
  with tools disabled. Every web request asks for a fresh conversation, so
  nothing Noema sends can continue, or become, another app's chat.
* Other apps come first. Before it uses the web LLM, Noema checks a tiny shared
  lease directory (``<lane_dir>/<rival>.busy``, refreshed by the rival while it
  has a request in flight). A fresh lease means "busy": Noema skips the web LLM
  without penalty and falls through to Claude Code, then Gemini. While Noema
  itself is calling, it writes ``noema.busy`` so rivals can look too.
* Inside Noema, one request at a time per lane, ordered by priority
  (realtime nudge reasoning before background classification).

Nothing here is imported by ``providers.py``; the chain talks to lanes through
duck typing (``lane_busy()``, ``limits``, ``classify``, ``complete_json``,
``classify_batch``, ``count_tokens``).
"""

from __future__ import annotations

import contextlib
import heapq
import itertools
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextvars import ContextVar
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from noema.infrastructure.providers import (
    BATCH_INSTRUCTIONS,
    _FENCE_RE,
    GeminiProvider,
    ProviderError,
)

logger = logging.getLogger("noema.llm_lane")

# Effectively no local quota: the hub / Claude Code enforce their own limits and
# answer 429, which the chain already handles. (rpm, tpm, rpd)
UNMETERED_LIMITS: Tuple[int, int, int] = (600, 50_000_000, 1_000_000)

JSON_ONLY_SUFFIX = (
    "\n\nReturn ONLY the JSON requested above: no prose, no markdown fences, "
    "no commentary before or after it."
)


class LaneBusy(ProviderError):
    """The lane is taken by a higher-priority user; fall through, no penalty."""


class Priority(IntEnum):
    """Lower number goes first. Other apps (Quaero) sit above both via lease files."""

    REALTIME = 0     # drift reasoning / nudge copy: a human is waiting
    BACKGROUND = 1   # periodic classification


_priority: ContextVar[Priority] = ContextVar("noema_lane_priority", default=Priority.BACKGROUND)


@contextlib.contextmanager
def lane_priority(priority: Priority) -> Iterator[None]:
    token = _priority.set(priority)
    try:
        yield
    finally:
        _priority.reset(token)


def current_priority() -> Priority:
    return _priority.get()


# ---------------------------------------------------------------------------
# in-process priority gate


class PriorityGate:
    """Capacity-1 gate; waiters are served by (priority, arrival order)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._held = False
        self._waiters: List[Tuple[int, int]] = []
        self._seq = itertools.count()
        self.waiting = 0

    @contextlib.contextmanager
    def acquire(self, priority: int, timeout: float) -> Iterator[None]:
        ticket = (int(priority), next(self._seq))
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            heapq.heappush(self._waiters, ticket)
            self.waiting = len(self._waiters)
            try:
                while self._held or self._waiters[0] != ticket:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LaneBusy("lane wait timed out behind higher-priority work")
                    self._cond.wait(remaining)
                self._held = True
            finally:
                self._waiters.remove(ticket)
                heapq.heapify(self._waiters)
                self.waiting = len(self._waiters)
                self._cond.notify_all()
        try:
            yield
        finally:
            with self._cond:
                self._held = False
                self._cond.notify_all()

    @property
    def held(self) -> bool:
        return self._held


# ---------------------------------------------------------------------------
# cross-app politeness (lease files)


def default_lane_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "LLM-LANE" if base else Path.home() / ".llm-lane"


class LaneCoordinator:
    """Shared lease directory. ``<name>.busy`` is fresh => that app has work in flight."""

    def __init__(self, lane_dir: Optional[Any] = None, owner: str = "noema",
                 rivals: Sequence[str] = ("quaero",), stale_seconds: float = 30.0,
                 heartbeat_seconds: float = 10.0) -> None:
        self.dir = Path(lane_dir) if lane_dir else default_lane_dir()
        self.owner = owner
        self.rivals = tuple(r for r in rivals if r and r != owner)
        self.stale_seconds = float(stale_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.last_rival: Optional[str] = None
        self.last_yield_at: Optional[float] = None

    def rival_active(self, now: Optional[float] = None) -> Optional[str]:
        current = time.time() if now is None else now
        for rival in self.rivals:
            try:
                age = current - (self.dir / f"{rival}.busy").stat().st_mtime
            except OSError:
                continue
            if age <= self.stale_seconds:
                self.last_rival = rival
                self.last_yield_at = current
                return rival
        return None

    @contextlib.contextmanager
    def hold(self, what: str = "") -> Iterator[None]:
        """Mark this app busy (refreshed by a heartbeat) while a request is in flight."""
        path = self.dir / f"{self.owner}.busy"
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.heartbeat_seconds):
                with contextlib.suppress(OSError):
                    os.utime(path, None)

        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"pid": os.getpid(), "since": time.time(), "what": what[:80]}),
                            encoding="utf-8")
        except OSError:
            path = None  # type: ignore[assignment]  # best effort: never block a call on a lease
        thread = None
        if path is not None:
            thread = threading.Thread(target=beat, name="noema-lane-heartbeat", daemon=True)
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if path is not None:
                with contextlib.suppress(OSError):
                    path.unlink()


# ---------------------------------------------------------------------------
# helpers


def _extract_json(text: str) -> Any:
    cleaned = _FENCE_RE.sub(r"\1", str(text or "")).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ProviderError("model returned text that is not JSON")


def _as_object(parsed: Any, what: str) -> Dict[str, Any]:
    if not isinstance(parsed, Mapping):
        raise ProviderError(f"{what} must be a JSON object")
    return dict(parsed)


def _as_batch(parsed: Any, count: int, what: str) -> List[Dict[str, Any]]:
    if not isinstance(parsed, list) or len(parsed) != count:
        raise ProviderError("{} returned {} items for {} sessions".format(
            what, len(parsed) if isinstance(parsed, list) else "non-list", count))
    out = []
    for item in parsed:
        if not isinstance(item, Mapping):
            raise ProviderError(f"{what} batch item was not an object")
        out.append(dict(item))
    return out


class _JsonLaneMixin:
    """classify / complete_json / classify_batch / count_tokens on top of ``_ask``."""

    name: str
    model: str

    def _ask(self, prompt: str, timeout: float) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def classify(self, session: Any, prompt: str) -> Mapping[str, Any]:
        return _as_object(_extract_json(self._ask(prompt + JSON_ONLY_SUFFIX, self.timeout)),
                          f"{self.name} classification")

    def complete_json(self, prompt: str, max_output_tokens: int = 800,
                      thinking: Optional[str] = None, **_: Any) -> Mapping[str, Any]:
        return _as_object(_extract_json(self._ask(prompt + JSON_ONLY_SUFFIX, self.timeout)),
                          f"{self.name} completion")

    def classify_batch(self, session_ids: List[str], evidences: List[Mapping[str, Any]],
                       timeout_ms: int = 600000) -> List[Mapping[str, Any]]:
        if not session_ids or len(session_ids) != len(evidences):
            raise ProviderError("batch needs matching non-empty sessions and evidence")
        prompt = (
            BATCH_INSTRUCTIONS.format(count=len(session_ids)) + "\nSESSIONS:\n"
            + json.dumps([{"session_id": s, "evidence": dict(e)} for s, e in zip(session_ids, evidences)],
                         ensure_ascii=False, sort_keys=True, default=str)
            + JSON_ONLY_SUFFIX)
        text = self._ask(prompt, max(self.timeout, min(timeout_ms / 1000.0, 600.0)))
        return _as_batch(_extract_json(text), len(session_ids), self.name)

    def count_tokens(self, prompt: str) -> int:
        return GeminiProvider._estimate_tokens(prompt)

    def build_batch_prompt(self, session_ids: List[str], evidences: List[Mapping[str, Any]]) -> str:
        return (BATCH_INSTRUCTIONS.format(count=len(session_ids)) + "\nSESSIONS:\n"
                + json.dumps([{"session_id": s, "evidence": dict(e)} for s, e in zip(session_ids, evidences)],
                             ensure_ascii=False, sort_keys=True, default=str))

    timeout: float = 120.0


# ---------------------------------------------------------------------------
# web LLM hub (Noema's own instance)

Opener = Callable[..., Any]


class HubClient:
    """Minimal client for a web-llm-hub instance (``/v1/chat``, ``/v1/sessions``)."""

    def __init__(self, base_url: str, api_key: Optional[str] = None, opener: Optional[Opener] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self._open = opener or urllib.request.urlopen
        self._busy_cache: Dict[str, Tuple[float, Optional[bool]]] = {}

    def _call(self, method: str, path: str, body: Optional[Mapping[str, Any]], timeout: float) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with self._open(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            with contextlib.suppress(Exception):
                detail = exc.read().decode("utf-8", "replace")[:200]
            raise ProviderError(f"web LLM hub HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ProviderError(f"web LLM hub unreachable: {exc}") from exc
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise ProviderError("web LLM hub returned invalid JSON") from exc

    @staticmethod
    def _find_busy(node: Any, provider: str) -> Optional[bool]:
        if isinstance(node, Mapping):
            if str(node.get("provider") or node.get("name") or "").lower() == provider and "busy" in node:
                return bool(node["busy"])
            hit = node.get(provider)
            if isinstance(hit, Mapping) and "busy" in hit:
                return bool(hit["busy"])
            for value in node.values():
                found = HubClient._find_busy(value, provider)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = HubClient._find_busy(value, provider)
                if found is not None:
                    return found
        return None

    def provider_busy(self, provider: str, max_age: float = 2.0) -> Optional[bool]:
        """True/False from ``GET /v1/sessions``; None when it cannot be determined."""
        now = time.monotonic()
        cached = self._busy_cache.get(provider)
        if cached and now - cached[0] < max_age:
            return cached[1]
        try:
            busy = self._find_busy(self._call("GET", "/v1/sessions", None, 5.0), provider.lower())
        except ProviderError:
            busy = None
        self._busy_cache[provider] = (now, busy)
        return busy

    def chat(self, provider: str, prompt: str, timeout: float) -> str:
        payload = self._call("POST", "/v1/chat", {
            "provider": provider, "prompt": prompt,
            # Always a fresh chat, so this never continues (or becomes) anyone's conversation.
            "new_conversation": True, "timeout_s": int(timeout),
        }, timeout + 15.0)
        text = payload.get("response") if isinstance(payload, Mapping) else None
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("web LLM hub returned no text")
        return text


LANE_REGISTRY: List["_Lane"] = []


class _Lane:
    """Shared status surface for ``GET /api/llm/lanes``."""

    def _register(self) -> None:
        LANE_REGISTRY.append(self)
        self.calls = 0
        self.failures = 0
        self.yields = 0
        self.last_error: Optional[str] = None
        self.last_latency_ms: Optional[float] = None
        self.last_ok_at: Optional[float] = None

    def status(self) -> Dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError


class WebLLMProvider(_JsonLaneMixin, _Lane):
    """Noema's own web-llm-hub instance. Yields to other apps; never stacks requests."""

    name = "web_llm"
    limits = UNMETERED_LIMITS

    def __init__(self, base_url: str, provider: str = "deepseek", timeout: float = 120.0,
                 api_key: Optional[str] = None, coordinator: Optional[LaneCoordinator] = None,
                 client: Optional[HubClient] = None, queue_wait: float = 20.0,
                 realtime_timeout: float = 25.0) -> None:
        self.hub_provider = provider.strip().lower()
        self.model = f"web:{self.hub_provider}"
        self.timeout = float(timeout)
        self.realtime_timeout = float(realtime_timeout)
        self.queue_wait = float(queue_wait)
        self.client = client or HubClient(base_url, api_key)
        self.coordinator = coordinator or LaneCoordinator()
        self.gate = PriorityGate()
        self._register()

    # -- chain hook ---------------------------------------------------------
    def lane_busy(self) -> bool:
        """True when somebody with higher priority is using the web LLM right now."""
        if self.coordinator.rival_active():
            self.yields += 1
            return True
        return self.client.provider_busy(self.hub_provider) is True

    def _ask(self, prompt: str, timeout: float) -> str:
        priority = current_priority()
        budget = min(timeout, self.realtime_timeout) if priority == Priority.REALTIME else timeout
        started = time.perf_counter()
        self.calls += 1
        try:
            with self.gate.acquire(priority, self.queue_wait):
                # Re-check once it is our turn: a rival may have started while we waited.
                if self.lane_busy():
                    raise LaneBusy("web LLM is busy with a higher-priority request")
                with self.coordinator.hold(f"web:{self.hub_provider}"):
                    text = self.client.chat(self.hub_provider, prompt, budget)
            self.last_ok_at = time.time()
            self.last_error = None
            return text
        except LaneBusy:
            self.last_error = "yielded to a higher-priority request"
            raise
        except ProviderError as exc:
            self.failures += 1
            self.last_error = str(exc)[:200]
            raise
        finally:
            self.last_latency_ms = round((time.perf_counter() - started) * 1000, 1)

    def status(self) -> Dict[str, Any]:
        rival = self.coordinator.rival_active()
        return {
            "lane": "web_llm", "model": self.model, "state": (
                f"yielding to {rival}" if rival else "busy" if self.gate.held else "idle"),
            "queued": self.gate.waiting, "calls": self.calls, "failures": self.failures,
            "yields": self.yields, "last_error": self.last_error, "last_latency_ms": self.last_latency_ms,
            "last_rival": self.coordinator.last_rival,
        }


# ---------------------------------------------------------------------------
# Claude Code (own instance per call)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def default_claude_workdir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return (Path(base) / "NOEMA-CLAUDE" if base else Path.home() / ".noema-claude") / "lane"


class ClaudeCodeProvider(_JsonLaneMixin, _Lane):
    """``claude -p`` as a pure text model: own process, own empty cwd, no tools, no history."""

    name = "claude_code"
    limits = UNMETERED_LIMITS

    def __init__(self, model: str = "claude-sonnet-5-5", effort: str = "low", binary: str = "claude",
                 workdir: Optional[Any] = None, timeout: float = 120.0, runner: Optional[Runner] = None,
                 queue_wait: float = 60.0, realtime_timeout: float = 30.0) -> None:
        self.model = model
        self.effort = effort
        self.binary = binary
        self.workdir = Path(workdir) if workdir else default_claude_workdir()
        self.timeout = float(timeout)
        self.realtime_timeout = float(realtime_timeout)
        self.queue_wait = float(queue_wait)
        self._run = runner or subprocess.run
        self.gate = PriorityGate()
        self._register()

    def argv(self) -> List[str]:
        exe = shutil.which(self.binary) or self.binary
        return [exe, "-p", "--model", self.model, "--effort", self.effort, "--tools", "",
                "--no-session-persistence", "--output-format", "json", "--disable-slash-commands"]

    def _ask(self, prompt: str, timeout: float) -> str:
        priority = current_priority()
        budget = min(timeout, self.realtime_timeout) if priority == Priority.REALTIME else timeout
        started = time.perf_counter()
        self.calls += 1
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
            with self.gate.acquire(priority, self.queue_wait):
                try:
                    done = self._run(self.argv(), input=prompt, capture_output=True, text=True, encoding="utf-8",
                                     timeout=budget, cwd=str(self.workdir), check=False)
                except subprocess.TimeoutExpired as exc:
                    raise ProviderError("Claude Code timed out") from exc
                except OSError as exc:
                    raise ProviderError(f"Claude Code could not start: {exc}") from exc
            if done.returncode != 0:
                raise ProviderError("Claude Code exited with {}: {}".format(
                    done.returncode, (done.stderr or done.stdout or "")[:200].strip()))
            try:
                payload = json.loads(done.stdout)
            except json.JSONDecodeError as exc:
                raise ProviderError("Claude Code returned non-JSON output") from exc
            if not isinstance(payload, Mapping) or payload.get("is_error"):
                raise ProviderError("Claude Code reported an error: {}".format(
                    str(payload.get("result") if isinstance(payload, Mapping) else payload)[:200]))
            text = payload.get("result")
            if not isinstance(text, str) or not text.strip():
                raise ProviderError("Claude Code returned no text")
            self.last_ok_at = time.time()
            self.last_error = None
            return text
        except ProviderError as exc:
            if not isinstance(exc, LaneBusy):
                self.failures += 1
            self.last_error = str(exc)[:200]
            raise
        finally:
            self.last_latency_ms = round((time.perf_counter() - started) * 1000, 1)

    def status(self) -> Dict[str, Any]:
        return {
            "lane": "claude_code", "model": f"{self.model} · {self.effort}",
            "state": "busy" if self.gate.held else "idle", "queued": self.gate.waiting,
            "calls": self.calls, "failures": self.failures, "yields": 0,
            "last_error": self.last_error, "last_latency_ms": self.last_latency_ms, "last_rival": None,
        }


class _RealtimeView:
    """Same lane, but every call runs at REALTIME priority (nudge reasoning, meme choice)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.name = inner.name
        self.model = inner.model
        self.limits = inner.limits

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    def complete_json(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        with lane_priority(Priority.REALTIME):
            return self._inner.complete_json(*args, **kwargs)

    def classify(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        with lane_priority(Priority.REALTIME):
            return self._inner.classify(*args, **kwargs)


def realtime_view(provider: Any) -> Any:
    return _RealtimeView(provider)


def lane_status() -> List[Dict[str, Any]]:
    out = []
    for lane in LANE_REGISTRY:
        with contextlib.suppress(Exception):
            out.append(lane.status())
    return out


def build_external_providers(config: Any) -> List[Any]:
    """Web LLM first, then Claude Code. Returns [] unless explicitly enabled."""
    providers: List[Any] = []
    coordinator = LaneCoordinator(
        lane_dir=getattr(config, "llm_lane_dir", None) or None,
        rivals=tuple(getattr(config, "llm_lane_rivals", None) or ("quaero",)),
        stale_seconds=float(getattr(config, "llm_lane_stale_seconds", 30.0)),
    )
    if getattr(config, "webllm_enabled", False):
        providers.append(WebLLMProvider(
            base_url=str(config.webllm_url), provider=str(config.webllm_provider),
            timeout=float(config.webllm_timeout_seconds), coordinator=coordinator,
            api_key=os.environ.get(str(getattr(config, "webllm_api_key_env", "") or "")) or None))
    if getattr(config, "claude_code_enabled", False):
        providers.append(ClaudeCodeProvider(
            model=str(config.claude_code_model), effort=str(config.claude_code_effort),
            binary=str(config.claude_code_binary), timeout=float(config.claude_code_timeout_seconds)))
    return providers
