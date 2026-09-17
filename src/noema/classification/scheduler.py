"""Classification scheduler driver: deterministic job-boundary policy.

This module owns the pure scheduling math for semantic classification:
which window the 20-minute job covers, which completed 5-minute bucket
the batch row names, and how pending units are ordered and bounded per
run. No database, no model calls, no quota spend, no lifecycle.

Direction: ``runtime.daemon`` calls into this module; this module never
imports the daemon, the pipeline, or API code. Cadence (20 minutes),
cursor semantics, and max-per-run bounding are preserved verbatim from
the daemon implementation — this is a physical move, not a redesign.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Tuple


def classification_window(
    last_analysis: Optional[datetime],
    now: datetime,
    interval_seconds: float,
) -> Tuple[datetime, datetime]:
    """(start, end) evidence window for one semantic job.

    Moved verbatim from the daemon: the job covers stored evidence
    since the last analysis cursor, defaulting to one interval back,
    clamped so a future cursor can never produce an empty window.
    """
    start = last_analysis or (now - timedelta(seconds=interval_seconds))
    if start >= now:
        start = now - timedelta(seconds=interval_seconds)
    return start, now


def completed_window(now: datetime) -> Tuple[datetime, datetime]:
    """The completed 5-minute bucket the batch row is keyed on."""
    completed_end = now.replace(second=0, microsecond=0) - timedelta(
        minutes=now.minute % 5
    )
    return completed_end - timedelta(minutes=5), completed_end


def build_batch_id(completed_start: datetime, completed_end: datetime) -> str:
    """Deterministic batch-row id for one completed window."""
    return "batch_" + hashlib.sha256(
        (completed_start.isoformat() + "|" + completed_end.isoformat()).encode(
            "utf-8"
        )
    ).hexdigest()[:20]


def deduplicate_candidates(primary: Iterable, overflow: Iterable) -> list:
    """Merge window + backlog scans into one id-deduplicated candidate list.

    Each scan is chronological (the synthesis weighs recency); the merge
    keeps first-seen order so window rows precede backlog rows and no id
    is ever queued twice in one run.
    """
    seen_ids: set = set()
    candidates = []
    for candidate in sorted(primary, key=lambda item: item.start_time):
        if candidate.id not in seen_ids:
            seen_ids.add(candidate.id)
            candidates.append(candidate)
    for candidate in sorted(overflow, key=lambda item: item.start_time):
        if candidate.id not in seen_ids:
            seen_ids.add(candidate.id)
            candidates.append(candidate)
    return candidates


def select_chunk(pending: List, max_per_run: int) -> list:
    """Bound one run to the newest ``max_per_run`` pending units.

    Newest-first so fresh activity classifies quickly while older
    remainder continues on later cadences. Failures keep retryable
    state and never abort the chunk (enforced by the caller).
    """
    ordered = sorted(pending, key=lambda item: item.start_time, reverse=True)
    return ordered[: max(1, int(max_per_run))]
