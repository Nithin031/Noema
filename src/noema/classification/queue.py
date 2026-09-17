"""Classification work-queue: eligibility, retry, member expansion.

Deterministic scheduling policy for semantic classification: which
units need verdicts now, how member items expand from episodes, and how
retry/backoff is stamped. No model calls, no quota spend here — this
module only decides work; the classifier and providers execute it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from noema.classification.core import Classification
from noema.domain.activity import coerce_timestamp

# Retry policy for failed classification attempts. Delays grow as
# 10/20/40/80/120 minutes and stop after max_retries attempts; the row
# then stays terminally failed with its last error.
RETRY_BACKOFF_BASE_MINUTES = 5.0
RETRY_BACKOFF_MAX_MINUTES = 120.0
# Default classifier contract version and retry cap applied when callers
# do not pin their own. Single source of truth; pipeline signatures
# reference these names.
CLASSIFICATION_RETRIES_MAX = 5
CLASSIFIER_VERSION_DEFAULT = "1"
# Member expansion bound (batched per-member classification). One
# episode's expansion never exceeds a daemon batch so a giant episode
# cannot wedge a cycle; uncovered remainder converges on later cycles
# because member coverage feeds queue eligibility. Sibling context per
# member is capped separately so prompts stay bounded.
MAX_MEMBER_ITEMS_PER_EPISODE = 20
MAX_MEMBER_SIBLINGS = 10


def retry_delay_minutes(retry_count: int) -> float:
    """Minutes to wait before attempt number ``retry_count`` (1-based)."""

    return min(
        RETRY_BACKOFF_MAX_MINUTES,
        RETRY_BACKOFF_BASE_MINUTES * (2.0 ** max(1, int(retry_count))),
    )

def apply_retry_accounting(
    result: Classification,
    existing: Optional[Classification],
    now: datetime,
    classifier_version: str,
    max_retries: int,
) -> Classification:
    """Stamp retry/version bookkeeping onto one classification result.

    Success resets the counters. Failure increments the attempt count,
    keeps the error text from the classifier (or a generic fallback),
    and schedules the next eligible retry with backoff. Terminal rows
    (attempts exhausted under the current version) keep their error and
    are never silently dropped.
    """

    if result.classification_status == "classified":
        return replace(
            result,
            retry_count=0,
            last_error=None,
            next_retry_at=None,
            classifier_version=classifier_version,
        )
    previous = int(getattr(existing, "retry_count", 0) or 0)
    count = previous + 1
    if result.classification_status == "classification_failed":
        error = result.last_error or "model response failed validation"
    else:
        error = result.last_error or "provider unavailable"
    if count >= max(1, int(max_retries)):
        next_retry = None
    else:
        next_retry = now + timedelta(minutes=retry_delay_minutes(count))
    version = getattr(existing, "classifier_version", None) or classifier_version
    return replace(
        result,
        retry_count=count,
        last_error=error,
        next_retry_at=next_retry,
        classifier_version=version,
    )

def is_eligible(
    existing: Optional[Classification],
    now: datetime,
    classifier_version: str,
    max_retries: int,
) -> bool:
    """Whether one stored row still needs (re)classification work."""

    if existing is None:
        return True
    version = str(getattr(existing, "classifier_version", "1") or "1")
    if (
        existing.classification_status == "classified"
        and existing.source != "heuristic"
        and version == classifier_version
    ):
        return False
    if existing.retry_count >= max(1, int(max_retries)) and version == classifier_version:
        return False
    upcoming = getattr(existing, "next_retry_at", None)
    if upcoming is not None:
        try:
            if coerce_timestamp(upcoming) > now:
                return False
        except (AttributeError, TypeError, ValueError):
            pass
    return True

@staticmethod

def is_afk_only(session: Any) -> bool:
    """True for sessions positively measured as AFK with no active time.

    The sessionizer never emits AFK spans as sessions, so this gate only
    fires for edge cases (e.g. presence data arriving after the session
    was built). Legacy rows without measured durations always pass.
    """
    try:
        active = float(getattr(session, "active_duration_seconds", 0) or 0)
        afk = float(getattr(session, "afk_duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        return False
    return active <= 0.0 and afk > 0.0

def pending_classification_sessions(
    store,
    sessions: Iterable[Any],
    now: Optional[Any] = None,
    classifier_version: str = CLASSIFIER_VERSION_DEFAULT,
    max_retries: int = CLASSIFICATION_RETRIES_MAX,
) -> list:
    """Filter candidate sessions down to work the scheduler should do now.

    This is the single source of truth for queue eligibility: missing
    rows, pending rows due for retry, failed rows with attempts left,
    historic heuristic rows, and rows stamped with an older classifier
    version. Successfully classified rows under the current version,
    terminally failed rows, and AFK-only sessions are excluded so quota
    is never wasted and the LLM never sees idle time.

    Meaningful sessions additionally qualify while any member raw
    session still needs a verdict, so per-member coverage converges
    across cycles instead of stranding remainder behind the per-episode
    expansion cap.
    """

    current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
    candidates = list(sessions)
    stored = store.query_classification_map([item.id for item in candidates])
    # Member coverage for meaningful sessions: an episode re-qualifies
    # while a resolvable member still needs a verdict, so per-member
    # coverage converges across cycles. Unknown member ids (pruned or
    # fixture-phantom sessions) are ignored — only members present in
    # the store can need work, otherwise phantom ids would re-queue
    # their episode forever.
    member_ids = [
        str(member_id)
        for item in candidates
        for member_id in (getattr(item, "activity_session_ids", None) or ())
        if str(member_id or "").strip()
    ]
    member_rows = store.query_classification_map(member_ids) if member_ids else {}
    present_members: dict[str, set[str]] = {}
    if member_ids:
        try:
            resolved = store.query_sessions_by_ids(member_ids)
        except (AttributeError, OSError, TypeError, ValueError):
            resolved = []
        resolved_ids = {item.id for item in resolved}
        for item in candidates:
            present_members[item.id] = {
                str(member_id)
                for member_id in (getattr(item, "activity_session_ids", None) or ())
                if str(member_id) in resolved_ids
            }
    return [
        item for item in candidates
        if not is_afk_only(item)
        and (
            is_eligible(stored.get(item.id), current, classifier_version, max_retries)
            or any(
                is_eligible(member_rows.get(member_id), current, classifier_version, max_retries)
                for member_id in present_members.get(item.id, ())
            )
        )
    ]

def episode_members(store: Any, episode: Any) -> list:
    """Resolve one episode's member raw sessions in episode order.

    Unknown ids are skipped: a member that cannot be loaded cannot be
    judged, and the episode verdict path still covers display.
    """
    member_ids = [
        str(member_id)
        for member_id in (getattr(episode, "activity_session_ids", None) or ())
        if str(member_id or "").strip()
    ]
    if not member_ids:
        return []
    try:
        members = store.query_sessions_by_ids(member_ids)
    except (AttributeError, OSError, TypeError, ValueError):
        return []
    by_id = {item.id: item for item in members}
    return [by_id[mid] for mid in member_ids if mid in by_id]

@staticmethod

def member_context(episode: Any, member: Any, members: list) -> list:
    """Episode-scoped context for one member item: the episode envelope
    plus capped sibling items. Never draws from other episodes, so batch
    items cannot corroborate each other across group boundaries."""
    siblings = []
    for item in members or ():
        if getattr(item, "id", None) == getattr(member, "id", None):
            continue
        try:
            siblings.append({
                "application": item.app,
                "title": item.title,
                "domain": item.domain,
                "url": item.url,
                "duration_seconds": item.duration,
            })
        except (AttributeError, TypeError, ValueError):
            continue
    envelope = {
        "episode_task": getattr(episode, "primary_task", None),
        "episode_topic": getattr(episode, "primary_topic", None),
        "episode_project": getattr(episode, "primary_project", None),
    }
    return [envelope] + siblings[:MAX_MEMBER_SIBLINGS]

def expand_classification_items(
    store: Any,
    episodes: Iterable[Any],
    context_by_session: Optional[Mapping[str, Iterable[Mapping[str, Any]]]] = None,
    classifier_version: str = "1",
    max_retries: int = 5,
    now: Optional[Any] = None,
) -> Tuple[list, dict, set]:
    """Expand episodes into batched classification items.

    Returns ``(expanded, context, sealed)`` where ``expanded`` is
    [episode, member, ...] in episode order, ``context`` merges the given
    per-session context with sealed episode-scoped member contexts, and
    ``sealed`` holds member ids whose context is complete (temporal
    neighbors from other groups must not corroborate them).
    """
    current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
    ordered = list(episodes)
    context = dict(context_by_session or {})
    expanded: list = []
    sealed: set = set()
    for episode in ordered:
        expanded.append(episode)
        members = episode_members(store, episode)
        if not members:
            continue
        pending = pending_classification_sessions(
            store, members, now=current,
            classifier_version=classifier_version,
            max_retries=max_retries,
        )
        for member in pending[:max(1, int(MAX_MEMBER_ITEMS_PER_EPISODE))]:
            expanded.append(member)
            sealed.add(member.id)
            context.setdefault(
                member.id, member_context(episode, member, members))
    return expanded, context, sealed
