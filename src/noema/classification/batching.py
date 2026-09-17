"""Deterministic batch planning and item validation (moved verbatim).

Pure functions: entries in, groups/decisions out. No database, no model
calls, no quota state — the provider layer supplies budgets and calls
these planners before sending anything.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Tuple

from noema.classification.contracts import BATCH_INSTRUCTIONS

BATCH_OUTPUT_RESERVE_PER_ITEM = 500
BATCH_MAX_OUTPUT_TOKENS = 65536


def pack_batches(entries: List[Mapping[str, Any]], provider: Any,
                 budget: int) -> Tuple[List[tuple], List[Dict[str, Any]]]:
    """Group sessions to exploit the model's full usable window.

    Fill order is chronological (synthesis weighs recency). Item sizes
    come from text length first (chars//4, one accurate-ish estimate),
    then each assembled group is measured EXACTLY once with the model's
    real tokenizer; oversized groups shed their tail until they fit.
    Groups are only ever split for size — never pre-split small.
    Returns ``(groups, pack_stats)`` where groups is
    ``[(group, measured_tokens_or_None)]``.
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
    return groups, pack_stats

def valid_item(payload: Any, session_id: Any) -> bool:
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
