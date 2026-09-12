"""Orchestration for the read/filter/store Generation 0 pipeline."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.domain.activity import ActivityEvent, coerce_timestamp
from noema.infrastructure.browser import BrowserBridge, BrowserEvent, DesktopNotificationHandler
from noema.domain.behavior import BehaviorObservation, BehaviorEngine
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification, Classifier
from noema.domain.intent import AlignmentResult, GoalAligner, Intent, IntentEngine
from noema.domain.intervention import Intervention, InterventionEngine, InterventionMode, InterventionStatus
from noema.domain.meme import MemeIntelligence, MemePayload
from noema.domain.memory import Memory, MemoryEngine, PersonalizationProfile, Personalizer
from noema.domain.meaningful import MeaningfulSession, MeaningfulSessionEngine, MeaningfulSessionSummarizer
from noema.domain.outcomes import InterventionOutcome, OutcomeTracker
from noema.infrastructure.sync import SyncEnvelope, UnifiedTimeline
from noema.application.autonomous import AgentRecommendation, AutonomousAgent, AutonomousCycle
from noema.domain.normalization import EventNormalizer, application_identity
from noema.domain.presence import PresenceDetector, PresenceState, PresenceTimeline, is_presence_event
from noema.domain.privacy import PrivacyFilter
from noema.domain.sessions import ActivitySession, Sessionizer
from noema.domain.session_context import browser_name


class _NullCallHandle:
    """Writable stand-in for a model-call handle when telemetry is off."""

    def __init__(self) -> None:
        self.usage = None
        self.estimated_input_tokens = None
        self.session_ids: list = []
        self.batch_size = 0
        self.context_window = None
        self.max_output_tokens = None
        self.quota_scope = None
        self.quota_before = None
        self.quota_after = None
        self.error = None
        self.success_payload = False


@dataclass(frozen=True)
class IngestionResult:
    accepted: int = 0
    discarded: int = 0
    duplicates: int = 0
    updated: int = 0

    @property
    def processed(self) -> int:
        return self.accepted + self.discarded + self.duplicates + self.updated

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "discarded": self.discarded,
            "duplicates": self.duplicates,
            "processed": self.processed,
        }


class NoemaService:
    """Compose AW adapter, privacy policy, and application database."""

    def __init__(
        self,
        adapter: ActivityWatchAdapter,
        store: SQLiteStore,
        privacy_filter: Optional[PrivacyFilter] = None,
        normalizer: Optional[EventNormalizer] = None,
        sessionizer: Optional[Sessionizer] = None,
        classifier: Optional[Classifier] = None,
        intent_engine: Optional[IntentEngine] = None,
        goal_aligner: Optional[GoalAligner] = None,
        behavior_engine: Optional[BehaviorEngine] = None,
        intervention_engine: Optional[InterventionEngine] = None,
        meme_intelligence: Optional[MemeIntelligence] = None,
        outcome_tracker: Optional[OutcomeTracker] = None,
        memory_engine: Optional[MemoryEngine] = None,
        personalizer: Optional[Personalizer] = None,
        autonomous_agent: Optional[AutonomousAgent] = None,
        meaningful_session_engine: Optional[MeaningfulSessionEngine] = None,
        meaningful_session_summarizer: Optional[MeaningfulSessionSummarizer] = None,
        browser_bridge: Optional[BrowserBridge] = None,
        notification_handler: Optional[DesktopNotificationHandler] = None,
        presence_detector: Optional[Any] = None,
    ):
        self.adapter = adapter
        self.store = store
        self.privacy_filter = privacy_filter or PrivacyFilter()
        self.normalizer = normalizer or EventNormalizer()
        self.sessionizer = sessionizer or Sessionizer()
        self.classifier = classifier or Classifier()
        self.intent_engine = intent_engine or IntentEngine()
        self.goal_aligner = goal_aligner or GoalAligner()
        self.behavior_engine = behavior_engine or BehaviorEngine()
        self.intervention_engine = intervention_engine or InterventionEngine()
        self.meme_intelligence = meme_intelligence or MemeIntelligence()
        self.outcome_tracker = outcome_tracker or OutcomeTracker()
        self.memory_engine = memory_engine or MemoryEngine()
        self.personalizer = personalizer or Personalizer()
        self.autonomous_agent = autonomous_agent or AutonomousAgent()
        self.meaningful_session_engine = meaningful_session_engine or MeaningfulSessionEngine()
        self.meaningful_session_summarizer = meaningful_session_summarizer or MeaningfulSessionSummarizer()
        self.browser_bridge = browser_bridge or BrowserBridge()
        self.notification_handler = notification_handler or DesktopNotificationHandler()
        self.presence_detector = presence_detector
        # Real-time time scale (wired by daemon/cli.py; safe defaults here
        # so the service is usable without the daemon). The 20-minute
        # semantic pipeline never touches these.
        from noema.application.realtime import DetectionTracker, DistractionCandidateDetector

        self.realtime_detector = DistractionCandidateDetector()
        self.detection_tracker = DetectionTracker()
        self.fast_verifier = None
        # Observability hook (wired by daemon/cli.py to a TelemetryRecorder).
        # When None, all instrumentation below no-ops and product behavior
        # is unchanged. The layer observes; it never decides.
        self.telemetry = None

    def _observe_db(self, name: str, metadata: Optional[dict] = None):
        """No-op-safe timer for DB stages (context manager)."""
        import contextlib

        recorder = getattr(self, "telemetry", None)
        if recorder is None:
            return contextlib.nullcontext({})
        return recorder.observe_operation("db", name, metadata=metadata)

    def telemetry_repository(self):
        """Repository for observability reads (tables migrate idempotently)."""
        from noema.observability.repository import TelemetryRepository

        recorder = getattr(self, "telemetry", None)
        repository = getattr(recorder, "repository", None)
        if repository is not None:
            return repository
        return TelemetryRepository(self.store)

    def metrics_bundle(self, days: int = 30, daemon: Any = None) -> dict:
        """Assemble every metrics view from canonical sources."""
        from noema.observability.metrics import build_metrics_bundle

        chain = getattr(getattr(self, "classifier", None), "provider", None)
        from noema.infrastructure.providers import ProviderChain

        if not isinstance(chain, ProviderChain):
            chain = None
        return build_metrics_bundle(
            self.telemetry_repository(), self.store,
            chain=chain, daemon=daemon, days=days)

    def _observe_model(self, provider_name: Any, model: Any, purpose: str,
                       pipeline: str, operation: str, request_id: str):
        """No-op-safe model-call timer (context manager)."""
        import contextlib
        from types import SimpleNamespace

        recorder = getattr(self, "telemetry", None)
        if recorder is None:
            return contextlib.nullcontext(_NullCallHandle())
        return recorder.observe_model_call(
            SimpleNamespace(name=str(provider_name or "?")), model,
            purpose, pipeline, operation, request_id)

    def ingest_events(self, events: Iterable[ActivityEvent]) -> IngestionResult:
        import time as _time

        started = _time.perf_counter()
        accepted = discarded = duplicates = updated = 0
        for event in events:
            normalized = self.normalizer.normalize(event)
            filtered = self.privacy_filter.filter(normalized)
            if filtered is None:
                discarded += 1
            else:
                status = self.store.upsert_event(filtered)
                if status == "inserted":
                    accepted += 1
                elif status == "updated":
                    updated += 1
                else:
                    duplicates += 1
        result = IngestionResult(accepted, discarded, duplicates, updated)
        recorder = getattr(self, "telemetry", None)
        if recorder is not None:
            try:
                recorder.record_operation(
                    "db", "event_ingest",
                    duration_ms=round((_time.perf_counter() - started) * 1000, 1),
                    rows_affected=accepted,
                    metadata={"duplicates": duplicates, "updated": updated,
                              "discarded": discarded})
            except Exception:
                pass
        return result

    def ingest_bucket(self, bucket: Any) -> IngestionResult:
        return self.ingest_events(self.adapter.iter_bucket(bucket))

    def ingest_buckets(self, buckets: Any) -> IngestionResult:
        return self.ingest_events(self.adapter.iter_buckets(buckets))

    def ingest_telemetry(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        bucket_ids: Optional[Sequence[str]] = None,
    ) -> IngestionResult:
        events = self.adapter.fetch_events(start, end, bucket_ids)
        if start is not None:
            start_timestamp = coerce_timestamp(start)
            # ActivityWatch currentwindow rows are mutable heartbeats: their
            # original timestamp stays fixed while duration grows. Include a
            # row when its interval overlaps the polling window, otherwise a
            # long-running app would be frozen at its first observed minute.
            events = (event for event in events if event.end_timestamp > start_timestamp)
        if end is not None:
            end_timestamp = coerce_timestamp(end)
            events = (event for event in events if event.timestamp < end_timestamp)
        return self.ingest_events(events)

    def query_events(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query(*args, **kwargs)

    @staticmethod
    def _union_seconds(intervals: Iterable[tuple[datetime, datetime]], start: datetime, end: datetime) -> float:
        clipped = []
        for interval_start, interval_end in intervals:
            left = max(interval_start, start)
            right = min(interval_end, end)
            if right > left:
                clipped.append((left, right))
        total = 0.0
        current_start = current_end = None
        for interval_start, interval_end in sorted(clipped):
            if current_start is None:
                current_start, current_end = interval_start, interval_end
            elif interval_start <= current_end:
                current_end = max(current_end, interval_end)
            else:
                total += (current_end - current_start).total_seconds()
                current_start, current_end = interval_start, interval_end
        if current_start is not None:
            total += (current_end - current_start).total_seconds()
        return round(total, 3)

    def reconciliation_report(
        self,
        application: str,
        start: datetime,
        end: datetime,
    ) -> dict:
        """Trace one application through AW, storage, sessions, and dashboard."""

        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("reconciliation requires an aware, positive time window")
        application_id, display_name = application_identity(application)

        raw_rows = []
        if hasattr(self.adapter, "fetch_raw_events"):
            raw_rows = [
                item for item in self.adapter.fetch_raw_events(start=start, end=end)
                if application_identity(item["normalized"].app, item["normalized"].title)[0] == application_id
            ]
        raw_intervals = [(item["start"], item["end"]) for item in raw_rows]
        raw_sum = round(sum(
            max(0.0, (min(item["end"], end) - max(item["start"], start)).total_seconds())
            for item in raw_rows
        ), 3)

        stored = [
            item for item in self.store.query(start=start, end=end, limit=100000)
            if application_identity(item.event.app, item.event.title)[0] == application_id
        ]
        normalized_intervals = [(item.event.timestamp, item.event.end_timestamp) for item in stored]
        normalized_sum = round(sum(
            max(0.0, (min(item.event.end_timestamp, end) - max(item.event.timestamp, start)).total_seconds())
            for item in stored
        ), 3)

        sessions = [
            item for item in self.store.query_sessions(start=start, end=end, limit=100000)
            if application_identity(item.app, item.title)[0] == application_id
        ]
        session_intervals = [(item.start, item.end) for item in sessions]
        meaningful_all = self.store.query_meaningful_sessions(start=start, end=end, limit=100000)
        session_ids = {item.id for item in sessions}
        meaningful = [
            item for item in meaningful_all
            if session_ids.intersection(item.activity_session_ids)
        ]
        meaningful_intervals = [(item.start, item.end) for item in meaningful]

        # Session start/end describe the semantic wall-clock span and can
        # include small gaps between watcher events. Reconciliation compares
        # observed telemetry, so calculate the exact interval union from the
        # source event keys at both session layers. Wall-clock spans remain in
        # the serialized session records for debugging/classification.
        event_intervals = {
            item.event.event_key: (item.event.timestamp, item.event.end_timestamp)
            for item in stored
        }
        session_observed_intervals = [
            event_intervals[event_key]
            for session in sessions
            for event_key in session.event_keys
            if event_key in event_intervals
        ]
        session_id_to_observed = {
            session.id: [
                event_intervals[event_key]
                for event_key in session.event_keys
                if event_key in event_intervals
            ]
            for session in sessions
        }
        meaningful_observed_intervals = [
            interval
            for item in meaningful
            for session_id in item.activity_session_ids
            for interval in session_id_to_observed.get(session_id, [])
        ]

        daily = self.daily_summary(start, end)
        dashboard_segments = [
            segment for segment in daily.get("timeline", [])
            if segment.get("application_id") == application_id
        ]
        dashboard_seconds = round(sum(float(segment.get("seconds", 0) or 0) for segment in dashboard_segments), 3)

        def iso(value: datetime) -> str:
            return value.isoformat().replace("+00:00", "Z")

        raw_debug = []
        for item in raw_rows[:1000]:
            raw_event = item["event"]
            raw_debug.append({
                "bucket": item["bucket_id"],
                "event_id": raw_event.get("id"),
                "timestamp": raw_event.get("timestamp"),
                "duration": raw_event.get("duration", 0),
                "title": (raw_event.get("data") or {}).get("title"),
                "data": raw_event.get("data", {}),
                "event_start": iso(item["start"]),
                "event_end": iso(item["end"]),
            })

        return {
            "application": display_name,
            "application_id": application_id,
            "start": iso(start),
            "end": iso(end),
            "raw_aw_duration": raw_sum,
            "raw_aw_union_seconds": self._union_seconds(raw_intervals, start, end),
            "normalized_duration": normalized_sum,
            "normalized_union_seconds": self._union_seconds(normalized_intervals, start, end),
            "session_seconds": self._union_seconds(session_observed_intervals, start, end),
            "meaningful_session_seconds": self._union_seconds(meaningful_observed_intervals, start, end),
            "session_wall_seconds": self._union_seconds(session_intervals, start, end),
            "meaningful_session_wall_seconds": self._union_seconds(meaningful_intervals, start, end),
            "dashboard_seconds": dashboard_seconds,
            "raw_event_count": len(raw_rows),
            "stored_event_count": len(stored),
            "activity_session_count": len(sessions),
            "meaningful_session_count": len(meaningful),
            "raw_events": raw_debug,
            "normalized_events": [item.to_dict() for item in stored[:1000]],
            "activity_sessions": [item.to_dict() for item in sessions],
            "meaningful_sessions": [item.to_dict() for item in meaningful],
            "differences": {
                "aw_sum_minus_normalized_sum": round(raw_sum - normalized_sum, 3),
                "aw_union_minus_normalized_union": round(
                    self._union_seconds(raw_intervals, start, end)
                    - self._union_seconds(normalized_intervals, start, end), 3
                ),
                "normalized_union_minus_session_union": round(
                    self._union_seconds(normalized_intervals, start, end)
                    - self._union_seconds(session_observed_intervals, start, end), 3
                ),
                "session_union_minus_meaningful_union": round(
                    self._union_seconds(session_observed_intervals, start, end)
                    - self._union_seconds(meaningful_observed_intervals, start, end), 3
                ),
                "meaningful_union_minus_dashboard": round(
                    self._union_seconds(meaningful_observed_intervals, start, end) - dashboard_seconds, 3
                ),
                "aw_union_minus_dashboard": round(
                    self._union_seconds(raw_intervals, start, end) - dashboard_seconds, 3
                ),
            },
            "cursor": {
                "last_ingest_at": self.store.get_state("daemon.last_ingest_at"),
                "last_ingest_local_date": self.store.get_state("daemon.last_ingest_local_date"),
            },
        }

    def daily_summary(
        self,
        start: datetime,
        end: datetime,
        timezone_name: str = "UTC",
    ) -> dict:
        """Aggregate one calendar window without counting overlapping events twice."""

        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise ValueError("daily summary requires an aware, positive time window")
        # Dashboard calendar metrics describe the selected ActivityWatch day.
        # A manual pipeline reset must not hide earlier telemetry from the
        # real-time Today view; reset state only controls processing cursors.
        effective_start = start
        events = self.store.query(start=effective_start, end=end, limit=100000)
        # AFK-watcher rows are presence telemetry, never activity: split them
        # out for the presence timeline (PART 9 timeline intersection).
        afk_events = [item.event for item in events if is_presence_event(item.event)]
        detector = self.presence_detector or PresenceDetector()
        presence = detector.build_timeline(
            afk_events, None, start=effective_start, end=end, now=end)
        afk_intervals = sorted(
            (item for item in presence.intervals if item.state.value == "afk"),
            key=lambda item: (item.start, item.end),
        )
        window_events = [
            item for item in events
            if str(item.event.metadata.get("event_kind", "")).casefold() == "window"
            or str(item.event.metadata.get("activitywatch_bucket_type", "")).casefold() in {"currentwindow", "current_window"}
        ]
        # Window-kind events are the source for screen-time totals.
        # Keep the generic fallback for tests/imports that have no kind stamp.
        if window_events:
            events = window_events
        sessions = self.store.query_sessions(start=effective_start, end=end, limit=100000)
        classifications = {
            item.session_id: item
            for item in self.store.query_classifications(limit=100000)
            if item.classification_status == "classified" and item.source != "heuristic"
        }
        # The live pipeline classifies meaningful sessions, so a raw session
        # without its own row inherits its parent meaningful verdict. Direct
        # rows always win. Without this the summary would show unclassified
        # time that Results already displays as classified.
        meaningful_parents: dict[str, str] = {}
        for meaningful in self.store.query_meaningful_sessions(
            start=effective_start, end=end, limit=100000
        ):
            for raw_id in meaningful.activity_session_ids:
                meaningful_parents.setdefault(raw_id, meaningful.id)
        for session in sessions:
            if session.id not in classifications and session.id in meaningful_parents:
                parent = classifications.get(meaningful_parents[session.id])
                if parent is not None:
                    classifications[session.id] = parent
        event_sessions = {
            event_key: session.id
            for session in sessions
            for event_key in session.event_keys
        }

        # A raw watcher can emit overlapping heartbeats. Attribute each time
        # slice once, to the earliest event, so totals and breakdowns reconcile.
        # Every slice is then intersected with AFK intervals (PART 9): AFK
        # time becomes its own bucket and is never attributed as activity.
        def emit_piece(piece_event: Any, piece_start: datetime, piece_end: datetime) -> None:
            if piece_end <= piece_start:
                return
            sub_cursor = piece_start
            for afk in afk_intervals:
                if afk.end <= sub_cursor or afk.start >= piece_end:
                    continue
                if afk.start > sub_cursor:
                    segments.append(self._summary_segment(
                        piece_event, sub_cursor, min(afk.start, piece_end),
                        event_sessions, classifications))
                afk_piece_end = min(afk.end, piece_end)
                afk_piece_start = max(sub_cursor, afk.start)
                if afk_piece_end > afk_piece_start:
                    segments.append(self._afk_segment(piece_event, afk_piece_start, afk_piece_end))
                sub_cursor = max(sub_cursor, afk.end)
                if sub_cursor >= piece_end:
                    break
            if sub_cursor < piece_end:
                segments.append(self._summary_segment(
                    piece_event, sub_cursor, piece_end, event_sessions, classifications))

        occupied: list[tuple[datetime, datetime]] = []
        segments: list[dict] = []
        for stored in sorted(events, key=lambda item: (item.event.timestamp, item.event.event_key)):
            event = stored.event
            event_start = max(event.timestamp, effective_start)
            event_end = min(event.end_timestamp, end)
            if event_end <= event_start:
                continue
            cursor = event_start
            for occupied_start, occupied_end in occupied:
                if occupied_end <= cursor or occupied_start >= event_end:
                    continue
                if occupied_start > cursor:
                    emit_piece(event, cursor, min(occupied_start, event_end))
                cursor = max(cursor, occupied_end)
                if cursor >= event_end:
                    break
            if cursor < event_end:
                emit_piece(event, cursor, event_end)
            occupied.append((event_start, event_end))
            occupied.sort()

        totals = defaultdict(float)
        applications = defaultdict(float)
        application_categories = defaultdict(set)
        distractions = defaultdict(float)
        timeline = []
        for segment in segments:
            seconds = segment["seconds"]
            totals[segment["aggregation_productivity"]] += seconds
            applications[segment["application"]] += seconds
            application_categories[segment["application"]].add(segment["aggregation_productivity"])
            if segment["aggregation_productivity"] == "distractive":
                distractions[segment["activity"]] += seconds
            timeline.append(segment)
        timeline = self._merge_summary_segments(timeline)
        top_activities = defaultdict(float)
        for segment in timeline:
            top_activities[segment["activity"]] += segment["seconds"]
        total = sum(totals.values())
        productive = totals["productive"]
        unclassified = totals["unclassified"]
        idle = totals["afk"]
        active_total = total - idle
        classified_total = productive + totals["distractive"] + totals["neutral"]
        return {
            "date": start.astimezone(ZoneInfo(timezone_name)).date().isoformat(),
            "timezone": timezone_name,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "total_time": round(total, 1),
            "total_tracked_seconds": round(total, 1),
            "screen_on_time": round(total, 1),
            "productive_time": round(productive, 1),
            "productive_seconds": round(productive, 1),
            "distractive_time": round(totals["distractive"], 1),
            "distractive_seconds": round(totals["distractive"], 1),
            "neutral_time": round(totals["neutral"], 1),
            "neutral_seconds": round(totals["neutral"], 1),
            "unclassified_time": round(unclassified, 1),
            "unclassified_seconds": round(unclassified, 1),
            "classified_time": round(classified_total, 1),
            "classified_seconds": round(classified_total, 1),
            "afk_time": round(idle, 1),
            "afk_seconds": round(idle, 1),
            "active_time": round(active_total, 1),
            "active_seconds": round(active_total, 1),
            "coverage_percent": round(classified_total / active_total * 100, 1) if active_total else None,
            "productivity_score": round(productive / classified_total * 100) if classified_total else None,
            "productivity_percentage": round(productive / classified_total * 100, 1) if classified_total else None,
            "applications": [
                {"name": name, "seconds": round(seconds, 1), "category": (
                    next(iter(application_categories[name]))
                    if len(application_categories[name]) == 1
                    else "mixed"
                )}
                for name, seconds in sorted(applications.items(), key=lambda item: (-item[1], item[0]))
            ],
            "distractions": [
                {"name": name, "seconds": round(seconds, 1)}
                for name, seconds in sorted(distractions.items(), key=lambda item: (-item[1], item[0]))
            ],
            "top_applications": [
                {"name": name, "seconds": round(seconds, 1), "category": (
                    next(iter(application_categories[name]))
                    if len(application_categories[name]) == 1 else "mixed"
                )}
                for name, seconds in sorted(applications.items(), key=lambda item: (-item[1], item[0]))[:10]
            ],
            "top_activities": [
                {"name": name, "seconds": round(seconds, 1)}
                for name, seconds in sorted(top_activities.items(), key=lambda item: (-item[1], item[0]))[:10]
            ],
            "timeline": timeline,
        }

    @staticmethod
    def _merge_summary_segments(segments: list[dict]) -> list[dict]:
        """Merge adjacent fragments with the same logical activity."""
        merged: list[dict] = []
        for segment in sorted(segments, key=lambda item: item["timestamp"]):
            if not merged:
                merged.append(dict(segment))
                continue
            previous = merged[-1]
            same = (
                previous["application"].casefold() == segment["application"].casefold()
                and previous["activity"].casefold() == segment["activity"].casefold()
                and previous["productivity"] == segment["productivity"]
                and previous["subcategory"] == segment["subcategory"]
            )
            previous_end = datetime.fromisoformat(previous["end"].replace("Z", "+00:00"))
            current_start = datetime.fromisoformat(segment["timestamp"].replace("Z", "+00:00"))
            if same and (current_start - previous_end).total_seconds() <= 5:
                previous["end"] = segment["end"]
                previous["seconds"] = round(previous["seconds"] + segment["seconds"], 3)
                previous["confidence"] = max(previous["confidence"], segment["confidence"])
            else:
                merged.append(dict(segment))
        return merged

    @staticmethod
    def _afk_segment(event: Any, start: datetime, end: datetime) -> dict:
        """One idle slice: presence state, never a semantic verdict (PART 10)."""
        application_id, display_name = application_identity(event.app or event.browser, event.title)
        application = str(event.metadata.get("application_display_name") or display_name)
        return {
            "timestamp": start.isoformat(),
            "end": end.isoformat(),
            "seconds": round((end - start).total_seconds(), 3),
            "application": application,
            "application_id": application_id,
            "activity": "Idle — no user input",
            "title": event.title,
            "domain": event.domain,
            "productivity": "afk",
            "aggregation_productivity": "afk",
            "confidence": 0.0,
            "category": None,
            "subcategory": "afk",
            "activity_description": "Away from keyboard — no user input",
            "signal": "AFK interval from input-timing telemetry; not classified",
            "source": "presence",
            "classification_status": "afk",
        }

    @staticmethod
    def _summary_segment(event: Any, start: datetime, end: datetime, event_sessions: dict, classifications: dict) -> dict:
        session_id = event_sessions.get(event.event_key)
        classification = classifications.get(session_id)
        classified_productivity = None
        if classification:
            classified_productivity = "distractive" if classification.category == "distractive" or classification.productivity == "distracting" else classification.category
            if classified_productivity not in {"productive", "distractive", "neutral"}:
                classified_productivity = "neutral"
        # Unclassified time is NEVER neutral: pending/failed/missing verdicts
        # aggregate into their own bucket so coverage gaps stay visible.
        productivity = classified_productivity or "unclassified"
        application_id, display_name = application_identity(event.app or event.browser, event.title)
        application = str(event.metadata.get("application_display_name") or display_name)
        activity = str(event.domain or event.title or event.url or application)
        return {
            "timestamp": start.isoformat(),
            "end": end.isoformat(),
            # Keep millisecond-level precision in the timeline. The UI can
            # format this for humans, while reconciliation must not lose
            # fractions of a second by rounding every fragment to tenths.
            "seconds": round((end - start).total_seconds(), 3),
            "application": application,
            "application_id": application_id,
            "activity": activity,
            "title": event.title,
            "domain": event.domain,
            "productivity": productivity,
            "aggregation_productivity": productivity,
            "confidence": classification.confidence if classification else 0.0,
            "category": classification.category if classification else None,
            "subcategory": classification.subcategory if classification else "ambiguous",
            "activity_description": classification.activity if classification else "Ambiguous activity",
            "signal": classification.signal if classification else "Classification unavailable",
            "source": classification.source if classification else "none",
            "classification_status": classification.classification_status if classification else "pending",
            "evidence_quality": classification.evidence_quality if classification else None,
            "evidence_quality_score": classification.evidence_quality_score if classification else None,
        }

    def events_with_classifications(self, events: Iterable[Any]) -> list:
        """Attach the latest session classification to each raw event."""

        sessions = self.store.query_sessions(limit=10000)
        classifications = {
            item.session_id: item
            for item in self.store.query_classifications(limit=10000)
            if item.classification_status == "classified" and item.source != "heuristic"
        }
        event_session_ids = {
            event_key: session.id
            for session in sessions
            for event_key in session.event_keys
        }
        enriched = []
        for stored_event in events:
            payload = stored_event.to_dict()
            payload["classification_status"] = "pending"
            session_id = event_session_ids.get(stored_event.event.event_key)
            classification = classifications.get(session_id)
            if classification is not None:
                payload.update({
                    "category": classification.category,
                    "topic": classification.topic,
                    "activity_type": classification.activity_type,
                    "productivity": classification.productivity,
                    "classification_confidence": classification.confidence,
                    "subcategory": classification.subcategory,
                    "activity": classification.activity,
                    "signal": classification.signal,
                    "classification_source": classification.source,
                    "classification_status": classification.classification_status,
                    "evidence_quality": classification.evidence_quality,
                    "evidence_quality_score": classification.evidence_quality_score,
                    "provider": classification.provider,
                    "model": classification.model,
                })
            enriched.append(payload)
        return enriched

    def query_classifications_for_window(
        self,
        start: Any,
        end: Any,
        category: Optional[str] = None,
        productivity: Optional[str] = None,
        limit: int = 1000,
    ) -> list:
        """Return classifications belonging to sessions in the exact window.

        ``semantic_classifications`` is historical storage, so its insertion
        order is never a valid substitute for a dashboard time filter.
        """

        session_ids = {
            item.id for item in self.store.query_sessions(start=start, end=end, limit=100000)
        }
        session_ids.update(
            item.id for item in self.store.query_meaningful_sessions(start=start, end=end, limit=100000)
        )
        rows = [
            item for item in self.store.query_classifications(
                category=category, productivity=productivity, limit=100000
            )
            if item.classification_status == "classified" and item.source != "heuristic"
        ]
        return [item for item in rows if item.session_id in session_ids][:limit]

    @staticmethod
    def _classification_payload(classification: Optional[Classification]) -> dict:
        if classification is None:
            return {
                "category": None,
                "status": "pending",
                "provider": None,
                "model": None,
                "source": None,
            }
        return {
            "category": classification.category,
            "status": classification.classification_status,
            "provider": classification.provider,
            "model": classification.model,
            "source": classification.source,
            "confidence": classification.confidence,
            "topic": classification.topic,
            "service": classification.service,
            "activity_type": classification.activity_type,
            "intent_signal": classification.intent_signal,
            "evidence_quality": classification.evidence_quality,
            "evidence_quality_score": classification.evidence_quality_score,
            "classified_at": classification.classified_at.isoformat().replace("+00:00", "Z") if classification.classified_at else None,
        }

    def recent_activity(self, start: Any, end: Any, limit: int = 100) -> list:
        """Return logical ActivitySessions, not one row per watcher heartbeat."""

        sessions = self.store.query_sessions(start=start, end=end, limit=100000)
        classifications = {
            item.session_id: item
            for item in self.store.query_classifications(limit=100000)
            if item.classification_status == "classified" and item.source != "heuristic"
        }
        # The live pipeline classifies meaningful sessions, so a raw session
        # without its own row inherits its parent meaningful verdict.
        parent_of = {}
        for meaningful in self.store.query_meaningful_sessions(start=start, end=end, limit=100000):
            for raw_id in meaningful.activity_session_ids:
                parent_of.setdefault(raw_id, meaningful.id)
        rows = []
        for session in sessions:
            classification = classifications.get(session.id)
            inherited_from = None
            if classification is None:
                parent_id = parent_of.get(session.id)
                classification = classifications.get(parent_id)
                if classification is not None:
                    # One episode verdict rendered on many raw rows: mark
                    # the inheritance explicitly so the UI never implies
                    # independent model decisions.
                    inherited_from = parent_id
            application_id, application = application_identity(session.app or session.browser, session.title)
            item = {
                "id": session.id,
                "session_id": session.id,
                "timestamp": session.start.isoformat().replace("+00:00", "Z"),
                "end": session.end.isoformat().replace("+00:00", "Z"),
                "duration_seconds": round(session.duration, 3),
                "application": application,
                "application_id": application_id,
                "title": session.title,
                "domain": session.domain,
                "url": session.url,
                "device": session.device,
                "browser": session.browser,
                "window_id": session.browser_window_id,
                "tab_id": session.browser_tab_id,
                "classification": self._classification_payload(classification),
                "category": classification.category if classification else None,
                "productivity": classification.productivity if classification else None,
                "confidence": classification.confidence if classification else 0.0,
                "signal": classification.signal if classification else None,
                "classification_status": classification.classification_status if classification else "pending",
                "classification_source": classification.source if classification else None,
                "service": classification.service if classification else None,
                "topic": classification.topic if classification else None,
                "activity_type": classification.activity_type if classification else None,
                "intent_signal": classification.intent_signal if classification else None,
                "evidence_quality": classification.evidence_quality if classification else None,
                "evidence_quality_score": classification.evidence_quality_score if classification else None,
                "provider": classification.provider if classification else None,
                "model": classification.model if classification else None,
                "inherited_from": inherited_from,
            }
            rows.append(item)
        rows.sort(key=lambda item: (item["timestamp"], item["id"]), reverse=True)
        return rows[:limit]

    def current_activity(self, now: Optional[Any] = None) -> dict:
        """Return latest normalized ActivityWatch telemetry independently of AI."""

        current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
        events = self.store.query(end=current, limit=100000)
        if not events:
            return {"telemetry_status": "unavailable", "activity": None, "classification": {"status": "pending"}}
        stored = max(events, key=lambda item: item.event.end_timestamp)
        event = stored.event
        sessions = self.store.query_sessions(start=event.timestamp - timedelta(seconds=2), end=event.end_timestamp + timedelta(seconds=2), limit=100000)
        session_id = next((session.id for session in sessions if event.event_key in session.event_keys), None)
        classification = None
        if session_id:
            found = self.store.query_classifications(session_id=session_id, limit=1)
            classification = found[0] if found and found[0].classification_status == "classified" and found[0].source != "heuristic" else None
        if classification is None and session_id:
            for meaningful in self.store.query_meaningful_sessions(
                start=event.timestamp - timedelta(days=7),
                end=event.end_timestamp + timedelta(seconds=2),
                limit=100000,
            ):
                if session_id in meaningful.activity_session_ids:
                    found = self.store.query_classifications(session_id=meaningful.id, limit=1)
                    if found and found[0].classification_status == "classified" and found[0].source != "heuristic":
                        classification = found[0]
                    break
        application_id, application = application_identity(event.app or event.browser, event.title)
        age = max(0.0, (current - event.end_timestamp).total_seconds())
        return {
            "application": application,
            "application_id": application_id,
            "title": event.title,
            "domain": event.domain,
            "url": event.url,
            "duration_seconds": round(event.duration, 3),
            "timestamp": event.timestamp.isoformat().replace("+00:00", "Z"),
            "end": event.end_timestamp.isoformat().replace("+00:00", "Z"),
            "device": event.device,
            "browser": event.browser,
            "window_id": event.browser_window_id,
            "tab_id": event.browser_tab_id,
            "session_id": session_id,
            "telemetry_status": "live" if age <= 10 else "stale",
            "telemetry_age_seconds": round(age, 3),
            "classification": self._classification_payload(classification),
        }

    def provider_quotas(self) -> dict:
        """Per-model quota table: limits vs today's measured use vs left.

        Combines the in-memory rate-limit windows with the persisted usage
        ledger, so a daemon restart cannot hide spend. Anything without a
        hosted chain (plain Ollama) reports unlimited local capacity.
        """
        import time as _time

        from noema.infrastructure.providers import (
            FAST_DISTRACTION_MODEL,
            MODEL_CONTEXT_WINDOWS,
            MODEL_LIMITS,
            PROVIDER_QUOTA_RESET_TZ,
            WORKHORSE_MODEL,
            ESCALATION_MODEL,
            openrouter_registry,
        )

        provider = getattr(self.classifier, "provider", None)
        states = getattr(provider, "rate_limits", None)
        ledger_snapshot = {}
        ledger_usage = {}
        if hasattr(provider, "usage") and callable(provider.usage):
            try:
                ledger_snapshot = dict(provider.usage() or {})
            except (AttributeError, OSError, ValueError):
                ledger_snapshot = {}
            ledger_usage = ledger_snapshot
        day = datetime.now(timezone.utc).date().isoformat()
        day_timezone = "UTC"
        latency_ms = {}
        latencies = getattr(provider, "model_latency_ms", None)
        if isinstance(latencies, dict):
            latency_ms = {str(key): value for key, value in latencies.items()}
        ledger = getattr(provider, "ledger", None)
        if ledger is not None:
            try:
                day = ledger.day
                day_timezone = getattr(ledger, "day_timezone", day_timezone)
            except (AttributeError, OSError, ValueError):
                pass
        if not isinstance(states, dict) or not states:
            return {
                "day": day,
                "provider": getattr(provider, "name", None),
                "ollama_fallback": None,
                "models": [],
                "embeddings": [],
                "note": "no hosted quota applies to this provider",
            }
        models = []
        registry_by_id = {entry["id"]: entry for entry in openrouter_registry()}
        for model, state in states.items():
            try:
                state.available(0)
            except (AttributeError, TypeError):
                pass
            used_day = max(int(getattr(state, "requests_today", 0) or 0),
                           int(ledger_usage.get("llm:{}".format(model), {}).get("requests", 0) or 0))
            used_min = int(getattr(state, "attempts_this_minute", 0) or 0)
            used_tpm = int(getattr(state, "tokens_this_minute", 0) or 0)
            rpm = int(getattr(state, "rpm_limit", 0) or 0)
            tpm = int(getattr(state, "tpm_limit", 0) or 0)
            rpd = int(getattr(state, "rpd_limit", 0) or 0)
            cooldown = max(0.0, float(getattr(state, "cooldown_until", 0.0) or 0.0) - _time.monotonic())
            if used_day >= rpd:
                status = "exhausted"
            elif cooldown > 0:
                status = "cooldown"
            else:
                status = "ok"
            row = {
                "model": str(model),
                "provider": str(getattr(state, "provider", "")),
                "role": (
                    "workhorse" if str(model) == WORKHORSE_MODEL
                    else "escalation" if str(model) == ESCALATION_MODEL
                    else "fallback"
                ),
                "rpm": {"limit": rpm, "used": used_min, "left": max(0, rpm - used_min)},
                "tpm": {"limit": tpm, "used": used_tpm, "left": max(0, tpm - used_tpm)},
                "rpd": {"limit": rpd, "used": used_day, "left": max(0, rpd - used_day)},
                # Attempts vs successes are separate concepts: one HTTP/model
                # request is one attempt; only successes consume daily budget.
                "attempts_today": int(getattr(state, "attempts_today", 0) or 0),
                "attempts_this_minute": int(getattr(state, "attempts_this_minute", 0) or 0),
                "rate_limited_count": int(getattr(state, "rate_limited_count", 0) or 0),
                "last_rate_limited_at": getattr(state, "last_rate_limited_at", None),
                "tokens_today": int(ledger_usage.get("llm:{}".format(model), {}).get("tokens", 0) or 0),
                "last_latency_ms": latency_ms.get(str(model)),
                "cooldown_seconds": round(cooldown, 1),
                "consecutive_failures": int(getattr(state, "consecutive_failures", 0) or 0),
                "last_error": getattr(state, "last_error", None),
                "last_success": getattr(state, "last_success", None),
                "status": status,
                "context_window": MODEL_CONTEXT_WINDOWS.get(str(model)),
                # Gemini rows are model-specific quotas; OpenRouter free rows
                # are conservative guards against ONE shared account pool.
                "quota_scope": "model-specific",
            }
            registry_entry = registry_by_id.get(str(model))
            if registry_entry is not None:
                row["role"] = registry_entry["role"]
                row["purpose"] = registry_entry["purpose"]
                row["capabilities"] = list(registry_entry["capabilities"])
                row["fast_path"] = bool(registry_entry["fast_path"])
                row["excludes"] = list(registry_entry["excludes"])
                row["display_name"] = registry_entry["display_name"]
                row["vendor"] = registry_entry["vendor"]
                row["max_output"] = registry_entry["max_output"]
                row["normal_classification"] = bool(registry_entry["normal_classification"])
                row["enabled"] = bool(registry_entry["enabled"])
                row["priority"] = int(registry_entry["priority"])
                row["quota_scope"] = "shared-openrouter-guards"
            models.append(row)
        embeddings = []
        for model, (rpm, tpm, rpd) in sorted(MODEL_LIMITS.items()):
            if "embedding" not in str(model):
                continue
            used = ledger_usage.get("embedding:{}".format(model), {})
            used_day = int(used.get("requests", 0) or 0)
            embeddings.append({
                "model": str(model),
                "rpm": {"limit": rpm, "used": None, "left": None},
                "tpm": {"limit": tpm, "used": None, "left": None},
                "rpd": {"limit": rpd, "used": used_day, "left": max(0, rpd - used_day)},
                "tokens_today": int(used.get("tokens", 0) or 0),
                "status": "exhausted" if used_day >= rpd else "ok",
                # Honest scope: only daily counts are tracked for embeddings;
                # per-minute usage is untracked, hence null (never zero).
                "quota_scope": "model-specific-rpd-only",
            })
        import os as _os

        openrouter_key_env = None
        openrouter_tier_present = False
        for row_provider in (getattr(provider, "providers", None) or []):
            if getattr(row_provider, "name", "") == "openrouter":
                openrouter_tier_present = True
                openrouter_key_env = getattr(row_provider, "api_key_env", None)
                break
        return {
            "day": day,
            "day_timezone": day_timezone,
            "provider": getattr(provider, "name", None),
            "ollama_fallback": bool(getattr(provider, "include_ollama", False)),
            "openrouter_configured": bool(
                openrouter_key_env and _os.environ.get(openrouter_key_env, "").strip()
            ),
            # Canonical registry: roles/purposes/windows live in
            # OPENROUTER_MODEL_REGISTRY; the free tier shares account-level
            # limits, so per-model guards above deliberately under-use.
            # Retired tiers report no shared quota and no note so dead
            # models never render as cards in the UI.
            "openrouter_shared_quota": bool(openrouter_tier_present),
            "openrouter_quota_note": (
                "OpenRouter free tier shares account-level limits; "
                "per-model guards under-use by design."
            ) if openrouter_tier_present else None,
            "fast_distraction_model": FAST_DISTRACTION_MODEL,
            "openrouter_registry": openrouter_registry(),
            # Quota vocabulary, stated plainly: configured limits are
            # operator-supplied; local usage counts local successful
            # requests; provider-remaining is unavailable unless the
            # provider reports it (it currently does not).
            "quota_day_timezone": day_timezone,
            "provider_reset_timezone": PROVIDER_QUOTA_RESET_TZ,
            "rpd_policy": "local-success-count; provider state unavailable",
            "models": models,
            "embeddings": embeddings,
        }

    def classifier_debug(self, now: Optional[Any] = None) -> dict:
        """Expose classifier provenance and bounded-job diagnostics."""

        payload = self.classifier.debug_telemetry() if hasattr(self.classifier, "debug_telemetry") else {}
        current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
        start = current - timedelta(seconds=1800)
        sessions = self.store.query_meaningful_sessions(start=start, end=current, limit=100000)
        classifications = {item.session_id: item for item in self.store.query_classifications(limit=100000)}
        payload["pending_jobs"] = sum(
            1 for item in sessions
            if item.id not in classifications
            or classifications[item.id].source == "heuristic"
            or classifications[item.id].classification_status in {"pending", "classification_failed"}
        )
        return payload

    def afk_status_events(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        limit: int = 100000,
    ) -> list:
        """Presence telemetry rows (AFK-watcher status), never activity."""

        stored = self.store.query(start, end, None, None, limit)
        return [item.event for item in stored if is_presence_event(item.event)]

    def build_presence_timeline(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        now: Optional[Any] = None,
        afk_events: Optional[Iterable[ActivityEvent]] = None,
    ):
        """Presence over a window from stored afk-status rows (PART 3-5).

        No extra database round trip when the caller already holds the
        window's events: pass them as ``afk_events`` (mixed lists are fine;
        non-presence rows are ignored).
        """

        detector = self.presence_detector or PresenceDetector()
        if afk_events is None:
            rows = self.afk_status_events(start, end)
        else:
            rows = [event for event in afk_events if is_presence_event(event)]
        return detector.build_timeline(rows, None, start=start, end=end, now=now)

    def sessionize_events(
        self,
        events: Iterable[ActivityEvent],
        persist: bool = True,
        presence: Any = None,
    ) -> list:
        """Build Generation 1 sessions from already-filtered events."""

        sessions = self.sessionizer.sessionize(events, presence=presence)
        if persist:
            self.store.insert_sessions(sessions)
        return sessions

    def sessionize_stored_events(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        device: Optional[str] = None,
        domain: Optional[str] = None,
        limit: int = 1000,
        persist: bool = True,
    ) -> list:
        """Sessionize the AI database's normalized event view.

        Presence is derived from the same window's afk-status rows, so AFK
        spans split sessions and AFK telemetry never becomes activity.
        Windows without any afk data behave exactly as before.
        """

        with self._observe_db("session_rebuild") as state:
            stored = self.store.query(start, end, device, domain, limit)
            events = [item.event for item in stored]
            timeline = self.build_presence_timeline(
                start, end, afk_events=[event for event in events if is_presence_event(event)])
            sessions = self.sessionize_events(events, persist=persist, presence=timeline)
            state["rows_affected"] = len(sessions)
            return sessions

    def query_sessions(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_sessions(*args, **kwargs)

    # Retry policy for failed classification attempts. Delays grow as
    # 10/20/40/80/120 minutes and stop after CLASSIFICATION_RETRIES_MAX
    # attempts; the row then stays terminally failed with its last error.
    RETRY_BACKOFF_BASE_MINUTES = 5.0
    RETRY_BACKOFF_MAX_MINUTES = 120.0
    CLASSIFICATION_RETRIES_MAX = 5
    CLASSIFIER_VERSION_DEFAULT = "1"

    @staticmethod
    def retry_delay_minutes(retry_count: int) -> float:
        """Minutes to wait before attempt number ``retry_count`` (1-based)."""

        return min(
            NoemaService.RETRY_BACKOFF_MAX_MINUTES,
            NoemaService.RETRY_BACKOFF_BASE_MINUTES * (2.0 ** max(1, int(retry_count))),
        )

    def _apply_retry_accounting(
        self,
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
            next_retry = now + timedelta(minutes=self.retry_delay_minutes(count))
        version = getattr(existing, "classifier_version", None) or classifier_version
        return replace(
            result,
            retry_count=count,
            last_error=error,
            next_retry_at=next_retry,
            classifier_version=version,
        )

    def _is_eligible(
        self,
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
    def _is_afk_only(session: Any) -> bool:
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
        self,
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
        """

        current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
        candidates = list(sessions)
        stored = self.store.query_classification_map([item.id for item in candidates])
        return [
            item for item in candidates
            if not self._is_afk_only(item)
            and self._is_eligible(stored.get(item.id), current, classifier_version, max_retries)
        ]

    def presence_snapshot(self, now: Optional[Any] = None) -> dict:
        """Current presence + current session/AFK summary for dashboards.

        Presence state comes from afk-watcher rows (device truth) blended
        with the live browser bridge's last report time. The current session
        is the latest meaningful session; its stored classification (if any)
        is attached. Nothing here is inferred from window focus alone.
        """

        from noema.domain.presence import PresenceState

        current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
        detector = self.presence_detector or PresenceDetector()
        lookback = current - timedelta(hours=24)
        afk_events = self.afk_status_events(start=lookback, end=current)
        bridge_seen: Optional[datetime] = None
        try:
            snapshots = self.browser_bridge.snapshot()
        except (AttributeError, TypeError):
            snapshots = []
        for registration in snapshots or []:
            seen = registration.get("last_seen_at") if isinstance(registration, dict) else None
            try:
                seen_at = coerce_timestamp(seen)
            except (AttributeError, TypeError, ValueError):
                continue
            if seen_at <= current and (bridge_seen is None or seen_at > bridge_seen):
                bridge_seen = seen_at
        presence = detector.current_state(
            afk_events, None, bridge_last_seen_at=bridge_seen, now=current)
        snapshot: dict = {
            "state": presence["state"],
            "last_input_at": presence["last_input_at"],
            "idle_seconds": presence["idle_seconds"],
            "as_of": presence["as_of"],
            "stale": presence["stale"],
            "current_session": None,
            "afk_since": None,
            "last_activity": None,
        }
        if presence["state"] == PresenceState.AFK.value:
            try:
                timeline = detector.build_timeline(afk_events, None, start=lookback, end=current, now=current)
                for interval in reversed(timeline.intervals):
                    if interval.state == PresenceState.AFK and interval.start <= current:
                        snapshot["afk_since"] = interval.start.isoformat().replace("+00:00", "Z")
                        break
            except (AttributeError, TypeError, ValueError):
                pass
        recent_sessions = self.store.query_sessions(start=lookback, end=current, limit=100000)
        if recent_sessions:
            latest_raw = max(recent_sessions, key=lambda item: item.end)
            snapshot["last_activity"] = {
                "app": latest_raw.app,
                "title": latest_raw.title,
                "domain": latest_raw.domain,
                "at": latest_raw.end.isoformat().replace("+00:00", "Z"),
            }
        meaningful = self.store.query_meaningful_sessions(start=lookback, end=current, limit=100000)
        if meaningful:
            latest = max(meaningful, key=lambda item: item.end_time)
            entry: dict = {
                "session_id": latest.id,
                "start": latest.start_time.isoformat().replace("+00:00", "Z"),
                "end": latest.end_time.isoformat().replace("+00:00", "Z"),
                "active_duration_seconds": round(latest.active_duration_seconds, 1),
                "afk_duration_seconds": round(latest.afk_duration_seconds, 1),
                "presence": (
                    "active" if latest.active_duration_seconds > 0
                    else "afk" if latest.afk_duration_seconds > 0
                    else "unknown"
                ),
                "application": (latest.evidence or [None])[0],
                "context": latest.primary_topic or latest.primary_task,
                "classification": None,
            }
            found = self.store.query_classifications(session_id=latest.id, limit=1)
            if found and found[0].classification_status == "classified" and found[0].source != "heuristic":
                entry["classification"] = {
                    "category": found[0].category,
                    "confidence": found[0].confidence,
                    "source": found[0].source,
                    "status": found[0].classification_status,
                }
            snapshot["current_session"] = entry
        return snapshot

    def fast_distraction_check(self, now: Optional[Any] = None) -> dict:
        """Advisory live distraction verdict for the current session.

        Read-only by contract: this never writes classifications, never
        touches the scheduler, and never consults window focus beyond what
        ``presence_snapshot`` already reports. AFK returns immediately
        without any model call (presence is device truth, not a verdict).
        Otherwise exactly ONE single call goes to the registry's fast-path
        OpenRouter model, guarded by the same per-minute + persisted daily
        quota checks as the batch chain. Every outcome — including skips —
        is an explicit payload, never an exception.
        """

        import time as _time

        from noema.infrastructure.providers import FAST_DISTRACTION_MODEL

        current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
        try:
            snapshot = self.presence_snapshot(now=current)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            return {
                "checked_at": current.isoformat().replace("+00:00", "Z"),
                "state": "unknown",
                "verdict": "unclear",
                "confidence": 0.0,
                "reason": "presence snapshot unavailable: {}".format(exc),
                "model": None,
                "advisory": True,
                "stored": False,
            }
        checked_at = current.isoformat().replace("+00:00", "Z")
        if snapshot.get("state") == PresenceState.AFK.value:
            return {
                "checked_at": checked_at,
                "state": "afk",
                "verdict": "afk",
                "confidence": 1.0,
                "reason": "no input device activity; presence is AFK, not a focus verdict",
                "model": None,
                "model_calls": 0,
                "session_id": (snapshot.get("current_session") or {}).get("session_id"),
                "advisory": True,
                "stored": False,
            }
        session = snapshot.get("current_session") or {}
        session_id = session.get("session_id")
        if not session_id:
            return {
                "checked_at": checked_at,
                "state": snapshot.get("state") or "unknown",
                "verdict": "unclear",
                "confidence": 0.0,
                "reason": "no current meaningful session to check",
                "model": None,
                "model_calls": 0,
                "advisory": True,
                "stored": False,
            }
        chain = getattr(self.classifier, "provider", None)
        candidates = list(getattr(chain, "providers", None) or [])
        fast_provider = None
        for provider in candidates:
            if getattr(provider, "name", "") == "openrouter" and getattr(
                provider, "model", None
            ) == FAST_DISTRACTION_MODEL:
                fast_provider = provider
                break
        if fast_provider is None:
            for provider in candidates:
                if getattr(provider, "name", "") == "openrouter":
                    fast_provider = provider
                    break
        if fast_provider is None:
            return {
                "checked_at": checked_at,
                "state": snapshot.get("state") or "unknown",
                "verdict": "unclear",
                "confidence": 0.0,
                "reason": "OpenRouter tier is not configured on this daemon",
                "model": None,
                "model_calls": 0,
                "session_id": session_id,
                "advisory": True,
                "stored": False,
            }
        try:
            fast_provider._api_key()
        except Exception:
            return {
                "checked_at": checked_at,
                "state": snapshot.get("state") or "unknown",
                "verdict": "unclear",
                "confidence": 0.0,
                "reason": "OPENROUTER_API_KEY is not set; fast check disabled",
                "model": getattr(fast_provider, "model", None),
                "model_calls": 0,
                "session_id": session_id,
                "advisory": True,
                "stored": False,
            }
        last_activity = snapshot.get("last_activity") or {}
        stored_classification = session.get("classification") or {}
        prompt = (
            "You are a focus monitor. Look at ONE current desktop session and "
            "answer ONLY whether it looks like focused work or distraction. "
            "Reply with a single JSON object "
            '{"verdict":"focusing | distracted | unclear",'
            '"confidence":0.0,"reason":"short why"}. '
            "Session: app={app} title={title} domain={domain} "
            "active_seconds={active} stored_verdict={verdict}. "
            "AFK state is handled elsewhere; never answer afk.".format(
                app=str(session.get("application") or last_activity.get("app") or "?")[:80],
                title=str(last_activity.get("title") or session.get("context") or "?")[:160],
                domain=str(last_activity.get("domain") or "?")[:80],
                active=session.get("active_duration_seconds"),
                verdict=str(stored_classification.get("category") or "?")[:40],
            )
        )
        usable = getattr(chain, "_model_usable", None)
        try:
            billable = int(usable(fast_provider, prompt)) if callable(usable) else 0
        except Exception:
            billable = -1
        if billable < 0:
            state = (getattr(chain, "rate_limits", None) or {}).get(
                getattr(fast_provider, "model", None))
            quota = None
            if state is not None:
                quota = {
                    "used": int(getattr(state, "requests_today", 0) or 0),
                    "limit": int(getattr(state, "rpd_limit", 0) or 0),
                }
            return {
                "checked_at": checked_at,
                "state": snapshot.get("state") or "unknown",
                "verdict": "unclear",
                "confidence": 0.0,
                "reason": "fast model quota exhausted or cooling down; skipped without a call",
                "model": getattr(fast_provider, "model", None),
                "model_calls": 0,
                "session_id": session_id,
                "quota": quota,
                "advisory": True,
                "stored": False,
            }
        started = _time.perf_counter()
        try:
            payload = fast_provider.classify(None, prompt)
        except Exception as exc:
            state = (getattr(chain, "rate_limits", None) or {}).get(
                getattr(fast_provider, "model", None))
            if state is not None:
                try:
                    state.record_failure(exc)
                except Exception:
                    pass
            return {
                "checked_at": checked_at,
                "state": snapshot.get("state") or "unknown",
                "verdict": "unclear",
                "confidence": 0.0,
                "reason": "fast model call failed: {}: {}".format(
                    type(exc).__name__, str(exc)[:160]),
                "model": getattr(fast_provider, "model", None),
                "model_calls": 1,
                "session_id": session_id,
                "advisory": True,
                "stored": False,
            }
        latency_ms = round((_time.perf_counter() - started) * 1000, 1)
        verdict = str((payload or {}).get("verdict", "")).strip().lower()
        try:
            confidence = float((payload or {}).get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str((payload or {}).get("reason", "") or "")[:280]
        if verdict not in {"focusing", "distracted", "unclear"} or not 0.0 <= confidence <= 1.0:
            state = (getattr(chain, "rate_limits", None) or {}).get(
                getattr(fast_provider, "model", None))
            if state is not None:
                try:
                    from noema.infrastructure.providers import ProviderError

                    state.record_failure(ProviderError("fast check returned an invalid verdict"))
                except Exception:
                    pass
            return {
                "checked_at": checked_at,
                "state": snapshot.get("state") or "unknown",
                "verdict": "unclear",
                "confidence": 0.0,
                "reason": "fast model returned an unusable verdict; ignored",
                "model": getattr(fast_provider, "model", None),
                "model_calls": 1,
                "latency_ms": latency_ms,
                "session_id": session_id,
                "advisory": True,
                "stored": False,
            }
        state = (getattr(chain, "rate_limits", None) or {}).get(
            getattr(fast_provider, "model", None))
        ledger = getattr(chain, "ledger", None)
        quota = None
        if state is not None:
            try:
                state.record_success(billable)
            except Exception:
                pass
            if ledger is not None:
                try:
                    ledger.consume("llm", getattr(fast_provider, "model", None), billable)
                except Exception:
                    pass
            quota = {
                "used": int(getattr(state, "requests_today", 0) or 0),
                "limit": int(getattr(state, "rpd_limit", 0) or 0),
            }
        return {
            "checked_at": checked_at,
            "state": snapshot.get("state") or "unknown",
            "verdict": verdict,
            "confidence": round(confidence, 2),
            "reason": reason,
            "model": getattr(fast_provider, "model", None),
            "model_calls": 1,
            "latency_ms": latency_ms,
            "session_id": session_id,
            "quota": quota,
            "advisory": True,
            "stored": False,
        }

    def query_detections(self, session_id: Optional[str] = None, limit: int = 100) -> list:
        return [item.to_dict() for item in self.store.query_detections(
            session_id=session_id, limit=limit)]

    def realtime_status(self) -> dict:
        """Detector state + last detection + fast-path wiring (display only)."""
        try:
            saved = self.store.get_state("realtime.tracker")
        except (AttributeError, OSError, TypeError, ValueError):
            saved = None
        tracker_payload = None
        if saved:
            try:
                import json as _json

                from noema.application.realtime import DetectionTracker as _Tracker

                tracker_payload = _Tracker.from_dict(
                    _json.loads(saved), config=self.detection_tracker.config).to_dict()
            except (AttributeError, TypeError, ValueError):
                tracker_payload = None
        latest = self.store.query_detections(limit=1)
        verifier = getattr(self, "fast_verifier", None)
        return {
            "tracker": tracker_payload or self.detection_tracker.to_dict(),
            "last_detection": latest[0].to_dict() if latest else None,
            "detector": {
                "enter_threshold": float(self.realtime_detector.config.enter_threshold),
                "exit_threshold": float(self.realtime_detector.config.exit_threshold),
                "min_active_seconds": float(self.realtime_detector.config.min_active_seconds),
            },
            "fast_verifier_configured": verifier is not None,
        }

    def evaluate_realtime(self, now: Optional[Any] = None,
                          execute_intervention: bool = True) -> dict:
        """Run one real-time evaluation: windows → score → hysteresis.

        Independent of the 20-minute semantic scheduler and read-light:
        local feature math on stored sessions, one model call only when a
        FRESH candidate is entered, one intervention attempt only after a
        concerning verification AND existing actionable DISTRACTED behavior
        evidence. Only meaningful decisions are persisted; every tick is
        not. Never raises for provider reasons.
        """
        import json as _json

        from noema.domain.behavior import BehaviorState
        from noema.observability.models import new_request_id
        from noema.application.realtime import (
            BehaviorWindowConfig,
            DetectionRecord,
            DetectionTracker,
            build_behavior_windows,
            detection_id,
        )

        import time as _time

        current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
        evaluated_at = current.isoformat().replace("+00:00", "Z")
        request_id = new_request_id()
        eval_started = _time.perf_counter()
        mark = {"start": eval_started}
        tracker = self.detection_tracker
        try:
            saved = self.store.get_state("realtime.tracker")
            if saved:
                tracker = DetectionTracker.from_dict(
                    _json.loads(saved), config=tracker.config)
                self.detection_tracker = tracker
        except (AttributeError, OSError, TypeError, ValueError):
            pass

        def _persist_tracker() -> None:
            try:
                self.store.set_state("realtime.tracker", _json.dumps(tracker.to_dict()))
            except (AttributeError, OSError, TypeError, ValueError):
                pass

        def _record(window: Any, decision: Any, tracker_state: str,
                    decision_name: str, reason: str,
                    verification: Any = None) -> dict:
            if verification is not None and not verification.skipped:
                model_verdict = "concerning" if verification.concerning else "not_concerning"
            else:
                model_verdict = None
            record = DetectionRecord(
                detection_id=detection_id(
                    evaluated_at, window.current_session_id,
                    decision.score, decision_name),
                timestamp=current,
                session_id=window.current_session_id,
                window_start=coerce_timestamp(window.window_start),
                window_end=coerce_timestamp(window.window_end),
                window_minutes=window.window_minutes,
                feature_version=window.feature_version,
                candidate_score=round(decision.score, 3),
                signals_json=_json.dumps(
                    {key: round(value, 3) for key, value in decision.signals.items()},
                    sort_keys=True),
                tracker_state=tracker_state,
                model_verdict=model_verdict,
                model_confidence=(
                    verification.confidence if verification is not None else None),
                severity=(verification.severity if verification is not None else None),
                recommended_intervention=(
                    verification.recommended_intervention if verification is not None else None),
                provider=(verification.provider if verification is not None else None),
                model=(verification.model if verification is not None else None),
                latency_ms=(verification.latency_ms if verification is not None else None),
                decision=decision_name,
                reason=reason,
            )
            try:
                self.store.insert_detection(record)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
            return record.to_dict()

        try:
            snapshot = self.presence_snapshot(now=current)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            return {
                "evaluated_at": evaluated_at,
                "state": "unknown",
                "error": "presence snapshot unavailable: {}".format(exc),
                "tracker": tracker.to_dict(),
            }
        mark["presence"] = _time.perf_counter()
        presence_state = str(snapshot.get("state") or "unknown")
        if presence_state.strip().lower() == "afk":
            # AFK veto at the front door: no scoring, no model, no action.
            state = tracker.update(0.0, current)
            _persist_tracker()
            self._record_realtime_eval(
                request_id, mark, eval_started, state, presence_state,
                None, None, None, [], tracker)
            return {
                "evaluated_at": evaluated_at,
                "state": state,
                "presence": "afk",
                "action": "none (AFK veto)",
                "tracker": tracker.to_dict(),
            }

        lookback_start = current - timedelta(minutes=65)
        try:
            sessions = self.store.query_meaningful_sessions(
                start=lookback_start, end=current, limit=100000)
        except (AttributeError, OSError, TypeError, ValueError):
            sessions = []
        session_ids = {getattr(item, "id", None) for item in sessions}
        try:
            class_map = {
                item.session_id: item
                for item in self.store.query_classifications(limit=100000)
                if item.session_id in session_ids
            }
        except (AttributeError, OSError, TypeError, ValueError):
            class_map = {}
        current_goal = None
        goal_alignment = None
        try:
            intents = self.store.query_intents(limit=1)
        except (AttributeError, OSError, TypeError, ValueError):
            intents = []
        intent = intents[0] if intents else None
        if intent is not None:
            current_goal = getattr(intent, "goal", None) or getattr(intent, "text", None)
        windows = build_behavior_windows(
            sessions, class_map, now=current,
            config=BehaviorWindowConfig(),
            current_goal=current_goal,
            goal_alignment=goal_alignment,
        )
        window = max(windows, key=lambda item: item.window_minutes)
        if intent is not None and window.current_session_id:
            try:
                alignments = self.store.query_alignments(
                    intent_id=intent.id, limit=100000)
                for alignment in alignments:
                    if getattr(alignment, "session_id", None) == window.current_session_id:
                        goal_alignment = float(getattr(alignment, "score", 0.0) or 0.0)
                        break
                if goal_alignment is not None:
                    windows = build_behavior_windows(
                        sessions, class_map, now=current,
                        config=BehaviorWindowConfig(),
                        current_goal=current_goal,
                        goal_alignment=goal_alignment,
                    )
                    window = max(windows, key=lambda item: item.window_minutes)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        mark["features"] = _time.perf_counter()
        decision = self.realtime_detector.score(window)
        mark["score"] = _time.perf_counter()
        previous_state = tracker.state
        state = tracker.update(decision.score, current)
        verification = None
        intervention_payload = None
        detections = []
        fresh_candidate = (
            previous_state in {"NORMAL", "RECOVERING"}
            and state == "CANDIDATE"
            and decision.is_candidate
        )
        if fresh_candidate:
            # The ONLY model call in this time scale, and only on entry.
            verifier = getattr(self, "fast_verifier", None)
            if verifier is not None:
                try:
                    verification = verifier.verify(
                        window.compact_summary(), decision.score,
                        decision.reasons, presence_state=presence_state,
                        telemetry={"request_id": request_id})
                except (AttributeError, TypeError, ValueError) as exc:
                    verification = None
                    detections.append(_record(
                        window, decision, state, "verification_error",
                        "verifier raised: {}: {}".format(
                            type(exc).__name__, str(exc)[:160])))
            if verification is None:
                detections.append(_record(
                    window, decision, state, "candidate_detected",
                    "candidate entered (score {:.2f}); no verifier configured".format(
                        decision.score)))
            elif verification.concerning:
                state = tracker.notify_confirmed(decision.score, current)
                detections.append(_record(
                    window, decision, state, "verified_concerning",
                    "fast model confirmed concern (severity {}, {}): {}".format(
                        verification.severity,
                        verification.recommended_intervention,
                        verification.reason),
                    verification=verification))
            else:
                detections.append(_record(
                    window, decision, state, "verified_not_concerning",
                    "fast model did not confirm concern: {}".format(
                        verification.reason),
                    verification=verification))
        mark["verify"] = _time.perf_counter()
        if state == "CONFIRMED":
            # Act only on existing behavior evidence: an actionable
            # DISTRACTED observation for the current session. The state
            # machine owns transitions; this path never invents them.
            actionable = []
            if window.current_session_id:
                try:
                    actionable = [
                        item for item in self.store.query_behavior_observations(limit=100000)
                        if getattr(item, "session_id", None) == window.current_session_id
                        and getattr(item, "state", None) == BehaviorState.DISTRACTED
                        and bool(getattr(item, "actionable", False))
                    ]
                except (AttributeError, OSError, TypeError, ValueError):
                    actionable = []
            if actionable and window.current_session_id:
                try:
                    intervention = self.consider_intervention(
                        window.current_session_id,
                        intent_id=getattr(intent, "id", None),
                        execute=execute_intervention,
                        now=current,
                    )
                except (AttributeError, TypeError, ValueError, OSError):
                    intervention = None
                if intervention is not None:
                    intervention_payload = {
                        "id": intervention.id,
                        "mode": intervention.mode.value if intervention.mode else None,
                        "status": intervention.status.value,
                        "reason": intervention.reason,
                    }
                    if str(intervention.status.value) == "EXECUTED":
                        state = tracker.notify_intervention(current)
                        detections.append(_record(
                            window, decision, state, "intervention_triggered",
                            "{} intervention {} ({})".format(
                                intervention.mode.value if intervention.mode else "?",
                                intervention.id[:8],
                                intervention.reason),
                            verification=verification))
                    else:
                        detections.append(_record(
                            window, decision, state, "intervention_skipped",
                            "intervention not executed: {}".format(intervention.reason),
                            verification=verification))
        mark["policy"] = _time.perf_counter()
        _persist_tracker()
        self._record_realtime_eval(
            request_id, mark, eval_started, state, presence_state,
            decision, verification, intervention_payload, detections, tracker)
        return {
            "evaluated_at": evaluated_at,
            "state": state,
            "previous_state": previous_state,
            "presence": presence_state,
            "window": window.to_dict(),
            "decision": decision.to_dict(),
            "verification": verification.to_dict() if verification is not None else None,
            "intervention": intervention_payload,
            "detections": detections,
            "tracker": tracker.to_dict(),
        }

    def _record_realtime_eval(self, request_id: str, mark: dict,
                                eval_started: float, state: str,
                                presence_state: str, decision: Any,
                                verification: Any, intervention_payload: Any,
                                detections: list, tracker: Any) -> None:
        """Persist one realtime-evaluation timing record (never raises)."""
        import time as _time

        def _segment(first: str, second: str) -> Optional[float]:
            if mark.get(first) is None or mark.get(second) is None:
                return None
            return round((mark[second] - mark[first]) * 1000, 1)

        total_ms = round((_time.perf_counter() - eval_started) * 1000, 1)
        metadata = {
            "request_id": request_id,
            "presence": presence_state,
            "tracker_state": state,
            "candidate_score": round(decision.score, 3) if decision is not None else None,
            "is_candidate": bool(getattr(decision, "is_candidate", False)),
            "presence_ms": _segment("start", "presence"),
            "feature_ms": _segment("presence", "features"),
            "detection_ms": _segment("features", "score"),
            "verification_ms": _segment("score", "verify"),
            "policy_ms": _segment("verify", "policy"),
            "verification_concerning": (
                bool(verification.concerning) if verification is not None else None),
            "verification_latency_ms": (
                verification.latency_ms if verification is not None else None),
            "intervention": intervention_payload,
            "detection_ids": [item.get("detection_id") for item in detections
                              if isinstance(item, dict) and item.get("detection_id")],
        }
        recorder = getattr(self, "telemetry", None)
        if recorder is None:
            return
        try:
            recorder.record_operation("realtime_eval", "evaluate", duration_ms=total_ms,
                                      metadata=metadata)
        except Exception:
            pass

    def recent_classifications(self, limit: int = 12) -> list:
        """Latest meaningful-session verdicts, newest first (PART 34).

        Raw one-second rows are telemetry, not verdicts: this feed returns
        one row per meaningful session with its stored classification.
        """

        if limit < 1:
            raise ValueError("limit must be positive")
        meaningful = self.store.query_meaningful_sessions(limit=100000)
        if not meaningful:
            return []
        ordered = sorted(meaningful, key=lambda item: (item.end_time, item.id), reverse=True)
        window = ordered[: max(1, int(limit)) * 4]
        session_ids = {item.id for item in window}
        raw_by_id = {
            item.id: item
            for item in self.store.query_sessions(
                start=min(item.start_time for item in window),
                end=max(item.end_time for item in window),
                limit=100000,
            )
        }
        classifications = {
            item.session_id: item
            for item in self.store.query_classifications(limit=100000)
            if item.session_id in session_ids
            and item.classification_status == "classified"
            and item.source != "heuristic"
        }
        rows = []
        for item in window:
            applications: list = []
            for raw_id in item.activity_session_ids:
                raw = raw_by_id.get(raw_id)
                if raw is None:
                    continue
                label = raw.app or raw.browser or "unknown"
                if label not in applications:
                    applications.append(label)
            classification = classifications.get(item.id)
            rows.append({
                "session_id": item.id,
                "start": item.start_time.isoformat().replace("+00:00", "Z"),
                "end": item.end_time.isoformat().replace("+00:00", "Z"),
                "duration_seconds": round(item.duration, 1),
                "active_duration_seconds": round(item.active_duration_seconds, 1),
                "applications": applications,
                "topic": item.primary_topic or item.primary_task,
                "category": classification.category if classification else None,
                "confidence": classification.confidence if classification else 0.0,
                "source": classification.source if classification else None,
                "classification_status": classification.classification_status if classification else "pending",
                "evidence_quality": (classification.evidence_quality if classification and classification.evidence_quality else item.evidence_quality.value),
                "evidence_quality_score": classification.evidence_quality_score if classification else None,
                "provider": classification.provider if classification else None,
                "model": classification.model if classification else None,
            })
            if len(rows) >= max(1, int(limit)):
                break
        return rows

    def classification_coverage(
        self,
        now: Optional[Any] = None,
        classifier_version: str = CLASSIFIER_VERSION_DEFAULT,
        max_retries: int = CLASSIFICATION_RETRIES_MAX,
    ) -> dict:
        """Count coverage over meaningful sessions from stored state only."""

        current = coerce_timestamp(now) if now is not None else datetime.now(timezone.utc)
        sessions = self.store.query_meaningful_sessions(limit=100000)
        stored = self.store.query_classification_map([item.id for item in sessions])
        classified = pending = failed = 0
        for item in sessions:
            row = stored.get(item.id)
            if (
                row is not None
                and row.classification_status == "classified"
                and row.source != "heuristic"
            ):
                classified += 1
            elif row is not None and not self._is_eligible(
                row, current, classifier_version, max_retries,
            ):
                failed += 1
            else:
                pending += 1
        total = len(sessions)
        latest = self.store.query_classifications(limit=1)
        latest_row = latest[0] if latest else None
        return {
            "total_eligible": total,
            "classified": classified,
            "pending": pending,
            "failed": failed,
            "coverage_percent": round(classified / total * 100, 1) if total else None,
            "latest_classification_at": (
                latest_row.classified_at.isoformat().replace("+00:00", "Z")
                if latest_row and latest_row.classified_at else None
            ),
            "latest_provider": latest_row.provider if latest_row else None,
            "latest_model": latest_row.model if latest_row else None,
        }

    def classify_sessions(
        self,
        sessions: Iterable[ActivitySession],
        persist: bool = True,
        classifier_version: str = CLASSIFIER_VERSION_DEFAULT,
        max_retries: int = CLASSIFICATION_RETRIES_MAX,
    ) -> list:
        """Classify sessions with local Ollama and persist their results."""

        ordered = list(sessions)
        now = datetime.now(timezone.utc)
        stored = self.store.query_classification_map([item.id for item in ordered])
        classifications = [
            self._apply_retry_accounting(result, stored.get(result.session_id), now, classifier_version, max_retries)
            for result in self.classifier.classify_many(ordered)
        ]
        if persist:
            self.store.insert_classifications(classifications)
        return classifications

    def classify_stored_sessions(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        device: Optional[str] = None,
        domain: Optional[str] = None,
        limit: int = 1000,
        persist: bool = True,
    ) -> list:
        sessions = self.store.query_sessions(start, end, device, domain, limit)
        return self.classify_sessions(sessions, persist=persist)

    def classify_meaningful_sessions(
        self,
        sessions: Iterable[MeaningfulSession],
        persist: bool = True,
        context_by_session: Optional[dict[str, Iterable[dict]]] = None,
        classifier_version: str = CLASSIFIER_VERSION_DEFAULT,
        max_retries: int = CLASSIFICATION_RETRIES_MAX,
    ) -> list:
        """Classify canonical Gen 1.5 sessions, retaining raw sessions as evidence."""

        from noema.observability.models import new_request_id

        ordered = list(sessions)
        now = datetime.now(timezone.utc)
        request_id = new_request_id()
        stored = self.store.query_classification_map([item.id for item in ordered])
        classifications = [
            self._apply_retry_accounting(result, stored.get(result.session_id), now, classifier_version, max_retries)
            for result in self.classifier.classify_many(
                ordered,
                context_by_session=context_by_session,
                telemetry={"request_id": request_id},
            )
        ]
        if persist:
            with self._observe_db("classification_write",
                                  metadata={"request_id": request_id}) as state:
                self.store.insert_classifications(classifications)
                state["rows_affected"] = len(classifications)
        return classifications

    def classify_stored_meaningful_sessions(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        project: Optional[str] = None,
        intent_id: Optional[str] = None,
        limit: int = 1000,
        persist: bool = True,
    ) -> list:
        sessions = self.store.query_meaningful_sessions(start, end, project, intent_id, limit)
        return self.classify_meaningful_sessions(sessions, persist=persist)

    def query_classifications(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_classifications(*args, **kwargs)

    def record_feedback(
        self,
        session_id: str,
        verdict: str,
        note: Optional[str] = None,
        source: str = "user",
    ) -> dict:
        """Store a human verdict, snapshotting the model's provenance."""
        classifications = self.store.query_classifications(session_id=session_id, limit=1)
        current = classifications[0] if classifications else None
        return self.store.insert_feedback(
            session_id,
            verdict,
            note=note,
            source=source,
            model=getattr(current, "model", None),
            classifier_version=getattr(current, "classifier_version", None),
            prompt_version=getattr(current, "prompt_version", None),
        )

    def query_feedback(self, session_id: str) -> Optional[dict]:
        return self.store.get_feedback(session_id)

    def feedback_quality(self, model: Optional[str] = None) -> dict:
        return self.store.feedback_quality(model)

    def capture_intent(self, text: str, persist: bool = True) -> Intent:
        from noema.observability.models import new_request_id
        from noema.observability.provider_telemetry import take_usage

        client = getattr(self.intent_engine, "client", None)
        with self._observe_model(
                "ollama", getattr(client, "model", None),
                "OTHER", "OTHER", "CAPTURE_INTENT", new_request_id()) as handle:
            handle.estimated_input_tokens = max(1, len(text or "") // 4)
            intent = self.intent_engine.capture(text)
            handle.usage = take_usage(client)
            handle.success_payload = bool(getattr(intent, "provider", "") != "heuristic")
        if persist:
            self.store.insert_intent(intent)
        return intent

    def query_intents(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_intents(*args, **kwargs)

    def align_session(
        self,
        session: ActivitySession,
        intent: Intent,
        classification: Optional[Classification] = None,
        persist: bool = True,
    ) -> AlignmentResult:
        alignment = self.goal_aligner.align(session, intent, classification)
        if persist:
            self.store.insert_alignment(alignment)
        return alignment

    def align_stored_session(
        self,
        session_id: str,
        intent_id: str,
        persist: bool = True,
    ) -> AlignmentResult:
        sessions = [item for item in self.store.query_meaningful_sessions(limit=100000) if item.id == session_id]
        if not sessions:
            sessions = [item for item in self.store.query_sessions(limit=100000) if item.id == session_id]
        intents = [item for item in self.store.query_intents(limit=100000) if item.id == intent_id]
        if not sessions:
            raise ValueError("session not found: {}".format(session_id))
        if not intents:
            raise ValueError("intent not found: {}".format(intent_id))
        classifications = self.store.query_classifications(session_id=session_id, limit=1)
        return self.align_session(
            sessions[0], intents[0], classifications[0] if classifications else None, persist
        )

    def align_meaningful_session(
        self,
        session: MeaningfulSession,
        intent: Intent,
        classification: Optional[Classification] = None,
        persist: bool = True,
    ) -> AlignmentResult:
        """Align one canonical session to an explicit intent."""

        return self.align_session(session, intent, classification, persist=persist)

    def align_stored_meaningful_session(
        self,
        session_id: str,
        intent_id: str,
        persist: bool = True,
    ) -> AlignmentResult:
        sessions = [item for item in self.store.query_meaningful_sessions(limit=100000) if item.id == session_id]
        intents = [item for item in self.store.query_intents(limit=100000) if item.id == intent_id]
        if not sessions:
            raise ValueError("meaningful session not found: {}".format(session_id))
        if not intents:
            raise ValueError("intent not found: {}".format(intent_id))
        classifications = self.store.query_classifications(session_id=session_id, limit=1)
        return self.align_meaningful_session(
            sessions[0], intents[0], classifications[0] if classifications else None, persist
        )

    def query_alignments(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_alignments(*args, **kwargs)

    def evaluate_behavior(
        self,
        sessions: Iterable[ActivitySession],
        classifications: Optional[dict] = None,
        alignments: Optional[dict] = None,
        persist: bool = True,
    ) -> list:
        observations = self.behavior_engine.evaluate(sessions, classifications, alignments)
        if persist:
            self.store.insert_behavior_observations(observations)
        return observations

    def evaluate_stored_behavior(
        self,
        intent_id: Optional[str] = None,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        device: Optional[str] = None,
        limit: int = 1000,
        persist: bool = True,
    ) -> list:
        meaningful = self.store.query_meaningful_sessions(start, end, limit=limit)
        if device:
            meaningful = [item for item in meaningful if device in item.device_set]
        if meaningful:
            session_ids = {session.id for session in meaningful}
            classifications = {
                item.session_id: item
                for item in self.store.query_classifications(limit=limit)
                if item.session_id in session_ids
            }
            alignments = {
                item.session_id: item
                for item in self.store.query_alignments(intent_id=intent_id, limit=limit)
                if item.session_id in session_ids
            }
            return self.evaluate_behavior(meaningful, classifications, alignments, persist=persist)

        sessions = self.store.query_sessions(start, end, device, limit=limit)
        session_ids = {session.id for session in sessions}
        classifications = {
            item.session_id: item
            for item in self.store.query_classifications(limit=limit)
            if item.session_id in session_ids
        }
        alignments = {
            item.session_id: item
            for item in self.store.query_alignments(intent_id=intent_id, limit=limit)
            if item.session_id in session_ids
        }
        return self.evaluate_behavior(sessions, classifications, alignments, persist=persist)

    def query_behavior(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_behavior_observations(*args, **kwargs)

    def consider_intervention(
        self,
        session_id: str,
        intent_id: Optional[str] = None,
        execute: bool = False,
        handler: Optional[Any] = None,
        now: Optional[Any] = None,
    ) -> Intervention:
        observations = [item for item in self.store.query_behavior_observations(limit=100000) if item.session_id == session_id]
        sessions = [item for item in self.store.query_meaningful_sessions(limit=100000) if item.id == session_id]
        if not sessions:
            sessions = [item for item in self.store.query_sessions(limit=100000) if item.id == session_id]
        if not observations:
            raise ValueError("behavior observation not found: {}".format(session_id))
        if not sessions:
            raise ValueError("session not found: {}".format(session_id))
        intents = [item for item in self.store.query_intents(limit=100000) if item.id == intent_id] if intent_id else []
        classifications = self.store.query_classifications(session_id=session_id, limit=1)
        try:
            presence_state = str((self.presence_snapshot() or {}).get("state") or "active")
        except (AttributeError, OSError, TypeError, ValueError):
            presence_state = "active"
        intervention = self.intervention_engine.consider(
            observations[0],
            sessions[0],
            intents[0] if intents else None,
            classifications[0] if classifications else None,
            self.store.query_interventions(limit=100),
            now=now,
            presence_state=presence_state,
            recent_outcomes=self.store.query_outcomes(limit=100),
        )
        if intervention.status == InterventionStatus.PLANNED and intervention.mode == InterventionMode.MEME:
            # Detection never blocks on meme generation: the heuristic
            # payload above is already a valid text fallback. Replace it
            # with a generated meme only on success.
            try:
                meme = self.generate_meme(session_id, intent_id=intent_id)
                payload = dict(intervention.payload)
                payload["top"] = meme.top
                payload["bottom"] = meme.bottom
                payload["severity"] = meme.severity
                payload["meme"] = {
                    "template": meme.template,
                    "top": meme.top,
                    "bottom": meme.bottom,
                }
                payload["meme_id"] = meme.id
                intervention = replace(intervention, payload=payload)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        if intervention.status == InterventionStatus.PLANNED and not intervention.action.target.get("tab_id"):
            live_target = self.browser_bridge.current_target(
                device_id=getattr(sessions[0], "device", None),
                browser=browser_name(sessions[0]),
            )
            if not live_target:
                # ActivityWatch hostnames and user-facing extension device IDs
                # may use different naming conventions. Browser matching still
                # gives us the exact currently active tab without guessing at
                # another tab.
                live_target = self.browser_bridge.current_target(
                    browser=browser_name(sessions[0]),
                )
            if live_target:
                intervention = intervention.with_target(live_target)
        if execute and intervention.status == InterventionStatus.PLANNED:
            intervention = self.intervention_engine.execute(intervention, handler=handler)
        with self._observe_db("intervention_write") as state:
            self.store.insert_intervention(intervention)
            state["rows_affected"] = 1
        if intervention.status in {InterventionStatus.PLANNED, InterventionStatus.EXECUTED}:
            existing_actions = self.store.query_intervention_actions(intervention.id, limit=100)
            if not any(item["action"] == "triggered" for item in existing_actions):
                self.store.record_intervention_action(
                    intervention.id,
                    "TRIGGERED",
                    state=intervention.status.value,
                    target=intervention.action.target,
                    metadata={"reason": intervention.reason},
                )
        if execute and intervention.status == InterventionStatus.EXECUTED:
            delivery = self.browser_bridge.publish(intervention)
            self.store.record_intervention_action(
                intervention.id,
                "sent" if delivery.status in {"DELIVERED", "QUEUED"} else "suppressed",
                state="RECEIVED" if delivery.status == "DELIVERED" else delivery.status,
                target=intervention.action.target,
                metadata=delivery.to_dict(),
            )
            if delivery.status == "NO_EXACT_TARGET" and intervention.mode != InterventionMode.HOLDOUT:
                fallback = self.notification_handler.send(intervention)
                self.store.record_intervention_action(
                    intervention.id,
                    "desktop_notification",
                    state="RECEIVED" if fallback.get("delivered") else "FALLBACK_UNAVAILABLE",
                    target=intervention.action.target,
                    metadata=fallback,
                )
        return intervention

    def query_interventions(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_interventions(*args, **kwargs)

    def register_browser(
        self,
        browser: str,
        device_id: str,
        extension_instance_id: str,
    ) -> dict:
        registration = self.browser_bridge.register(browser, device_id, extension_instance_id)
        payload = registration.to_dict()
        payload["websocket_url"] = "ws://127.0.0.1:8766/api/browser/ws"
        payload["poll_url"] = "/api/browser/poll"
        return payload

    def report_browser_event(
        self,
        extension_instance_id: str,
        event: Any,
    ) -> dict:
        browser_event = event if isinstance(event, BrowserEvent) else BrowserEvent.from_mapping(event)
        registration = self.browser_bridge.report_event(extension_instance_id, browser_event)
        # Keep a lightweight browser heartbeat in the same local evidence
        # store as ActivityWatch. This makes the exact current tab available
        # for classification instead of only for intervention delivery.
        if not browser_event.private and browser_event.event_type != "tab_closed":
            hostname = None
            try:
                hostname = urlsplit(browser_event.url or "").hostname
            except ValueError:
                pass
            if hostname and hostname.casefold().startswith("www."):
                hostname = hostname[4:]
            self.ingest_events([
                ActivityEvent(
                    timestamp=browser_event.timestamp,
                    # A tab-activation observation needs a non-zero interval
                    # so it can form a session immediately. It is excluded
                    # from ActivityWatch screen-time aggregation because its
                    # metadata source is firefox_extension, not currentwindow.
                    duration=1.0,
                    device=registration.device_id,
                    app="Firefox",
                    title=browser_event.title,
                    domain=hostname,
                    url=browser_event.url,
                    source="firefox_extension",
                    metadata={"event_type": browser_event.event_type, "private": False},
                    browser=registration.browser,
                    browser_window_id=browser_event.window_id,
                    browser_tab_id=browser_event.tab_id,
                )
            ])
            self.sessionize_stored_events(
                start=browser_event.timestamp,
                end=browser_event.timestamp + timedelta(seconds=1),
                device=registration.device_id,
                limit=100,
            )
        return registration.to_dict()

    def categorize_browser_tab(
        self,
        device: str,
        browser: str,
        window_id: str,
        tab_id: str,
        classify: bool = False,
    ) -> dict:
        """Return or explicitly create the category for one exact browser tab."""

        now = datetime.now(timezone.utc)
        sessions = self.store.query_sessions(
            # A browser tab can be registered before the first daemon poll;
            # retain a small history window so exact-tab categorization does
            # not disappear merely because ingestion was delayed.
            start=now - timedelta(days=7),
            end=now + timedelta(seconds=1),
            device=device,
            limit=5000,
        )
        candidates = [
            item for item in sessions
            if (item.browser or "").casefold() == str(browser).casefold()
            and str(item.browser_window_id or "") == str(window_id)
            and str(item.browser_tab_id or "") == str(tab_id)
        ]
        if not candidates:
            return {
                "status": "pending",
                "category": "neutral",
                "productivity": "neutral",
                "confidence": 0.0,
                "activity": "No activity recorded for this tab yet",
                "signal": "The exact tab has not produced normalized evidence yet.",
                "reason": "no stored activity for this tab yet",
                "device": device, "browser": browser,
                "window_id": str(window_id), "tab_id": str(tab_id),
            }
        session = sorted(candidates, key=lambda item: item.end, reverse=True)[0]
        found = self.store.query_classifications(session_id=session.id, limit=1)
        if not found and classify:
            found = self.classify_sessions([session])
        if not found:
            return {
                "status": "pending",
                "category": "neutral",
                "productivity": "neutral",
                "confidence": 0.0,
                "activity": "Waiting for classification",
                "signal": "The tab evidence is present, but semantic classification has not been requested.",
                "reason": "waiting for Ollama classification",
                "session_id": session.id, "title": session.title,
                "domain": session.domain, "device": device,
                "browser": browser, "window_id": str(window_id), "tab_id": str(tab_id),
            }
        result = found[0].to_dict()
        result.update({
            "status": "categorized", "session_id": session.id,
            "title": session.title, "domain": session.domain,
            "device": device, "browser": browser,
            "window_id": str(window_id), "tab_id": str(tab_id),
        })
        return result

    def poll_browser(
        self,
        extension_instance_id: str,
        timeout_seconds: float = 0.0,
        limit: int = 10,
    ) -> list:
        return self.browser_bridge.poll(extension_instance_id, timeout_seconds, limit)

    def browser_registrations(self) -> list:
        return self.browser_bridge.snapshot()

    def record_intervention_action(
        self,
        intervention_id: str,
        action: str,
        state: Optional[str] = None,
        target: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        row_id = self.store.record_intervention_action(
            intervention_id, action, state=state, target=target, metadata=metadata
        )
        normalized_action = {
            "TRIGGERED": "triggered",
            "LOCK_IN": "lock_in",
            "DISMISS_WORKING": "dismissed_as_working",
            "DISMISS": "dismissed",
            "DISPLAYED": "displayed",
            "SEEN": "seen",
            "CLICKED": "clicked",
            "DISMISSED": "dismissed",
            "RECEIVED": "received",
            "AUTO_DISMISSED": "auto_dismissed",
            "IGNORED": "ignored",
            "RECOVERED": "recovered",
        }.get(str(action).strip().upper(), str(action).strip().casefold())
        return {
            "id": row_id,
            "intervention_id": str(intervention_id),
            "action": normalized_action,
            "state": state.upper() if state else None,
        }

    def query_intervention_actions(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_intervention_actions(*args, **kwargs)

    def build_meaningful_sessions(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        intent_id: Optional[str] = None,
        limit: int = 1000,
        sequence_terminated: bool = True,
        summarize: bool = False,
    ) -> list:
        raw = self.store.query_sessions(start=start, end=end, limit=limit)
        classifications = {item.session_id: item for item in self.store.query_classifications(limit=limit)}
        alignments = {
            item.session_id: item
            for item in self.store.query_alignments(intent_id=intent_id, limit=limit)
        }
        intents = [item for item in self.store.query_intents(limit=100000) if item.id == intent_id] if intent_id else []
        intent = intents[0] if intents else None
        presence = self.build_presence_timeline(start, end)
        with self._observe_db("meaningful_generation") as state:
            meaningful = self.meaningful_session_engine.build(
                raw, classifications, alignments, intent, sequence_terminated, presence
            )
            state["rows_affected"] = len(meaningful)
        if summarize:
            raw_by_id = {item.id: item for item in raw}
            meaningful = [
                self.meaningful_session_summarizer.summarize(
                    item,
                    [raw_by_id[raw_id] for raw_id in item.activity_session_ids if raw_id in raw_by_id],
                    intent,
                    classifications,
                )
                for item in meaningful
            ]
        self.store.insert_meaningful_sessions(meaningful)
        return meaningful

    def query_meaningful_sessions(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_meaningful_sessions(*args, **kwargs)

    def generate_meme(self, session_id: str, intent_id: Optional[str] = None) -> MemePayload:
        from noema.observability.models import new_request_id
        from noema.observability.provider_telemetry import take_usage

        observations = [item for item in self.store.query_behavior_observations(limit=100000) if item.session_id == session_id]
        sessions = [item for item in self.store.query_meaningful_sessions(limit=100000) if item.id == session_id]
        if not sessions:
            sessions = [item for item in self.store.query_sessions(limit=100000) if item.id == session_id]
        if not observations or not sessions:
            raise ValueError("behavior session not found: {}".format(session_id))
        intents = [item for item in self.store.query_intents(limit=100000) if item.id == intent_id] if intent_id else []
        classifications = self.store.query_classifications(session_id=session_id, limit=1)
        client = getattr(self.meme_intelligence, "client", None)
        with self._observe_model(
                "ollama", getattr(client, "model", None),
                "MEME_GENERATION", "MEME", "GENERATE_MEME", new_request_id()) as handle:
            handle.session_ids = [session_id]
            handle.estimated_input_tokens = max(1, len(MemeIntelligence._prompt(
                sessions[0], observations[0], intents[0] if intents else None,
                classifications[0] if classifications else None)) // 4)
            meme = self.meme_intelligence.create(
                sessions[0], observations[0], intents[0] if intents else None,
                classifications[0] if classifications else None,
            )
            handle.usage = take_usage(client)
            # A heuristic fallback means the model produced nothing usable.
            handle.success_payload = bool(getattr(meme, "provider", "") != "heuristic")
        self.store.insert_meme(meme)
        return meme

    def query_memes(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_memes(*args, **kwargs)

    def measure_outcome(
        self,
        intervention_id: str,
        now: Optional[Any] = None,
        meme_id: Optional[str] = None,
    ) -> InterventionOutcome:
        interventions = [item for item in self.store.query_interventions(limit=100000) if item.id == intervention_id]
        if not interventions:
            raise ValueError("intervention not found: {}".format(intervention_id))
        intervention = interventions[0]
        sessions = self.store.query_meaningful_sessions(start=intervention.created_at, limit=100000)
        if not sessions:
            sessions = self.store.query_sessions(start=intervention.created_at, limit=100000)
        classifications = {item.session_id: item for item in self.store.query_classifications(limit=100000)}
        outcome = self.outcome_tracker.measure(intervention, sessions, classifications, now, meme_id)
        with self._observe_db("outcome_write") as state:
            self.store.insert_outcome(outcome)
            state["rows_affected"] = 1
        if outcome.recovery_status.value == "RECOVERED":
            actions = self.store.query_intervention_actions(intervention.id, limit=100)
            if not any(item["action"] == "recovered" for item in actions):
                self.store.record_intervention_action(
                    intervention.id,
                    "RECOVERED",
                    state="RECOVERED",
                    metadata=outcome.to_dict(),
                )
        return outcome

    def query_outcomes(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_outcomes(*args, **kwargs)

    def derive_memories(self, limit: int = 100000) -> list:
        sessions = self.store.query_meaningful_sessions(limit=limit)
        if not sessions:
            sessions = self.store.query_sessions(limit=limit)
        classifications = {item.session_id: item for item in self.store.query_classifications(limit=limit)}
        observations = {item.session_id: item for item in self.store.query_behavior_observations(limit=limit)}
        memories = self.memory_engine.derive_distraction_patterns(sessions, classifications, observations)
        for memory in memories:
            self.store.insert_memory(memory)
        return memories

    def query_memories(self, *args: Any, **kwargs: Any) -> list:
        return self.store.query_memories(*args, **kwargs)

    def personalization_profile(self, limit: int = 100000) -> PersonalizationProfile:
        return self.personalizer.build_profile(self.store.query_outcomes(limit=limit))

    def export_sync(self, device: Optional[str] = None, limit: int = 100000) -> SyncEnvelope:
        events = [item.event for item in self.store.query(device=device, limit=limit)]
        return SyncEnvelope(device or "mixed", tuple(events))

    def import_sync(self, envelope: SyncEnvelope) -> IngestionResult:
        if not isinstance(envelope, SyncEnvelope):
            raise TypeError("import_sync expects a SyncEnvelope")
        return self.ingest_events(envelope.events)

    def unified_timeline(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        device: Optional[str] = None,
        limit: int = 100000,
    ) -> UnifiedTimeline:
        return UnifiedTimeline(item.event for item in self.store.query(start, end, device, limit=limit))

    def autonomous_recommendation(
        self,
        observation: BehaviorObservation,
        intent_id: Optional[str] = None,
    ) -> AgentRecommendation:
        intents = [item for item in self.store.query_intents(limit=100000) if item.id == intent_id] if intent_id else []
        return self.autonomous_agent.recommend(
            observation,
            intents[0] if intents else None,
            self.personalization_profile(),
            self.store.query_memories(limit=100000),
            self.store.query_outcomes(limit=100000),
        )

    def run_autonomous_cycle(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        intent_id: Optional[str] = None,
    ) -> AutonomousCycle:
        ingested = self.ingest_telemetry(start=start, end=end)
        raw_sessions = self.sessionize_stored_events(start=start, end=end)
        # Raw classification/alignment is bootstrap evidence for the merger.
        # Everything after the Gen 1.5 boundary runs on MeaningfulSession.
        raw_classifications = self.classify_sessions(raw_sessions)
        raw_classification_map = {item.session_id: item for item in raw_classifications}
        intents = [item for item in self.store.query_intents(limit=100000) if item.id == intent_id] if intent_id else self.store.query_intents(limit=1)
        intent = intents[0] if intents else None
        raw_alignments = {}
        if intent:
            raw_alignments = {
                session.id: self.align_session(session, intent, raw_classification_map.get(session.id))
                for session in raw_sessions
            }
        sessions = self.build_meaningful_sessions(
            start=start,
            end=end,
            intent_id=intent.id if intent else None,
            sequence_terminated=True,
        )
        classifications = self.classify_meaningful_sessions(sessions)
        classification_map = {item.session_id: item for item in classifications}
        alignments = {}
        if intent:
            alignments = {
                session.id: self.align_meaningful_session(
                    session, intent, classification_map.get(session.id)
                )
                for session in sessions
            }
        observations = self.evaluate_behavior(sessions, classification_map, alignments)
        latest = observations[-1] if observations else BehaviorObservation(
            session_id="none", state="IDLE", started_at=datetime.now(timezone.utc),
            confidence=0.0, distraction_score=0.0, actionable=False, reason="no sessions",
        )
        recommendation = self.autonomous_agent.recommend(
            latest, intent, self.personalization_profile(), self.store.query_memories(limit=100000),
            self.store.query_outcomes(limit=100000),
        )
        return AutonomousCycle(ingested, tuple(sessions), tuple(classifications), tuple(observations), recommendation)
