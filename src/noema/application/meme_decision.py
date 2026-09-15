"""Gemini-informed meme decisions over the curated asset catalog (V4).

Pipeline position: AFTER intervention reasoning decided to intervene with
a meme-bearing class, BEFORE the deterministic policy gate. Three bounded
steps, each with its own failure semantics:

1. ``retrieve_meme_candidates`` — deterministic local retrieval over
   ``meme_assets``. No model call. Sentiment is one feature among many,
   never a suitability verdict.
2. ``MemeRanker.rank`` — at most ONE reasoning-tier call picks an asset id
   from the candidate set. Unknown ids are rejected; the deterministic
   top candidate is the fallback. Never loops.
3. ``generate_intervention_copy`` — at most ONE reasoning-tier call drafts
   the short contextual line under hard constraints. Any failure falls
   back to the curated response template (never a fabricated meme).

Effectiveness data joins responses by asset id with a minimum-sample
guard: tiny samples stay "insufficient evidence", never fake rates.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: Maximum candidates handed to the ranking model. Bounded so prompts
#: stay small and ranking stays a single cheap call.
MEME_CANDIDATE_LIMIT = 20

#: Minimum direct shows before an asset's recovery rate may influence
#: reasoning. Below this, effectiveness is "insufficient evidence".
MIN_DIRECT_SAMPLES = 5

#: Copy length cap: overlays are glanceable, not paragraphs.
COPY_MAX_CHARS = 140

#: Sentiment compatibility per tone family. A soft feature: matching
#: sentiment adds weight, mismatching never excludes. Sentiment is
#: dataset metadata, not intervention suitability.
TONE_SENTIMENT_AFFINITY = {
    "playful": {"positive", "neutral"},
    "sarcastic": {"negative", "neutral"},
    "supportive": {"positive", "neutral"},
    "gentle": {"positive", "neutral"},
    "firm": {"negative", "neutral", "positive"},
}


def _tokens(value: Any) -> List[str]:
    tokens: List[str] = []
    for token in str(value or "").lower().split():
        cleaned = "".join(char for char in token if char.isalnum())
        if len(cleaned) >= 3 and cleaned not in tokens:
            tokens.append(cleaned)
    return tokens


def _asset_text(asset: Mapping[str, Any]) -> str:
    parts = [
        asset.get("ocr_text") or "",
        asset.get("corrected_text") or "",
        asset.get("filename") or "",
        " ".join(asset.get("tags") or []),
    ]
    return " ".join(part for part in parts if part)


def score_meme_candidate(asset: Mapping[str, Any], intent_words: List[str],
                         tone: str,
                         effectiveness: Optional[Mapping[str, Any]] = None) -> float:
    """Deterministic fit score for one asset. Pure function, no I/O."""
    text_tokens = set(_tokens(_asset_text(asset)))
    overlap = 0.0
    if intent_words:
        hits = sum(1 for word in intent_words if word in text_tokens)
        overlap = hits / max(1, len(intent_words))
    affinity = TONE_SENTIMENT_AFFINITY.get(str(tone or "").strip().lower(), set())
    sentiment = str(asset.get("sentiment") or "").strip().lower()
    sentiment_match = 1.0 if sentiment and sentiment in affinity else 0.0
    favorite = 1.0 if asset.get("favorite") else 0.0
    curated = 1.0 if int(asset.get("response_count") or 0) > 0 else 0.0
    if effectiveness is not None:
        shown = int(effectiveness.get("times_shown") or 0)
        if shown >= MIN_DIRECT_SAMPLES:
            rate = effectiveness.get("recovery_rate")
            try:
                evidence = min(1.0, max(0.0, float(rate))) if rate is not None else 0.5
            except (TypeError, ValueError):
                evidence = 0.5
        else:
            # Insufficient evidence: neutral weight, never a fake rate.
            evidence = 0.5
    else:
        evidence = 0.5
    return round(
        0.40 * min(1.0, overlap) + 0.20 * sentiment_match + 0.15 * favorite
        + 0.10 * curated + 0.15 * evidence, 4)


def retrieve_meme_candidates(
    store: Any,
    meme_intent: Optional[str] = None,
    tone: Optional[str] = None,
    severity: int = 3,
    limit: int = MEME_CANDIDATE_LIMIT,
    search_terms: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Retrieve candidate assets deterministically (no model call).

    Scans enabled assets (paged), scores each against intent keywords +
    tone affinity + curation + effectiveness, and returns the top ``limit``
    as metadata-only dicts (id, filename, OCR snippet, sentiment, tags,
    effectiveness summary). Image bytes never leave the loopback server.
    """
    _ = severity  # Reserved for future severity-weighted scoring.
    intent_words: List[str] = []
    for source in [meme_intent, " ".join(search_terms or [])]:
        intent_words.extend(word for word in _tokens(source) if word not in intent_words)
    tone_key = str(tone or "").strip().lower()
    query = getattr(store, "query_meme_assets", None)
    if not callable(query):
        return []
    pool: List[Dict[str, Any]] = []
    offset = 0
    page = 200
    while True:
        try:
            result = query(search=None, sentiment=None, status="all",
                           limit=page, offset=offset)
        except (AttributeError, OSError, TypeError, ValueError):
            break
        items = result.get("items", []) if isinstance(result, dict) else []
        if not items:
            break
        for asset in items:
            if isinstance(asset, Mapping):
                item = dict(asset)
            else:
                item = {key: getattr(asset, key, None) for key in (
                    "id", "filename", "ocr_text", "corrected_text",
                    "sentiment", "tags", "favorite", "enabled",
                    "response_count")}
            if item.get("enabled") is False:
                continue
            pool.append(item)
        if len(items) < page:
            break
        offset += len(items)
        if len(pool) >= 2000:
            break
    effectiveness_of = getattr(store, "response_effectiveness", None)
    eff_rows: List[Dict[str, Any]] = []
    if callable(effectiveness_of):
        try:
            eff_rows = list(effectiveness_of() or [])
        except (AttributeError, OSError, TypeError, ValueError):
            eff_rows = []
    # Attribute response effectiveness back to assets for the ranking
    # context. Only direct, sufficiently-sampled rows count.
    asset_eff: Dict[str, Dict[str, Any]] = {}
    responses_of = getattr(store, "query_responses", None)
    if callable(responses_of):
        try:
            by_id = {row.get("id"): row for row in eff_rows
                     if isinstance(row, Mapping)}
            for response in responses_of(enabled_only=False):
                rid = getattr(response, "id", None)
                aid = getattr(response, "asset_id", None)
                row = by_id.get(rid, {})
                shown = int(row.get("times_shown") or 0)
                if aid and shown >= MIN_DIRECT_SAMPLES:
                    asset_eff[str(aid)] = {
                        "shown": shown,
                        "direct": int(row.get("recovery_count") or 0),
                        "rate": row.get("recovery_rate"),
                    }
        except (AttributeError, OSError, TypeError, ValueError):
            asset_eff = {}
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for asset in pool:
        aid = str(asset.get("id") or "")
        if not aid:
            continue
        score = score_meme_candidate(asset, intent_words, tone_key,
                                     asset_eff.get(aid))
        scored.append((score, {
            "id": aid,
            "filename": asset.get("filename"),
            "ocr_snippet": str(asset.get("corrected_text")
                               or asset.get("ocr_text") or "")[:200],
            "sentiment": asset.get("sentiment"),
            "tags": list(asset.get("tags") or []),
            "favorite": bool(asset.get("favorite")),
            "score": score,
            "effectiveness": asset_eff.get(aid),
        }))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return [item for _, item in scored[:max(1, int(limit or MEME_CANDIDATE_LIMIT))]]


