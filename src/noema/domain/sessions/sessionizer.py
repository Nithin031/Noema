"""Deterministic event-to-session algorithm owned by the AI layer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, List, Optional, Tuple

from noema.domain.activity import ActivityEvent
from noema.domain.presence import PresenceState, PresenceTimeline, is_presence_event

from .models import ActivitySession


@dataclass
class _SessionBuilder:
    events: List[ActivityEvent]
    _end: Optional[datetime] = None

    @property
    def end(self):
        # Cached running maximum: events only ever join a builder, so the
        # end is monotonic and recomputing max() over the whole run on
        # every membership probe was O(run^2) per session.
        if self._end is None:
            self._end = max(event.end_timestamp for event in self.events)
        return self._end

    def add(self, event: ActivityEvent) -> None:
        self.events.append(event)
        if self._end is None or event.end_timestamp > self._end:
            self._end = event.end_timestamp

    def build(self) -> ActivitySession:
        return ActivitySession.from_events(tuple(self.events))


class Sessionizer:
    """Group nearby events with the same activity context.

    A new session starts when the context changes or the gap between the
    previous session's end and the next event exceeds ``max_gap_seconds``.
    Context intentionally excludes window title by default: editing two files
    in the same application should not create a new session for every title
    change. Browser domains are included, so switching from arxiv.org to
    reddit.com creates a boundary when browser watcher data includes domains.

    Presence-aware mode (``presence`` supplied to :meth:`sessionize`) adds:

    * AFK telemetry rows are never sessionized as activity.
    * Any AFK onset splits the stream; AFK-covered spans never become
      sessions (PART 9/19).
    * Rapid cross-context transitions merge into one active run when the gap
      fits ``merge_gap_seconds`` and presence is proven ACTIVE across it —
      whatever each stop lasts, because a coherent flow (Firefox 40s ->
      VS Code 22s -> Firefox 31s) must merge while a long detour after a
      large gap must not. Sub-``short_event_seconds`` transitions are
      absorbed as noise within ``max_gap_seconds``. Longer detours and
      distinct tasks are separated downstream by meaningful-session
      continuity scoring, which owns the semantic evidence.
    """

    def __init__(
        self,
        max_gap_seconds: float = 60.0,
        split_on_title_change: bool = False,
        merge_gap_seconds: float = 0.0,
        short_event_seconds: float = 5.0,
    ):
        if max_gap_seconds < 0:
            raise ValueError("max_gap_seconds cannot be negative")
        if merge_gap_seconds < 0:
            raise ValueError("merge_gap_seconds cannot be negative")
        if short_event_seconds < 0:
            raise ValueError("short_event_seconds cannot be negative")
        self.max_gap_seconds = float(max_gap_seconds)
        self.split_on_title_change = split_on_title_change
        self.merge_gap_seconds = float(merge_gap_seconds)
        self.short_event_seconds = float(short_event_seconds)

    def _context(self, event: ActivityEvent) -> Tuple[str, ...]:
        application_id = str(
            event.metadata.get("application_id")
            if isinstance(event.metadata, dict) and event.metadata.get("application_id")
            else (event.app or "")
        )
        context = (
            event.device.casefold(),
            application_id.casefold(),
            (event.domain or "").casefold(),
            event.source.casefold(),
            (event.browser or "").casefold(),
            event.browser_window_id or "",
            event.browser_tab_id or "",
        )
        app = (event.app or "").casefold()
        title = (event.title or "").casefold()
        browser_title = any(marker in title for marker in (
            "mozilla firefox", "google chrome", "microsoft edge", "brave", "opera",
        ))
        browser_without_domain = browser_title and not event.domain and not event.url
        if self.split_on_title_change or browser_without_domain:
            return context + ((event.title or "").casefold(),)
        return context

    def _afk_between(self, presence: Any, start: datetime, end: datetime) -> bool:
        """True when any proven AFK interval touches (start, end]."""
        if presence is None or PresenceState is None or end <= start:
            return False
        try:
            intervals = getattr(presence, "intervals", None) or []
            for interval in intervals:
                if getattr(interval, "state", None) != PresenceState.AFK:
                    continue
                if interval.start < end and interval.end > start:
                    return True
        except (AttributeError, TypeError, ValueError):
            return False
        return False

    def _belongs(self, builder: _SessionBuilder, event: ActivityEvent, presence: Any = None) -> bool:
        first = builder.events[0]
        if self._context(first) != self._context(event):
            return False
        gap = (event.timestamp - builder.end).total_seconds()
        if gap > self.max_gap_seconds:
            return False
        # An AFK onset is always a boundary, even inside one context.
        return not self._afk_between(presence, builder.end, event.timestamp)

    def _gap_is_active(self, presence: Any, gap_start: datetime, gap_end: datetime) -> bool:
        """True only when proven ACTIVE covers the whole gap (PART 8).

        UNKNOWN presence never authorizes a cross-context merge: without
        input evidence the sessionizer falls back to legacy boundaries.
        """
        if presence is None or PresenceState is None:
            return False
        if gap_end <= gap_start:
            return True
        probe = gap_start
        step = (gap_end - gap_start).total_seconds()
        # Check endpoints plus midpoint: short gaps need few probes, and any
        # AFK touch inside the gap vetoes the merge.
        probes = {probe, gap_end}
        if step > 1.0:
            from datetime import timedelta

            probes.add(gap_start + (gap_end - gap_start) / 2)
            probes.add(gap_end - timedelta(milliseconds=1))
        try:
            return all(presence.state_at(moment) == PresenceState.ACTIVE for moment in probes)
        except (AttributeError, TypeError, ValueError):
            return False

    def _should_merge_across_context(
        self,
        builder: _SessionBuilder,
        event: ActivityEvent,
        presence: Any,
    ) -> bool:
        """Decide a contextual transition belongs to the running session.

        Deliberately duration-blind beyond the short-event rule: a coherent
        interaction flow (proven input, tiny gaps) stays one session no
        matter how long each stop lasts, because PART 14's own example chain
        (40s OpenCode -> 22s VS Code -> 31s Firefox) must merge while a
        2-minute WhatsApp detour must not — durations alone cannot tell
        those apart, but gap size plus proven presence can. Longer detours
        and different tasks are separated downstream by the meaningful
        layer's continuity scoring, which has the semantic evidence.
        """
        if presence is None or self.merge_gap_seconds <= 0:
            return False
        gap = (event.timestamp - builder.end).total_seconds()
        if gap < 0:
            return False
        if not self._gap_is_active(presence, builder.end, event.timestamp):
            return False
        if float(event.duration) < self.short_event_seconds:
            # Transition noise (a 1s tab flash): absorb within the hard
            # boundary even when the merge window has passed.
            return gap <= self.max_gap_seconds
        return self.merge_gap_seconds > 0 and gap <= self.merge_gap_seconds

    @staticmethod
    def _active_runs(
        start: datetime,
        end: datetime,
        presence: Any,
    ) -> List[Tuple[datetime, datetime]]:
        """Maximal sub-spans of [start, end) containing no proven AFK time.

        UNKNOWN regions (no presence evidence) are kept whole: without input
        data the sessionizer falls back to legacy time/context boundaries
        rather than stranding activity unclassifiable.
        """
        try:
            intervals = list(getattr(presence, "intervals", None) or [])
        except (AttributeError, TypeError):
            return [(start, end)] if end > start else []
        cuts = {start, end}
        for interval in intervals:
            try:
                if getattr(interval, "state", None) != PresenceState.AFK:
                    continue
                if interval.start < end and interval.end > start:
                    if start < interval.start < end:
                        cuts.add(interval.start)
                    if start < interval.end < end:
                        cuts.add(interval.end)
            except (AttributeError, TypeError, ValueError):
                continue
        ordered_cuts = sorted(cuts)
        runs: List[Tuple[datetime, datetime]] = []
        for span_start, span_end in zip(ordered_cuts, ordered_cuts[1:]):
            if span_end <= span_start:
                continue
            midpoint = span_start + (span_end - span_start) / 2
            try:
                afk = presence.state_at(midpoint) == PresenceState.AFK
            except (AttributeError, TypeError, ValueError):
                afk = False
            if not afk:
                runs.append((span_start, span_end))
        return runs

    @staticmethod
    def _clip_session(
        session: ActivitySession,
        start: datetime,
        end: datetime,
    ) -> Optional[ActivitySession]:
        """Restrict a built session to proven-active bounds (PART 9/51).

        Event keys are preserved so dashboard joins keep working; the id
        re-derives deterministically from content plus bounds, so rebuilds
        are stable. Sessions fully outside the bounds vanish (None).
        """
        clip_start = max(session.start, start)
        clip_end = min(session.end, end)
        if clip_end <= clip_start:
            return None
        if clip_start == session.start and clip_end == session.end:
            return session
        return ActivitySession(
            start=clip_start,
            end=clip_end,
            device=session.device,
            app=session.app,
            title=session.title,
            domain=session.domain,
            url=session.url,
            source=session.source,
            event_keys=session.event_keys,
            metadata=dict(session.metadata),
            browser=session.browser,
            browser_window_id=session.browser_window_id,
            browser_tab_id=session.browser_tab_id,
        )

    def _split_by_presence(
        self,
        ordered: List[ActivityEvent],
        presence: Any,
    ) -> List[Tuple[datetime, datetime, List[ActivityEvent]]]:
        """Segment the stream at AFK onsets; AFK spans yield no sessions.

        An event joins every active run it overlaps, so one long event
        straddling AFK contributes its active pieces on both sides (PART 9)
        instead of vanishing or smearing idle time as activity. Raw rows
        stay untouched in storage for audit.
        """
        if not ordered:
            return []
        span_start = min(event.timestamp for event in ordered)
        span_end = max(event.end_timestamp for event in ordered)
        segments = []
        for run_start, run_end in self._active_runs(span_start, span_end, presence):
            members = [
                event for event in ordered
                if event.timestamp < run_end and event.end_timestamp > run_start
            ]
            if members:
                segments.append((run_start, run_end, members))
        return segments

    def sessionize(
        self,
        events: Iterable[ActivityEvent],
        presence: Any = None,
    ) -> List[ActivitySession]:
        ordered = sorted(events, key=lambda event: (event.timestamp, event.event_key))
        # AFK-watcher status rows are presence telemetry, never activity:
        # exclude them here (their raw rows remain stored as the source).
        ordered = [event for event in ordered if not is_presence_event(event)]
        if presence is None:
            return self._sessionize_run(ordered, None)
        sessions: List[ActivitySession] = []
        for run_start, run_end, run in self._split_by_presence(ordered, presence):
            for session in self._sessionize_run(run, presence):
                clipped = self._clip_session(session, run_start, run_end)
                if clipped is not None:
                    sessions.append(clipped)
        return sessions

    def _sessionize_run(
        self,
        ordered: List[ActivityEvent],
        presence: Any = None,
    ) -> List[ActivitySession]:
        sessions: List[ActivitySession] = []
        builder: Optional[_SessionBuilder] = None
        seen_keys = set()
        for event in ordered:
            if not isinstance(event, ActivityEvent):
                raise TypeError("Sessionizer expects ActivityEvent instances")
            if event.event_key in seen_keys:
                continue
            seen_keys.add(event.event_key)
            if builder is None:
                builder = _SessionBuilder([event])
            elif self._belongs(builder, event, presence):
                builder.add(event)
            elif presence is not None and self._should_merge_across_context(builder, event, presence):
                builder.add(event)
            else:
                sessions.append(builder.build())
                builder = _SessionBuilder([event])
        if builder is not None:
            sessions.append(builder.build())
        return sessions

    def __call__(self, events: Iterable[ActivityEvent], presence: Any = None) -> List[ActivitySession]:
        return self.sessionize(events, presence=presence)
