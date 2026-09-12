"""Small dependency-free client for Ollama's local generate endpoint."""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class OllamaError(ConnectionError):
    """Raised when local Ollama cannot produce a response."""


class OllamaClient:
    """Call Ollama locally; no cloud service or credential is involved."""

    # llama3.2:3b reports a 131072-token architecture context window
    # (verified via ``ollama show``), but Ollama serves generate requests
    # with a much smaller default ``num_ctx`` (4096) unless the caller sets
    # ``options.num_ctx``. A full 128k KV cache does not fit a typical
    # laptop alongside the 2GB Q4_K_M weights, so the live pipeline uses a
    # fixed 8192-token working window: large enough for a session plus its
    # surrounding evidence, small enough to stay reliable. Prompts are
    # budgeted by the classifier to fit inside this window; anything larger
    # is chunked, never silently truncated.
    MODEL_CONTEXT_TOKENS = 131072
    WORKING_NUM_CTX = 8192
    DEFAULT_OPTIONS: Mapping[str, Any] = {
        "temperature": 0,
        "num_ctx": WORKING_NUM_CTX,
        "num_predict": 600,
    }

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "llama3.2:3b",
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        # Last-response usage for observability (read via take_usage).
        # Reset at every call start so failures never inherit stale counts.
        self._last_usage: Optional[Dict[str, Any]] = None

    def generate_json(
        self,
        prompt: str,
        model: Optional[str] = None,
        options: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        merged = dict(self.DEFAULT_OPTIONS)
        if options:
            merged.update(dict(options))
        self._last_usage = None
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": merged,
        }
        request = Request(
            self.base_url + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise OllamaError("could not reach local Ollama at {}".format(self.base_url)) from exc
        except json.JSONDecodeError as exc:
            raise OllamaError("Ollama returned invalid JSON") from exc

        if isinstance(response_payload, dict):
            # Stash exact usage for observability; never affects the payload.
            prompt_count = response_payload.get("prompt_eval_count")
            eval_count = response_payload.get("eval_count")
            eval_ns = response_payload.get("eval_duration")
            total_ns = response_payload.get("total_duration")
            if isinstance(prompt_count, int) or isinstance(eval_count, int):
                total = None
                if isinstance(prompt_count, int) and isinstance(eval_count, int):
                    total = prompt_count + eval_count
                self._last_usage = {
                    "input_tokens": prompt_count if isinstance(prompt_count, int) else None,
                    "output_tokens": eval_count if isinstance(eval_count, int) else None,
                    "total_tokens": total,
                    "exact": True,
                    "generation_ms": (eval_ns / 1e6) if isinstance(eval_ns, (int, float)) else None,
                    "total_duration_ms": (total_ns / 1e6) if isinstance(total_ns, (int, float)) else None,
                }

        raw = response_payload.get("response") if isinstance(response_payload, dict) else None
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            raise OllamaError("Ollama response did not contain a JSON result")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OllamaError("Ollama response was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise OllamaError("Ollama JSON result must be an object")
        return parsed

    def count_tokens(self, prompt: str) -> int:
        """Count tokens using Ollama's model-specific runtime tokenizer."""
        request = Request(
            self.base_url + "/api/tokenize",
            data=json.dumps({"model": self.model, "content": prompt}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code != 404:
                raise OllamaError("Ollama tokenizer unavailable") from exc
            return self._count_tokens_dry_run(prompt)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise OllamaError("Ollama tokenizer unavailable") from exc
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        if not isinstance(tokens, list):
            raise OllamaError("Ollama tokenizer returned no tokens")
        return len(tokens)

    def _count_tokens_dry_run(self, prompt: str) -> int:
        """Use a zero-output generation to obtain prompt_eval_count."""
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": self.WORKING_NUM_CTX, "num_predict": 0},
        }
        request = Request(
            self.base_url + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise OllamaError("Ollama dry-run token count failed") from exc
        count = result.get("prompt_eval_count") if isinstance(result, dict) else None
        if not isinstance(count, int):
            raise OllamaError("Ollama did not return prompt_eval_count")
        return count