@dataclass(frozen=True)
class MemeRanking:
    """Outcome of one bounded meme-ranking attempt."""

    selected_asset_id: Optional[str]
    confidence: float
    reason: str
    alternatives: Tuple[str, ...]
    provider: Optional[str]
    model: Optional[str]
    latency_ms: Optional[float]
    failure_kind: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "selected_asset_id": self.selected_asset_id,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "alternatives": list(self.alternatives),
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "failure_kind": self.failure_kind,
        }


MEME_RANK_INSTRUCTIONS = (
    "You rank meme candidates for a focus intervention. Reply with a "
    "SINGLE JSON object and nothing else: "
    '{"selected_asset_id":"<id exactly as listed>",'
    '"confidence":0.0-1.0,'
    '"reason":"under 25 words, citing listed metadata only",'
    '"alternatives":["<id>", "...up to 3"]}. '
    "Select ONLY from the listed candidate ids. Never invent an id, "
    "activity, goal, or URL. No chain-of-thought. No prose."
)

COPY_INSTRUCTIONS = (
    "You write ONE short intervention line for a focus overlay. Use ONLY "
    "the supplied goal, activity, and minutes. Never invent domains, URLs, "
    "durations, user statements, or facts. Keep it under {limit} "
    "characters, plain text, no quotes around the whole line. Reply with "
    "a SINGLE JSON object and nothing else: "
    '\'{{"copy":"the line"}}\'. No prose.'
)

