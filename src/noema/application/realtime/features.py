"""Rolling behavioral feature windows.

A :class:`BehaviorWindow` compresses one rolling window (5/15/30/60
minutes by default) of *already-understood* state — meaningful sessions,
their stored classifications, presence truth, and goal alignment — into a
compact, bounded, JSON-safe feature set.

Deliberate boundaries:

- Input is meaningful sessions + classifications + presence, never raw
  ActivityWatch heartbeats and never the complete raw history.
- AFK-only sessions contribute to ``afk_seconds`` only. They never enter
  productivity buckets and never create distraction features.
- Unclassified sessions (missing, pending, failed, or heuristic rows) sit
  in ``unclassified_seconds``. ``unclassified != neutral`` everywhere.
- Gaps with no session coverage are simply not counted; every second in
  the buckets is traceable to a stored session.
- Pure function of its inputs: identical inputs always produce identical
  windows (deterministic, testable).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

FEATURE_VERSION = "1"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _overlap_seconds(start_a: Any, end_a: Any, start_b: Any, end_b: Any) -> float:
    try:
        latest_start = max(start_a, start_b)
        earliest_end = min(end_a, end_b)
    except TypeError:
        return 0.0
    return max(0.0, (earliest_end - latest_start).total_seconds())


def _session_label(session: Any) -> str:
    for attr in ("primary_topic", "primary_task", "dominant_category"):
        try:
            value = getattr(session, attr, None)
        except Exception:
            value = None
        if value:
            return str(value)[:80]
    return "unknown"


def _session_bounds(session: Any) -> Optional[Tuple[Any, Any]]:
    start = getattr(session, "start_time", None)
    end = getattr(session, "end_time", None)
    if start is None or end is None or end <= start:
        return None
    return start, end


@dataclass(frozen=True)
class BehaviorWindowConfig:
    """Knobs for the feature layer. All changes are code-free config."""

    windows_minutes: Tuple[int, ...] = (5, 15, 30, 60)
    short_burst_seconds: float = 180.0
    recent_sequence_limit: int = 12

    def __post_init__(self) -> None:
        windows = tuple(int(item) for item in self.windows_minutes)
        if not windows or any(item <= 0 for item in windows):
            raise ValueError("windows_minutes must name positive minute counts")
        object.__setattr__(self, "windows_minutes", windows)
        if float(self.short_burst_seconds) < 0:
            raise ValueError("short_burst_seconds cannot be negative")
        if int(self.recent_sequence_limit) < 1:
            raise ValueError("recent_sequence_limit must be positive")


@dataclass(frozen=True)
class BehaviorWindow:
    """One rolling window of behavioral features (see module docstring)."""

    window_minutes: int
    window_start: str
    window_end: str
    active_seconds: float
    afk_seconds: float
    productive_seconds: float
    distractive_seconds: float
    neutral_seconds: float
    unclassified_seconds: float
    distraction_ratio: float
    productive_ratio: float
    distraction_entries: int
    productive_entries: int
    neutral_entries: int
    context_switch_count: int
    productive_to_distraction_switches: int
    distraction_to_productive_switches: int
    short_distraction_bursts: int
    longest_distraction_streak: int
    longest_distraction_run_seconds: float
    repeated_distraction_contexts: Dict[str, int]
    unique_distraction_contexts: int
    time_since_last_productive_session: Optional[float]
    current_session_id: Optional[str]
    current_session_duration: float
    current_session_category: str
    current_session_confidence: float
    current_goal: Optional[str]
    goal_alignment: Optional[float]
    recent_session_sequence: List[Dict[str, Any]]
    feature_version: str = FEATURE_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_minutes": self.window_minutes,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "active_seconds": round(self.active_seconds, 1),
            "afk_seconds": round(self.afk_seconds, 1),
            "productive_seconds": round(self.productive_seconds, 1),
            "distractive_seconds": round(self.distractive_seconds, 1),
            "neutral_seconds": round(self.neutral_seconds, 1),
            "unclassified_seconds": round(self.unclassified_seconds, 1),
            "distraction_ratio": round(self.distraction_ratio, 3),
            "productive_ratio": round(self.productive_ratio, 3),
            "distraction_entries": self.distraction_entries,
            "productive_entries": self.productive_entries,
            "neutral_entries": self.neutral_entries,
            "context_switch_count": self.context_switch_count,
            "productive_to_distraction_switches": self.productive_to_distraction_switches,
            "distraction_to_productive_switches": self.distraction_to_productive_switches,
            "short_distraction_bursts": self.short_distraction_bursts,
            "longest_distraction_streak": self.longest_distraction_streak,
            "longest_distraction_run_seconds": round(self.longest_distraction_run_seconds, 1),
            "repeated_distraction_contexts": dict(self.repeated_distraction_contexts),
            "unique_distraction_contexts": self.unique_distraction_contexts,
            "time_since_last_productive_session": (
                None
                if self.time_since_last_productive_session is None
                else round(self.time_since_last_productive_session, 1)
            ),
            "current_session_id": self.current_session_id,
            "current_session_duration": round(self.current_session_duration, 1),
            "current_session_category": self.current_session_category,
            "current_session_confidence": round(self.current_session_confidence, 3),
            "current_goal": self.current_goal,
            "goal_alignment": self.goal_alignment,
            "recent_session_sequence": [dict(item) for item in self.recent_session_sequence],
            "feature_version": self.feature_version,
        }

    def compact_summary(self) -> Dict[str, Any]:
        """Minimal feature map for fast-model verification input.

        This is the ONLY thing the fast model ever sees: no raw events,
        no titles, no URLs, no history dumps.
        """
        return {
            "window_minutes": self.window_minutes,
            "active_minutes": round(self.active_seconds / 60.0, 1),
            "distraction_minutes": round(self.distractive_seconds / 60.0, 1),
            "distraction_ratio": round(self.distraction_ratio, 3),
            "distraction_entries": self.distraction_entries,
            "context_switches": self.context_switch_count,
            "short_distraction_bursts": self.short_distraction_bursts,
            "longest_distraction_streak": self.longest_distraction_streak,
            "longest_distraction_run_minutes": round(self.longest_distraction_run_seconds / 60.0, 1),
            "unique_distraction_contexts": self.unique_distraction_contexts,
            "time_since_productive_minutes": (
                None
                if self.time_since_last_productive_session is None
                else round(self.time_since_last_productive_session / 60.0, 1)
            ),
            "goal_alignment": self.goal_alignment,
            "current_category": self.current_session_category,
        }


def _bucket_for(classification: Any) -> Optional[str]:
    """Map a stored classification to a productivity bucket or None.

    None means "not classifiable right now" (missing, pending, failed, or
    heuristic rows) — those seconds become ``unclassified_seconds``.
    """
    if classification is None:
        return None
    try:
        status = str(getattr(classification, "classification_status", "") or "")
        source = str(getattr(classification, "source", "") or "")
    except Exception:
        return None
    if status != "classified" or source == "heuristic":
        return None
    try:
        productivity = str(getattr(classification, "productivity", "") or "").strip().lower()
    except Exception:
        return None
    if productivity == "productive":
        return "productive"
    if productivity == "distracting":
        return "distractive"
    if productivity == "neutral":
        return "neutral"
    return None


def _build_one_window(
    window_minutes: int,
    now: datetime,
    sessions: List[Any],
    classifications: Mapping[str, Any],
    config: BehaviorWindowConfig,
    current_goal: Optional[str],
    goal_alignment: Optional[float],
) -> BehaviorWindow:
    from datetime import timedelta

    window_start = now - timedelta(minutes=window_minutes)
    ordered = sorted(
        (session for session in sessions if _session_bounds(session) is not None),
        key=lambda item: (item.start_time, item.id),
    )
    productive_seconds = distractive_seconds = neutral_seconds = 0.0
    unclassified_seconds = afk_seconds = 0.0
    distraction_entries = productive_entries = neutral_entries = 0
    buckets: List[str] = []
    labels: List[str] = []
    short_bursts = 0
    longest_streak = streak = 0
    longest_run = run_seconds = 0.0
    distraction_context_counts: Dict[str, int] = {}
    last_productive_end: Optional[Any] = None
    sequence: List[Dict[str, Any]] = []

    for session in ordered:
        bounds = _session_bounds(session)
        if bounds is None:
            continue
        start, end = bounds
        overlap = _overlap_seconds(start, end, window_start, now)
        if overlap <= 0:
            continue
        try:
            session_afk = max(0.0, float(getattr(session, "afk_duration_seconds", 0.0) or 0.0))
            session_active = max(0.0, float(getattr(session, "active_duration_seconds", 0.0) or 0.0))
        except (TypeError, ValueError):
            session_afk, session_active = 0.0, 0.0
        total = session_active + session_afk
        # AFK-only sessions (no active time at all) own the AFK bucket and
        # nothing else — they can never look like distraction.
        if session_active <= 0 and session_afk > 0:
            afk_seconds += overlap
            continue
        if total > 0:
            afk_seconds += overlap * (session_afk / total)
            active_overlap = overlap * (session_active / total)
        else:  # No presence split recorded: treat overlap as active.
            active_overlap = overlap
        classification = classifications.get(getattr(session, "id", None))
        bucket = _bucket_for(classification)
        label = _session_label(session)
        if bucket is None:
            unclassified_seconds += active_overlap
            buckets.append("unclassified")
            run_seconds = 0.0
        elif bucket == "productive":
            productive_seconds += active_overlap
            productive_entries += 1
            buckets.append("productive")
            run_seconds = 0.0
            if end <= now and (last_productive_end is None or end > last_productive_end):
                last_productive_end = end
        elif bucket == "distractive":
            distractive_seconds += active_overlap
            distraction_entries += 1
            buckets.append("distractive")
            distraction_context_counts[label] = distraction_context_counts.get(label, 0) + 1
            # Unbroken run: adjacent distractive sessions merge into one
            # continuous drift, however the sessionizer cut them.
            run_seconds += active_overlap
            longest_run = max(longest_run, run_seconds)
            full_duration = (end - start).total_seconds()
            if full_duration <= float(config.short_burst_seconds):
                short_bursts += 1
        else:
            run_seconds = 0.0
            neutral_seconds += active_overlap
            neutral_entries += 1
            buckets.append("neutral")
        labels.append(label)
        try:
            confidence = float(getattr(classification, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        sequence.append({
            "session_id": str(getattr(session, "id", "")),
            "category": buckets[-1],
            "context": label,
            "duration_seconds": round(active_overlap, 1),
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
        })

    context_switches = sum(
        1 for first, second in zip(labels, labels[1:]) if first != second
    )
    prod_to_dist = sum(
        1
        for first, second in zip(buckets, buckets[1:])
        if first == "productive" and second == "distractive"
    )
    dist_to_prod = sum(
        1
        for first, second in zip(buckets, buckets[1:])
        if first == "distractive" and second == "productive"
    )
    for bucket in buckets:
        if bucket == "distractive":
            streak += 1
            longest_streak = max(longest_streak, streak)
        else:
            streak = 0
    repeated = {
        context: count
        for context, count in sorted(distraction_context_counts.items())
        if count >= 2
    }
    active_total = productive_seconds + distractive_seconds + neutral_seconds + unclassified_seconds
    distraction_ratio = (distractive_seconds / active_total) if active_total > 0 else 0.0
    productive_ratio = (productive_seconds / active_total) if active_total > 0 else 0.0
    if last_productive_end is not None:
        try:
            since_productive: Optional[float] = max(
                0.0, (now - last_productive_end).total_seconds()
            )
        except TypeError:
            since_productive = None
    else:
        since_productive = None

    current = ordered[-1] if ordered else None
    current_id: Optional[str] = None
    current_duration = 0.0
    current_category = "none"
    current_confidence = 0.0
    if current is not None:
        bounds = _session_bounds(current)
        current_id = str(getattr(current, "id", ""))
        if bounds is not None:
            current_duration = max(0.0, (min(bounds[1], now) - max(bounds[0], window_start)).total_seconds())
        current_classification = classifications.get(getattr(current, "id", None))
        current_bucket = _bucket_for(current_classification)
        try:
            current_active = float(getattr(current, "active_duration_seconds", 0.0) or 0.0)
        except (TypeError, ValueError):
            current_active = 0.0
        try:
            current_afk = float(getattr(current, "afk_duration_seconds", 0.0) or 0.0)
        except (TypeError, ValueError):
            current_afk = 0.0
        if current_active <= 0 and current_afk > 0:
            current_category = "afk"
        elif current_bucket is None:
            current_category = "unclassified"
        else:
            current_category = current_bucket
        try:
            current_confidence = max(
                0.0, min(1.0, float(getattr(current_classification, "confidence", 0.0) or 0.0))
            )
        except (TypeError, ValueError):
            current_confidence = 0.0

    return BehaviorWindow(
        window_minutes=window_minutes,
        window_start=_iso(window_start),
        window_end=_iso(now),
        active_seconds=round(active_total, 1),
        afk_seconds=round(afk_seconds, 1),
        productive_seconds=round(productive_seconds, 1),
        distractive_seconds=round(distractive_seconds, 1),
        neutral_seconds=round(neutral_seconds, 1),
        unclassified_seconds=round(unclassified_seconds, 1),
        distraction_ratio=distraction_ratio,
        productive_ratio=productive_ratio,
        distraction_entries=distraction_entries,
        productive_entries=productive_entries,
        neutral_entries=neutral_entries,
        context_switch_count=context_switches,
        productive_to_distraction_switches=prod_to_dist,
        distraction_to_productive_switches=dist_to_prod,
        short_distraction_bursts=short_bursts,
        longest_distraction_streak=longest_streak,
        longest_distraction_run_seconds=round(longest_run, 1),
        repeated_distraction_contexts=repeated,
        unique_distraction_contexts=len(distraction_context_counts),
        time_since_last_productive_session=since_productive,
        current_session_id=current_id,
        current_session_duration=current_duration,
        current_session_category=current_category,
        current_session_confidence=current_confidence,
        current_goal=current_goal,
        goal_alignment=goal_alignment,
        recent_session_sequence=sequence[-int(config.recent_sequence_limit):],
    )


def build_behavior_windows(
    sessions: Iterable[Any],
    classifications: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
    config: Optional[BehaviorWindowConfig] = None,
    current_goal: Optional[str] = None,
    goal_alignment: Optional[float] = None,
) -> List[BehaviorWindow]:
    """Build one window per configured span, longest context first.

    ``sessions`` are meaningful sessions (any superset is fine; only rows
    overlapping each window contribute). ``classifications`` maps session
    id → stored classification row. ``now`` defaults to UTC now. Output is
    ordered by ascending window size.
    """
    moment = now
    if moment is None:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    cfg = config or BehaviorWindowConfig()
    session_list = list(sessions or [])
    class_map = classifications or {}
    return [
        _build_one_window(
            minutes, moment, session_list, class_map, cfg, current_goal, goal_alignment
        )
        for minutes in sorted(cfg.windows_minutes)
    ]
