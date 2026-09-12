"""Local embedding helpers with token-managed chunking.

Target model is ``nomic-embed-text`` served by local Ollama. That model is
embed-only: ``/api/generate`` answers 400 and ``/api/tokenize`` answers 404,
so token budgeting here uses a conservative character-based estimate
instead of the runtime tokenizer. Chunking splits on paragraph and then
sentence boundaries (never mid-word), packs to a token budget with a small
overlap, and batches each document's chunks into a single ``/api/embed``
call. An optional ``tracker`` (``ProviderChain.track_embedding``)
records estimated tokens in the shared quota ledger.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.request import Request, urlopen

from noema.infrastructure.ollama import OllamaError

EMBED_MODEL = "nomic-embed-text"
MODEL_CONTEXT_TOKENS = 8192
DEFAULT_CHUNK_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 50
EST_CHARS_PER_TOKEN = 4

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def estimate_tokens(text: str) -> int:
    """Conservative token estimate when the runtime tokenizer is unavailable."""
    if not text:
        return 0
    return max(1, (len(text) + EST_CHARS_PER_TOKEN - 1) // EST_CHARS_PER_TOKEN)


def _split_sentences(paragraph: str) -> List[str]:
    parts = _SENTENCE_SPLIT_RE.split(paragraph.strip())
    return [part.strip() for part in parts if part.strip()] or [paragraph.strip()]


def chunk_text(text: str, max_tokens: int = DEFAULT_CHUNK_TOKENS,
               overlap_tokens: int = DEFAULT_OVERLAP_TOKENS) -> List[str]:
    """Split text into token-budgeted chunks with sentence overlap.

    Paragraphs are kept whole when they fit; oversized paragraphs fall back
    to sentence packing. Each chunk (except the first) starts with trailing
    sentences from the previous chunk up to ``overlap_tokens`` so context
    survives chunk boundaries. Always returns at least one chunk.
    """
    max_tokens = max(1, int(max_tokens))
    overlap_tokens = max(0, int(overlap_tokens))
    cleaned = (text or "").strip()
    if not cleaned:
        return [""]
    if estimate_tokens(cleaned) <= max_tokens:
        return [cleaned]
    units: List[str] = []
    for paragraph in re.split(r"\n\s*\n", cleaned):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if estimate_tokens(paragraph) <= max_tokens:
            units.append(paragraph)
        else:
            units.extend(_split_sentences(paragraph))
    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = estimate_tokens(unit)
        if unit_tokens > max_tokens:
            # Single unit exceeds budget: hard-split on whitespace.
            words = unit.split()
            piece, piece_tokens = [], 0
            for word in words:
                word_tokens = estimate_tokens(word + " ")
                if piece and piece_tokens + word_tokens > max_tokens:
                    if current:
                        chunks.append(" ".join(current))
                        current, current_tokens = [], 0
                    chunks.append(" ".join(piece))
                    piece, piece_tokens = [word], word_tokens
                else:
                    piece.append(word)
                    piece_tokens += word_tokens
            if piece:
                if current_tokens + piece_tokens > max_tokens and current:
                    chunks.append(" ".join(current))
                    current, current_tokens = [], 0
                current.append(" ".join(piece))
                current_tokens += piece_tokens
            continue
        if current and current_tokens + unit_tokens > max_tokens:
            chunks.append(" ".join(current))
            overlap = _tail_overlap(chunks[-1], overlap_tokens)
            current = list(overlap)
            current_tokens = sum(estimate_tokens(s) for s in current)
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        chunks.append(" ".join(current))
    return chunks or [cleaned]


def _tail_overlap(text: str, overlap_tokens: int) -> List[str]:
    if overlap_tokens <= 0 or not text:
        return []
    sentences = _split_sentences(text)
    picked: List[str] = []
    total = 0
    for sentence in reversed(sentences):
        tokens = estimate_tokens(sentence)
        if picked and total + tokens > overlap_tokens:
            break
        picked.append(sentence)
        total += tokens
    return list(reversed(picked))


def embed_texts(texts: List[str], model: str = EMBED_MODEL,
                base_url: str = "http://127.0.0.1:11434", timeout: float = 120.0,
                urlopen_fn: Optional[Callable[..., Any]] = None,
                tracker: Optional[Any] = None,
                recorder: Optional[Any] = None) -> List[List[float]]:
    """Embed texts in one ``/api/embed`` call; optionally track quota usage."""
    import time as _time
    from datetime import datetime, timezone

    from noema.observability.models import new_request_id
    from noema.observability.provider_telemetry import (
        ProviderResponseMetadata,
        assemble_invocation,
    )

    started = _time.perf_counter()
    opener = urlopen_fn or urlopen
    request = Request(
        base_url.rstrip("/") + "/api/embed",
        data=json.dumps({"model": model, "input": list(texts)}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    call_error: Optional[Exception] = None
    payload: Any = None
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        call_error = exc
    finally:
        if recorder is not None:
            try:
                now = datetime.now(timezone.utc)
                exact_in = None
                if isinstance(payload, dict):
                    count = payload.get("prompt_eval_count")
                    exact_in = count if isinstance(count, int) else None
                usage = None
                if exact_in is not None:
                    usage = ProviderResponseMetadata(
                        input_tokens=exact_in, exact=True)
                record = assemble_invocation(
                    request_id=new_request_id(),
                    timestamp_iso=now.isoformat().replace("+00:00", "Z"),
                    timestamp_ms=int(now.timestamp() * 1000),
                    provider_name="ollama", model=model,
                    purpose="EMBEDDING", pipeline="EMBEDDING",
                    operation="EMBED", kind="attempt",
                    batch_size=len(list(texts)),
                    usage=usage,
                    estimated_input_tokens=(
                        None if exact_in is not None
                        else sum(estimate_tokens(item) for item in texts)),
                    error=call_error, success_payload=call_error is None,
                    latency_ms=round((_time.perf_counter() - started) * 1000, 1),
                    context_window=MODEL_CONTEXT_TOKENS,
                    quota_scope="local",
                )
                recorder.record_invocation(record)
            except Exception:
                pass
    if call_error is not None:
        raise OllamaError(
            "local embedding request failed: {}".format(call_error)) from call_error
    embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise OllamaError("embedding response did not match input count")
    if tracker is not None:
        try:
            tracker(model, sum(estimate_tokens(item) for item in texts))
        except Exception:
            pass  # Tracking must never break embedding output.
    return [list(vector) for vector in embeddings]


def embed_chunks(text: str, max_tokens: int = DEFAULT_CHUNK_TOKENS,
                 overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
                 model: str = EMBED_MODEL, base_url: str = "http://127.0.0.1:11434",
                 timeout: float = 120.0,
                 urlopen_fn: Optional[Callable[..., Any]] = None,
                 tracker: Optional[Any] = None) -> Dict[str, Any]:
    """Chunk text with token management, then embed every chunk."""
    chunks = chunk_text(text, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
    vectors = embed_texts(chunks, model=model, base_url=base_url, timeout=timeout,
                          urlopen_fn=urlopen_fn, tracker=tracker)
    return {"model": model, "chunks": chunks, "embeddings": vectors,
            "estimated_tokens": sum(estimate_tokens(chunk) for chunk in chunks)}