_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def _validate_ranking(payload: Any, candidate_ids: List[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return None
    selected = str(payload.get("selected_asset_id", "")).strip()
    if selected not in candidate_ids:
        # Unknown id: reject outright. The caller falls back to the
        # deterministic top candidate — never an invented asset.
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
    alternatives = payload.get("alternatives", [])
    if alternatives is None:
        alternatives = []
    if not isinstance(alternatives, list):
        return None
    clean = [str(item).strip() for item in alternatives[:3]
             if str(item).strip() in candidate_ids
             and str(item).strip() != selected]
    return {
        "selected_asset_id": selected,
        "confidence": confidence,
        "reason": reason.strip()[:200],
        "alternatives": clean,
    }


def _validate_copy(payload: Any, limit: int) -> Optional[str]:
    if not isinstance(payload, Mapping):
        return None
    copy = payload.get("copy", "")
    if not isinstance(copy, str) or not copy.strip():
        return None
    copy = " ".join(copy.strip().split())
    if len(copy) > limit or _URL_RE.search(copy):
        return None
    return copy


class MemeDecider:
    """Bounded ranking + copy calls over reasoning-tier legs.

    At most one ranking call and one copy call per decision cycle — never
    a loop. Caches rank results per (goal, severity, episode, candidates,
    version) so retries converge.
    """

    _CACHE_MAX = 128

    def __init__(self, legs: Optional[List[Any]] = None,
                 max_output_tokens: int = 300,
                 observer: Any = None):
        self.legs = list(legs or [])
        self.max_output_tokens = int(max_output_tokens)
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        self._observer = observer
        self._rank_cache: "OrderedDict[Tuple[str, ...], MemeRanking]" = OrderedDict()

    def _rank_key(self, goal: Any, severity: Any, episode_id: Any,
                  candidate_ids: List[str]) -> Tuple[str, ...]:
        import hashlib as _hashlib

        goal_hash = _hashlib.sha256(str(goal or "").encode("utf-8")).hexdigest()[:16]
        return (goal_hash, str(severity), str(episode_id or ""),
                ",".join(sorted(candidate_ids)), "meme-rank-v1")

    def _emit_invocation(self, **fields: Any) -> bool:
        observer = self._observer
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

    def _call_once(self, prompt: str, purpose: str, operation: str,
                   detection_id: Optional[str] = None,
                   validate: Any = None):
        """Try legs in order; return (payload|None, failure_kind, attempts).

        A leg counts as spent once it answers. Transport errors advance to
        the next leg; payloads failing ``validate`` (when given) do too —
        a malformed answer from one model must not veto the rest.
        """
        from noema.infrastructure.providers import FailureKind, classify_failure
        from noema.observability.models import (
            Pipeline,
            new_request_id,
        )
        from noema.observability.provider_telemetry import (
            quota_scope_for,
            take_usage,
        )

        request_id = new_request_id()
        request_started = time.perf_counter()
        attempts: List[Tuple[str, Optional[str]]] = []
        previous: Optional[tuple] = None
        legs = [leg for leg in self.legs if leg is not None]
        if not legs:
            return None, FailureKind.UNAVAILABLE, attempts
        for provider in legs:
            name = str(getattr(provider, "name", ""))
            model = getattr(provider, "model", None)
            attempts.append((name, model))
            complete = getattr(provider, "complete_json", None)
            if not callable(complete):
                previous = (name, "provider has no JSON completion path")
                continue
            started = time.perf_counter()
            try:
                payload = complete(prompt, self.max_output_tokens)
                call_error = None
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
            except Exception as exc:
                call_error = exc
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                payload = {}
            from datetime import datetime, timezone

            stamp = datetime.now(timezone.utc)
            self._emit_invocation(
                request_id=request_id,
                timestamp_iso=stamp.isoformat().replace("+00:00", "Z"),
                timestamp_ms=int(stamp.timestamp() * 1000),
                provider_name=name, model=model, purpose=purpose,
                pipeline=Pipeline.INTERVENTION, operation=operation,
                kind="attempt", session_ids=[],
                batch_size=0, attempt_number=0,
                fallback_depth=max(0, len(attempts) - 1),
                fallback_from=previous[0] if previous else None,
                fallback_reason=str(previous[1])[:300] if previous else None,
                usage=take_usage(provider) if call_error is None else None,
                estimated_input_tokens=None,
                error=call_error,
                success_payload=(call_error is None),
                latency_ms=latency_ms,
                context_window=None, max_output_tokens=self.max_output_tokens,
                quota_scope=quota_scope_for(name),
                quota_before={}, quota_after={},
                detection_id=detection_id,
            )
            if call_error is not None:
                previous = (name, call_error)
                continue
            if validate is not None:
                try:
                    valid = validate(payload)
                except Exception:
                    valid = None
                if valid is None:
                    previous = (name, "malformed model output")
                    continue
            return payload, None, attempts
        if previous is not None and isinstance(previous[1], BaseException):
            failure_kind = classify_failure(previous[1])
        elif previous is not None:
            failure_kind = FailureKind.INVALID_OUTPUT
        else:
            failure_kind = FailureKind.UNAVAILABLE
        return None, failure_kind, attempts

    def rank(self, candidates: List[Mapping[str, Any]],
             goal: Any = None, severity: Any = 3,
             episode_id: Any = None,
             detection_id: Optional[str] = None) -> MemeRanking:
        """Rank candidates with one bounded model call, else deterministic."""
        from noema.infrastructure.providers import FailureKind
        from noema.observability.models import Purpose

        candidate_ids = [str(item.get("id") or "") for item in candidates
                         if isinstance(item, Mapping) and str(item.get("id") or "")]
        if not candidate_ids:
            return MemeRanking(
                selected_asset_id=None, confidence=0.0,
                reason="no candidates available",
                alternatives=(), provider=None, model=None,
                latency_ms=None, failure_kind=FailureKind.UNAVAILABLE)
        key = self._rank_key(goal, severity, episode_id, candidate_ids)
        cached = self._rank_cache.get(key)
        if cached is not None:
            return cached
        prompt = (MEME_RANK_INSTRUCTIONS + "\nCANDIDATES:\n" + json.dumps(
            [{"id": item.get("id"), "ocr": str(item.get("ocr_snippet") or "")[:200],
              "sentiment": item.get("sentiment"),
              "tags": list(item.get("tags") or [])[:8],
              "effectiveness": item.get("effectiveness")}
             for item in candidates if isinstance(item, Mapping)],
            ensure_ascii=False, sort_keys=True, default=str))
        started = time.perf_counter()
        payload, failure_kind, attempts = self._call_once(
            prompt, Purpose.MEME_SELECTION, "MEME_RANK",
            detection_id=detection_id,
            validate=lambda item: _validate_ranking(item, candidate_ids))
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        verdict = _validate_ranking(payload, candidate_ids) if payload is not None else None
        if verdict is None:
            # Deterministic fallback: top retrieved candidate. Any failure
            # (transport, malformed, unknown id) lands here — never a loop.
            fallback_kind = failure_kind or FailureKind.INVALID_OUTPUT
            ranking = MemeRanking(
                selected_asset_id=candidate_ids[0], confidence=0.0,
                reason="deterministic fallback to top retrieved candidate",
                alternatives=tuple(candidate_ids[1:4]),
                provider=(attempts[-1][0] if attempts else None),
                model=(attempts[-1][1] if attempts else None),
                latency_ms=latency_ms, failure_kind=fallback_kind)
        else:
            ranking = MemeRanking(
                selected_asset_id=verdict["selected_asset_id"],
                confidence=verdict["confidence"], reason=verdict["reason"],
                alternatives=tuple(verdict["alternatives"]),
                provider=(attempts[-1][0] if attempts else None),
                model=(attempts[-1][1] if attempts else None),
                latency_ms=latency_ms, failure_kind=None)
        self._rank_cache[key] = ranking
        while len(self._rank_cache) > self._CACHE_MAX:
            self._rank_cache.popitem(last=False)
        return ranking

    def generate_copy(self, goal: Any = None, activity: Any = None,
                      minutes_away: Any = None, tone: Any = None,
                      detection_id: Optional[str] = None,
                      limit: int = COPY_MAX_CHARS) -> Optional[str]:
        """Draft one constrained contextual line, or None on any failure."""
        from noema.observability.models import Purpose

        context = {
            "goal": str(goal or "your goal")[:200],
            "activity": str(activity or "off-goal activity")[:200],
            "minutes_away": minutes_away,
            "tone": str(tone or "neutral")[:32],
        }
        prompt = (COPY_INSTRUCTIONS.format(limit=int(limit))
                  + "\nCONTEXT:\n" + json.dumps(
                      context, ensure_ascii=False, sort_keys=True, default=str))
        payload, _, _ = self._call_once(
            prompt, Purpose.INTERVENTION_COPY, "INTERVENTION_COPY",
            detection_id=detection_id,
            validate=lambda item: _validate_copy(item, int(limit)))
        if payload is None:
            return None
        return _validate_copy(payload, int(limit))
