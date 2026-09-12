"""SQLite storage for normalized, privacy-filtered ActivityWatch events."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from noema.domain.activity import ActivityEvent, StoredActivityEvent, coerce_timestamp
from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.application.classification import Classification
from noema.domain.intervention import Intervention, InterventionMode, InterventionStatus
from noema.domain.meme import MemePayload
from noema.domain.memory import Memory, MemoryKind, PersonalizationProfile
from noema.domain.outcomes import InterventionOutcome, RecoveryStatus
from noema.domain.meaningful import MeaningfulSession, MeaningfulSessionOutcome, MeaningfulSessionStatus, SessionPhase
from noema.domain.intent import AlignmentResult, Intent
from noema.domain.sessions.models import ActivitySession


SCHEMA = """
CREATE TABLE IF NOT EXISTS normalized_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL,
    duration REAL NOT NULL,
    device TEXT NOT NULL,
    app TEXT,
    title TEXT,
    domain TEXT,
    url TEXT,
    browser TEXT,
    browser_window_id TEXT,
    browser_tab_id TEXT,
    source TEXT NOT NULL,
    bucket_id TEXT,
    source_event_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_normalized_events_timestamp
    ON normalized_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_normalized_events_domain
    ON normalized_events(domain);
CREATE INDEX IF NOT EXISTS idx_normalized_events_device
    ON normalized_events(device);
CREATE TABLE IF NOT EXISTS activity_sessions (
    id TEXT PRIMARY KEY,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    duration REAL NOT NULL,
    device TEXT NOT NULL,
    app TEXT,
    title TEXT,
    domain TEXT,
    url TEXT,
    browser TEXT,
    browser_window_id TEXT,
    browser_tab_id TEXT,
    source TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    event_keys_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_activity_sessions_start
    ON activity_sessions(start);
CREATE INDEX IF NOT EXISTS idx_activity_sessions_device
    ON activity_sessions(device);
CREATE TABLE IF NOT EXISTS semantic_classifications (
    session_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    topic TEXT,
    project TEXT,
    activity_type TEXT NOT NULL,
    productivity TEXT NOT NULL,
    confidence REAL NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    subcategory TEXT NOT NULL DEFAULT 'other',
    activity TEXT NOT NULL DEFAULT 'Ambiguous activity',
    signal TEXT NOT NULL DEFAULT 'Evidence was insufficient to determine the activity.',
    source TEXT NOT NULL DEFAULT 'pending',
    classification_status TEXT NOT NULL DEFAULT 'pending',
    service TEXT,
    intent_signal TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_retry_at TEXT,
    classifier_version TEXT NOT NULL DEFAULT '1',
    prompt_version TEXT NOT NULL DEFAULT '1',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_semantic_classifications_category
    ON semantic_classifications(category);
CREATE INDEX IF NOT EXISTS idx_semantic_classifications_productivity
    ON semantic_classifications(productivity);
CREATE INDEX IF NOT EXISTS idx_semantic_classifications_status
    ON semantic_classifications(classification_status);
-- Human verdicts on stored classifications. Append-by-session: the latest
-- row per session is the current verdict. The original classification row
-- is never modified by feedback; this table only observes it.
CREATE TABLE IF NOT EXISTS classification_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    note TEXT,
    source TEXT NOT NULL DEFAULT 'user',
    model TEXT,
    classifier_version TEXT,
    prompt_version TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, source)
);
CREATE INDEX IF NOT EXISTS idx_classification_feedback_session
    ON classification_feedback(session_id);
CREATE INDEX IF NOT EXISTS idx_classification_feedback_model
    ON classification_feedback(model);
CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    goal TEXT NOT NULL,
    topic TEXT,
    project TEXT,
    keywords_json TEXT NOT NULL,
    success_criteria TEXT,
    confidence REAL NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intents_created_at ON intents(created_at);
CREATE TABLE IF NOT EXISTS alignments (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    aligned INTEGER NOT NULL,
    score REAL NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, intent_id)
);
CREATE INDEX IF NOT EXISTS idx_alignments_intent_id ON alignments(intent_id);
CREATE TABLE IF NOT EXISTS behavior_observations (
    session_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    confidence REAL NOT NULL,
    distraction_score REAL NOT NULL,
    actionable INTEGER NOT NULL,
    reason TEXT NOT NULL,
    previous_state TEXT
);
CREATE INDEX IF NOT EXISTS idx_behavior_state ON behavior_observations(state);
CREATE TABLE IF NOT EXISTS interventions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    mode TEXT,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    executed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_interventions_session_id ON interventions(session_id);
CREATE INDEX IF NOT EXISTS idx_interventions_created_at ON interventions(created_at);
CREATE TABLE IF NOT EXISTS memes (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    template TEXT NOT NULL,
    severity INTEGER NOT NULL,
    top TEXT NOT NULL,
    bottom TEXT NOT NULL,
    tone TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memes_session_id ON memes(session_id);
CREATE TABLE IF NOT EXISTS intervention_outcomes (
    id TEXT PRIMARY KEY,
    intervention_id TEXT NOT NULL UNIQUE,
    intervention_time TEXT NOT NULL,
    recovery_status TEXT NOT NULL,
    recovery_time TEXT,
    recovery_session_id TEXT,
    recovery_duration_seconds REAL,
    intervention_type TEXT,
    meme_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_outcomes_status ON intervention_outcomes(recovery_status);
CREATE TABLE IF NOT EXISTS distraction_detections (
    detection_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    session_id TEXT,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    window_minutes INTEGER NOT NULL,
    feature_version TEXT NOT NULL,
    candidate_score REAL NOT NULL,
    signals_json TEXT NOT NULL DEFAULT '{}',
    tracker_state TEXT NOT NULL,
    model_verdict TEXT,
    model_confidence REAL,
    severity INTEGER,
    recommended_intervention TEXT,
    provider TEXT,
    model TEXT,
    latency_ms REAL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_detections_timestamp ON distraction_detections(timestamp);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    confidence REAL NOT NULL,
    evidence_count INTEGER NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
CREATE TABLE IF NOT EXISTS meaningful_sessions (
    id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    duration REAL NOT NULL,
    device_set_json TEXT NOT NULL,
    activity_session_ids_json TEXT NOT NULL,
    primary_project TEXT,
    primary_task TEXT,
    primary_topic TEXT,
    activities_json TEXT NOT NULL,
    dominant_category TEXT,
    dominant_activity_type TEXT,
    intent_id TEXT,
    alignment_score REAL NOT NULL,
    focus_score REAL NOT NULL,
    distraction_score REAL NOT NULL,
    context_switch_count INTEGER NOT NULL,
    active_duration_seconds REAL NOT NULL DEFAULT 0,
    afk_duration_seconds REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    summary_json TEXT,
    outcome TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    phases_json TEXT NOT NULL,
    browser TEXT,
    browser_window_id TEXT,
    browser_tab_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_meaningful_sessions_start ON meaningful_sessions(start_time);
CREATE INDEX IF NOT EXISTS idx_meaningful_sessions_project ON meaningful_sessions(primary_project);
CREATE TABLE IF NOT EXISTS intervention_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intervention_id TEXT NOT NULL,
    action TEXT NOT NULL,
    state TEXT,
    target_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intervention_actions_intervention
    ON intervention_actions(intervention_id, created_at);
CREATE TABLE IF NOT EXISTS daemon_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classification_batches (
    batch_id TEXT PRIMARY KEY,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    processed_at TEXT,
    failure_reason TEXT,
    UNIQUE(start_time, end_time)
);
CREATE INDEX IF NOT EXISTS idx_classification_batches_status_start
    ON classification_batches(status, start_time);
CREATE TABLE IF NOT EXISTS model_invocations (
    invocation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    purpose TEXT NOT NULL DEFAULT 'OTHER',
    pipeline TEXT NOT NULL DEFAULT 'OTHER',
    operation TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'attempt',
    session_ids_json TEXT NOT NULL DEFAULT '[]',
    batch_size INTEGER NOT NULL DEFAULT 0,
    attempt_number INTEGER NOT NULL DEFAULT 0,
    fallback_depth INTEGER NOT NULL DEFAULT 0,
    fallback_from TEXT,
    fallback_reason TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    error_code TEXT,
    error_message TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    token_basis TEXT NOT NULL DEFAULT 'UNKNOWN',
    latency_ms REAL,
    ttft_ms REAL,
    generation_ms REAL,
    tps REAL,
    tps_basis TEXT,
    context_window INTEGER,
    max_output_tokens INTEGER,
    quota_scope TEXT,
    quota_before_json TEXT NOT NULL DEFAULT '{}',
    quota_after_json TEXT NOT NULL DEFAULT '{}',
    cost REAL,
    cost_basis TEXT NOT NULL DEFAULT 'UNKNOWN',
    batch_budget_tokens INTEGER,
    batch_input_tokens INTEGER,
    batch_utilization REAL,
    detection_id TEXT,
    intervention_id TEXT,
    prompt_version TEXT,
    classifier_version TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invocations_timestamp
    ON model_invocations(timestamp);
CREATE INDEX IF NOT EXISTS idx_invocations_provider_model
    ON model_invocations(provider, model);
CREATE INDEX IF NOT EXISTS idx_invocations_purpose
    ON model_invocations(purpose);
CREATE INDEX IF NOT EXISTS idx_invocations_pipeline
    ON model_invocations(pipeline);
CREATE INDEX IF NOT EXISTS idx_invocations_request
    ON model_invocations(request_id);
CREATE INDEX IF NOT EXISTS idx_invocations_detection
    ON model_invocations(detection_id);
CREATE TABLE IF NOT EXISTS operation_timings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    duration_ms REAL,
    success INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    rows_affected INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operation_timings_kind_name
    ON operation_timings(kind, name);
CREATE INDEX IF NOT EXISTS idx_operation_timings_timestamp
    ON operation_timings(timestamp);
CREATE TABLE IF NOT EXISTS benchmark_runs (
    run_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    environment_json TEXT NOT NULL DEFAULT '{}',
    test_set TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT 'NORMAL_CLASSIFICATION',
    iterations INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL,
    status TEXT NOT NULL DEFAULT 'completed',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS benchmark_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    fixture TEXT NOT NULL DEFAULT '',
    iterations INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    schema_valid_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    mean_latency_ms REAL,
    p50_latency_ms REAL,
    p95_latency_ms REAL,
    mean_out_tokens REAL,
    mean_tps REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_benchmark_results_run
    ON benchmark_results(run_id);
"""


class SQLiteStore:
    """Small thread-safe SQLite repository with idempotent event ingestion."""

    def __init__(self, path: Any = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.executescript(SCHEMA)
            self._ensure_schema_columns()

    def _ensure_schema_columns(self) -> None:
        """Add fields introduced after an existing local database was created."""

        migrations = {
            "normalized_events": {
                "browser": "TEXT",
                "browser_window_id": "TEXT",
                "browser_tab_id": "TEXT",
            },
            "activity_sessions": {
                "url": "TEXT",
                "browser": "TEXT",
                "browser_window_id": "TEXT",
                "browser_tab_id": "TEXT",
            },
            "meaningful_sessions": {
                "browser": "TEXT",
                "browser_window_id": "TEXT",
                "browser_tab_id": "TEXT",
                "active_duration_seconds": "REAL NOT NULL DEFAULT 0",
                "afk_duration_seconds": "REAL NOT NULL DEFAULT 0",
            },
            "semantic_classifications": {
                "subcategory": "TEXT NOT NULL DEFAULT 'other'",
                "activity": "TEXT NOT NULL DEFAULT 'Ambiguous activity'",
                "signal": "TEXT NOT NULL DEFAULT 'Evidence was insufficient to determine the activity.'",
                "source": "TEXT NOT NULL DEFAULT 'pending'",
                "classification_status": "TEXT NOT NULL DEFAULT 'pending'",
                "service": "TEXT",
                "intent_signal": "TEXT",
                "retry_count": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT",
                "next_retry_at": "TEXT",
                "classifier_version": "TEXT NOT NULL DEFAULT '1'",
                "prompt_version": "TEXT NOT NULL DEFAULT '1'",
                "evidence_quality": "TEXT",
                "evidence_quality_score": "REAL",
            },
            "meaningful_sessions": {
                "browser": "TEXT",
                "browser_window_id": "TEXT",
                "browser_tab_id": "TEXT",
                "active_duration_seconds": "REAL NOT NULL DEFAULT 0",
                "afk_duration_seconds": "REAL NOT NULL DEFAULT 0",
                "evidence_quality": "TEXT NOT NULL DEFAULT 'absent'",
            },
            "model_invocations": {
                "prompt_version": "TEXT",
                "classifier_version": "TEXT",
            },
        }
        for table, columns in migrations.items():
            existing = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info({})".format(table)).fetchall()
            }
            for column, column_type in columns.items():
                if column not in existing:
                    self._connection.execute(
                        "ALTER TABLE {} ADD COLUMN {} {}".format(table, column, column_type)
                    )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def recover_processing_batches(self) -> int:
        """Return interrupted batches to the durable retry queue."""
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE classification_batches SET status = 'RETRYABLE', failure_reason = ? WHERE status = 'PROCESSING'",
                ("worker interrupted",),
            )
            return cursor.rowcount

    def upsert_classification_batch(self, batch_id: str, start_time: Any, end_time: Any, payload: Mapping[str, Any]) -> bool:
        start = coerce_timestamp(start_time).isoformat().replace("+00:00", "Z")
        end = coerce_timestamp(end_time).isoformat().replace("+00:00", "Z")
        with self._lock:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO classification_batches
                   (batch_id, start_time, end_time, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(batch_id), start, end, json.dumps(dict(payload), ensure_ascii=False, default=str),
                 datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
            )
            return cursor.rowcount == 1

    def update_classification_batch_payload(self, batch_id: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE classification_batches SET payload_json = ? WHERE batch_id = ?",
                (json.dumps(dict(payload), ensure_ascii=False, default=str), str(batch_id)),
            )

    def claim_classification_batches(self, limit: int = 100000) -> List[Dict[str, Any]]:
        with self._lock:
            self._connection.execute(
                "UPDATE classification_batches SET status = 'RETRYABLE', failure_reason = ? WHERE status = 'PROCESSING'",
                ("worker interrupted",),
            )
            rows = self._connection.execute(
                "SELECT * FROM classification_batches WHERE status IN ('PENDING', 'RETRYABLE') ORDER BY start_time ASC, batch_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            batches = []
            for row in rows:
                self._connection.execute(
                    "UPDATE classification_batches SET status = 'PROCESSING', attempt_count = attempt_count + 1 WHERE batch_id = ?",
                    (row["batch_id"],),
                )
                item = dict(row)
                item["status"] = "PROCESSING"
                item["attempt_count"] = int(item["attempt_count"]) + 1
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
                batches.append(item)
            return batches

    def complete_classification_batch(self, batch_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE classification_batches SET status = 'COMPLETED', processed_at = ?, failure_reason = NULL WHERE batch_id = ?",
                (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), str(batch_id)),
            )

    def retry_classification_batch(self, batch_id: str, reason: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE classification_batches SET status = 'RETRYABLE', failure_reason = ? WHERE batch_id = ?",
                (str(reason), str(batch_id)),
            )

    def query_classification_batches(self, status: Optional[str] = None, limit: int = 100000) -> List[Dict[str, Any]]:
        clauses = " WHERE status = ?" if status else ""
        params = [status, limit] if status else [limit]
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM classification_batches{} ORDER BY start_time ASC, batch_id ASC LIMIT ?".format(clauses),
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            result.append(item)
        return result

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    @staticmethod
    def _metadata_json(event: ActivityEvent) -> str:
        return json.dumps(dict(event.metadata), ensure_ascii=False, sort_keys=True)

    def insert_event(self, event: ActivityEvent) -> bool:
        """Insert one event; return ``False`` if it was already ingested."""

        return self.upsert_event(event) in {"inserted", "updated"}

    def upsert_event(self, event: ActivityEvent) -> str:
        """Insert or refresh one ActivityWatch event.

        ``currentwindow`` watcher rows are mutable heartbeats: ActivityWatch
        keeps the same source event id and increases its duration while the
        application remains active. Treating every repeated source id as an
        immutable duplicate freezes long-running applications at the first
        poll. Other sources remain immutable and return ``duplicate``.
        """

        if not isinstance(event, ActivityEvent):
            raise TypeError("SQLiteStore expects ActivityEvent instances")
        values = (
            event.event_key,
            event.timestamp_iso,
            event.duration,
            event.device,
            event.app,
            event.title,
            event.domain,
            event.url,
            event.browser,
            event.browser_window_id,
            event.browser_tab_id,
            event.source,
            event.bucket_id,
            event.source_event_id,
            self._metadata_json(event),
        )
        with self._lock:
            existing = self._connection.execute(
                "SELECT duration, metadata_json FROM normalized_events WHERE event_key = ?",
                (event.event_key,),
            ).fetchone()
            if existing is not None:
                metadata = dict(event.metadata or {})
                mutable = (
                    str(metadata.get("event_kind", "")).casefold() == "window"
                    or str(metadata.get("activitywatch_bucket_type", "")).casefold() in {
                        "currentwindow", "current_window"
                    }
                )
                old_metadata = existing["metadata_json"] or "{}"
                changed = float(existing["duration"]) != float(event.duration) or old_metadata != values[-1]
                if mutable and changed:
                    self._connection.execute(
                        """
                        UPDATE normalized_events SET
                            timestamp = ?, duration = ?, device = ?, app = ?, title = ?,
                            domain = ?, url = ?, browser = ?, browser_window_id = ?,
                            browser_tab_id = ?, source = ?, bucket_id = ?,
                            source_event_id = ?, metadata_json = ?, ingested_at = CURRENT_TIMESTAMP
                        WHERE event_key = ?
                        """,
                        values[1:] + (values[0],),
                    )
                    return "updated"
                return "duplicate"
            self._connection.execute(
                """
                INSERT INTO normalized_events
                (event_key, timestamp, duration, device, app, title, domain, url,
                 browser, browser_window_id, browser_tab_id, source, bucket_id,
                 source_event_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            return "inserted"

    def insert_many(self, events: Iterable[ActivityEvent]) -> int:
        accepted = 0
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                for event in events:
                    if self.insert_event(event):
                        accepted += 1
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return accepted

    insert = insert_event

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> StoredActivityEvent:
        metadata = json.loads(row["metadata_json"] or "{}")
        event = ActivityEvent(
            timestamp=row["timestamp"],
            duration=row["duration"],
            device=row["device"],
            app=row["app"],
            title=row["title"],
            domain=row["domain"],
            url=row["url"],
            browser=row["browser"],
            browser_window_id=row["browser_window_id"],
            browser_tab_id=row["browser_tab_id"],
            source=row["source"],
            bucket_id=row["bucket_id"],
            source_event_id=row["source_event_id"],
            metadata=metadata,
        )
        return StoredActivityEvent(id=int(row["id"]), event=event)

    def query(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        device: Optional[str] = None,
        domain: Optional[str] = None,
        limit: int = 1000,
    ) -> List[StoredActivityEvent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses = []
        parameters = []
        if start is not None:
            clauses.append("timestamp >= ?")
            parameters.append(coerce_timestamp(start).isoformat().replace("+00:00", "Z"))
        if end is not None:
            clauses.append("timestamp < ?")
            parameters.append(coerce_timestamp(end).isoformat().replace("+00:00", "Z"))
        if device:
            clauses.append("device = ?")
            parameters.append(device)
        if domain:
            clauses.append("domain = ?")
            parameters.append(domain.casefold().rstrip("."))

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM normalized_events{} ORDER BY timestamp ASC, id ASC LIMIT ?".format(where)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._row_to_event(row) for row in rows]

    def list_events(self, *args: Any, **kwargs: Any) -> List[StoredActivityEvent]:
        return self.query(*args, **kwargs)

    def count(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS count FROM normalized_events").fetchone()
        return int(row["count"])

    def count_sessions(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS count FROM activity_sessions").fetchone()
        return int(row["count"])

    def count_meaningful_sessions(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS count FROM meaningful_sessions").fetchone()
        return int(row["count"])

    def count_classifications(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS count FROM semantic_classifications").fetchone()
        return int(row["count"])

    def query_recent_events(self, limit: int = 100) -> List[StoredActivityEvent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM normalized_events ORDER BY timestamp DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def latest_event_timestamp(self) -> Optional[datetime]:
        """Return the newest persisted event timestamp for incremental workers."""

        with self._lock:
            row = self._connection.execute(
                "SELECT timestamp FROM normalized_events ORDER BY timestamp DESC, id DESC LIMIT 1"
            ).fetchone()
        return coerce_timestamp(row["timestamp"]) if row else None

    def get_state(self, key: str) -> Optional[str]:
        """Read a small durable runtime cursor or checkpoint."""

        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM daemon_state WHERE key = ?", (str(key),)
            ).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: Any) -> None:
        """Write a small durable runtime cursor or checkpoint atomically."""

        with self._lock:
            self._connection.execute(
                """
                INSERT INTO daemon_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(key), str(value)),
            )

    def insert_session(self, session: ActivitySession) -> bool:
        """Persist one session, replacing a previous calculation of the same ID."""

        if not isinstance(session, ActivitySession):
            raise TypeError("SQLiteStore expects ActivitySession instances")
        requested_id = session.id
        values = (
            requested_id,
            session.start.isoformat().replace("+00:00", "Z"),
            session.end.isoformat().replace("+00:00", "Z"),
            session.duration,
            session.device,
            session.app,
            session.title,
            session.domain,
            session.url,
            session.browser,
            session.browser_window_id,
            session.browser_tab_id,
            session.source,
            session.event_count,
            json.dumps(list(session.event_keys), ensure_ascii=False),
            json.dumps(dict(session.metadata), ensure_ascii=False, sort_keys=True, default=str),
        )
        with self._lock:
            existing = self._scan_session_overlap_locked()
            return self._insert_one_session_locked(session, values, existing)

    def _scan_session_overlap_locked(self) -> list:
        """Snapshot (id, event-key set, start, end) tuples once for bulk upsert.

        The previous implementation re-scanned the whole table for every
        inserted row (O(n^2)); a day with a few thousand sessions held the
        store lock for nearly a minute and stalled every dashboard read.
        Callers mutate the returned list via _insert_one_session_locked so
        later rows in the same batch observe earlier writes.
        """
        existing = []
        for row in self._connection.execute(
            "SELECT id, event_keys_json, start, end FROM activity_sessions"
        ).fetchall():
            try:
                keys = set(json.loads(row["event_keys_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                keys = set()
            existing.append([str(row["id"]), keys, str(row["start"] or ""), str(row["end"] or "")])
        return existing

    def _insert_one_session_locked(self, session: ActivitySession, values: tuple, existing: list) -> bool:
        # A growing currentwindow heartbeat can change the calculated
        # session end (and therefore its derived id). Preserve the first
        # persisted id when any source event key overlaps so refreshes
        # update one ActivitySession instead of creating duplicates.
        # Overlap additionally requires intersecting wall-clock time: two
        # AFK-separated pieces may share evidence keys but must persist as
        # distinct sessions, never merged back together.
        requested_id = session.id
        persisted_id = requested_id
        session_keys = set(session.event_keys)
        session_start = session.start.isoformat().replace("+00:00", "Z")
        session_end = session.end.isoformat().replace("+00:00", "Z")
        overlapping_ids = []
        if session_keys:
            for existing_id, existing_keys, existing_start, existing_end in existing:
                if session_keys.intersection(existing_keys) and existing_start < session_end and session_start < existing_end:
                    overlapping_ids.append(existing_id)
                    if persisted_id == requested_id:
                        persisted_id = existing_id
        if persisted_id != requested_id:
            values = (persisted_id,) + values[1:]
        existed = self._connection.execute(
            "SELECT 1 FROM activity_sessions WHERE id = ?", (session.id,)
        ).fetchone() is not None
        if persisted_id != requested_id:
            existed = True
        self._connection.execute(
            """
            INSERT INTO activity_sessions
            (id, start, end, duration, device, app, title, domain, url, browser,
             browser_window_id, browser_tab_id, source, event_count,
             event_keys_json, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                start = excluded.start,
                end = excluded.end,
                duration = excluded.duration,
                device = excluded.device,
                app = excluded.app,
                title = excluded.title,
                domain = excluded.domain,
                url = excluded.url,
                browser = excluded.browser,
                browser_window_id = excluded.browser_window_id,
                browser_tab_id = excluded.browser_tab_id,
                source = excluded.source,
                event_count = excluded.event_count,
                event_keys_json = excluded.event_keys_json,
                metadata_json = excluded.metadata_json
            """,
            values,
        )
        stale_ids = [item for item in overlapping_ids if item != persisted_id]
        if stale_ids:
            placeholders = ",".join("?" for _ in stale_ids)
            self._connection.execute(
                "DELETE FROM activity_sessions WHERE id IN ({})".format(placeholders),
                stale_ids,
            )
            stale_set = set(stale_ids)
            existing[:] = [entry for entry in existing if entry[0] not in stale_set]
        for entry in existing:
            if entry[0] == persisted_id:
                entry[1] = session_keys
                entry[2] = session_start
                entry[3] = session_end
                break
        else:
            existing.append([persisted_id, session_keys, session_start, session_end])
        return not existed

    def insert_sessions(self, sessions: Iterable[ActivitySession]) -> int:
        created = 0
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                existing = self._scan_session_overlap_locked()
                for session in sessions:
                    if not isinstance(session, ActivitySession):
                        raise TypeError("SQLiteStore expects ActivitySession instances")
                    values = (
                        session.id,
                        session.start.isoformat().replace("+00:00", "Z"),
                        session.end.isoformat().replace("+00:00", "Z"),
                        session.duration,
                        session.device,
                        session.app,
                        session.title,
                        session.domain,
                        session.url,
                        session.browser,
                        session.browser_window_id,
                        session.browser_tab_id,
                        session.source,
                        session.event_count,
                        json.dumps(list(session.event_keys), ensure_ascii=False),
                        json.dumps(dict(session.metadata), ensure_ascii=False, sort_keys=True, default=str),
                    )
                    if self._insert_one_session_locked(session, values, existing):
                        created += 1
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return created

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> ActivitySession:
        return ActivitySession(
            start=row["start"],
            end=row["end"],
            device=row["device"],
            app=row["app"],
            title=row["title"],
            domain=row["domain"],
            url=row["url"],
            browser=row["browser"],
            browser_window_id=row["browser_window_id"],
            browser_tab_id=row["browser_tab_id"],
            source=row["source"],
            event_keys=tuple(json.loads(row["event_keys_json"] or "[]")),
            metadata=json.loads(row["metadata_json"] or "{}"),
            session_id=row["id"],
        )

    def query_sessions(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        device: Optional[str] = None,
        domain: Optional[str] = None,
        limit: int = 1000,
    ) -> List[ActivitySession]:
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses = []
        parameters = []
        if start is not None:
            clauses.append("end > ?")
            parameters.append(coerce_timestamp(start).isoformat().replace("+00:00", "Z"))
        if end is not None:
            clauses.append("start < ?")
            parameters.append(coerce_timestamp(end).isoformat().replace("+00:00", "Z"))
        if device:
            clauses.append("device = ?")
            parameters.append(device)
        if domain:
            clauses.append("domain = ?")
            parameters.append(domain.casefold().rstrip("."))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM activity_sessions{} ORDER BY start ASC, id ASC LIMIT ?".format(where)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._row_to_session(row) for row in rows]

    def insert_classification(self, classification: Classification) -> bool:
        """Persist or refresh the semantic result for one session."""

        if not isinstance(classification, Classification):
            raise TypeError("SQLiteStore expects Classification instances")
        values = (
            classification.session_id,
            classification.category,
            classification.topic,
            classification.project,
            classification.activity_type,
            classification.productivity,
            classification.confidence,
            classification.provider,
            classification.model,
            classification.subcategory,
            classification.activity,
            classification.signal,
            classification.source,
            classification.classification_status,
            classification.service,
            classification.intent_signal,
            int(getattr(classification, "retry_count", 0) or 0),
            getattr(classification, "last_error", None),
            classification.next_retry_at.isoformat().replace("+00:00", "Z") if getattr(classification, "next_retry_at", None) else None,
            str(getattr(classification, "classifier_version", "1") or "1"),
            str(getattr(classification, "prompt_version", "1") or "1"),
            getattr(classification, "evidence_quality", None),
            getattr(classification, "evidence_quality_score", None),
            classification.classified_at.isoformat().replace("+00:00", "Z") if classification.classified_at else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        with self._lock:
            existed = self._connection.execute(
                "SELECT 1 FROM semantic_classifications WHERE session_id = ?",
                (classification.session_id,),
            ).fetchone() is not None
            self._connection.execute(
                """
                INSERT INTO semantic_classifications
                (session_id, category, topic, project, activity_type, productivity,
                confidence, provider, model, subcategory, activity, signal, source,
                classification_status, service, intent_signal, retry_count,
                last_error, next_retry_at, classifier_version, prompt_version, evidence_quality,
                evidence_quality_score, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    category = excluded.category,
                    topic = excluded.topic,
                    project = excluded.project,
                    activity_type = excluded.activity_type,
                    productivity = excluded.productivity,
                    confidence = excluded.confidence,
                    provider = excluded.provider,
                    model = excluded.model,
                    subcategory = excluded.subcategory,
                    activity = excluded.activity,
                    signal = excluded.signal,
                    source = excluded.source,
                    classification_status = excluded.classification_status,
                    service = excluded.service,
                    intent_signal = excluded.intent_signal,
                    retry_count = excluded.retry_count,
                    last_error = excluded.last_error,
                    next_retry_at = excluded.next_retry_at,
                    classifier_version = excluded.classifier_version,
                    prompt_version = excluded.prompt_version,
                    evidence_quality = excluded.evidence_quality,
                    evidence_quality_score = excluded.evidence_quality_score,
                    created_at = excluded.created_at
                """,
                values,
            )
        return not existed

    def insert_classifications(self, classifications: Iterable[Classification]) -> int:
        created = 0
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                for classification in classifications:
                    if self.insert_classification(classification):
                        created += 1
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return created

    @staticmethod
    def _row_to_classification(row: sqlite3.Row) -> Classification:
        # Schema migrations in __init__ guarantee these columns exist.
        return Classification(
            session_id=row["session_id"],
            category=row["category"],
            topic=row["topic"],
            project=row["project"],
            activity_type=row["activity_type"],
            productivity=row["productivity"],
            confidence=row["confidence"],
            provider=row["provider"],
            model=row["model"],
            subcategory=row["subcategory"],
            activity=row["activity"],
            signal=row["signal"],
            source=row["source"],
            classification_status=row["classification_status"],
            classified_at=row["created_at"],
            service=row["service"],
            intent_signal=row["intent_signal"],
            retry_count=int(row["retry_count"] or 0),
            last_error=row["last_error"],
            next_retry_at=row["next_retry_at"],
            classifier_version=row["classifier_version"] or "1",
            prompt_version=(row["prompt_version"] if "prompt_version" in row.keys() and row["prompt_version"] else "1"),
            evidence_quality=row["evidence_quality"] if "evidence_quality" in row.keys() else None,
            evidence_quality_score=row["evidence_quality_score"] if "evidence_quality_score" in row.keys() else None,
        )

    def query_classifications(
        self,
        session_id: Optional[str] = None,
        category: Optional[str] = None,
        productivity: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Classification]:
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses = []
        parameters = []
        if session_id:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        if category:
            clauses.append("category = ?")
            parameters.append(category.casefold())
        if productivity:
            clauses.append("productivity = ?")
            parameters.append(productivity.casefold())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM semantic_classifications{} ORDER BY created_at DESC LIMIT ?".format(where)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._row_to_classification(row) for row in rows]

    def query_classification_map(self, session_ids: Iterable[str]) -> Dict[str, Classification]:
        """Fetch stored classifications for an explicit id set in one query."""

        ids = [str(item) for item in dict.fromkeys(session_ids)]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM semantic_classifications WHERE session_id IN ({})".format(placeholders),
                ids,
            ).fetchall()
        return {row["session_id"]: self._row_to_classification(row) for row in rows}

    def classification_status_counts(self) -> Dict[str, int]:
        """Cheap per-status row counts for queue/health reporting."""

        with self._lock:
            rows = self._connection.execute(
                "SELECT classification_status, COUNT(*) AS total FROM semantic_classifications GROUP BY classification_status"
            ).fetchall()
        return {str(row["classification_status"]): int(row["total"]) for row in rows}

    FEEDBACK_VERDICTS = ("correct", "incorrect")

    def insert_feedback(
        self,
        session_id: str,
        verdict: str,
        note: Optional[str] = None,
        source: str = "user",
        model: Optional[str] = None,
        classifier_version: Optional[str] = None,
        prompt_version: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record (or re-record) a human verdict for one session.

        Latest row per (session_id, source) wins. The referenced
        classification row is never modified.
        """
        verdict = str(verdict or "").strip().lower()
        if verdict not in self.FEEDBACK_VERDICTS:
            raise ValueError("verdict must be one of {}".format(list(self.FEEDBACK_VERDICTS)))
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        source = str(source or "user").strip() or "user"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._lock:
            self._connection.execute(
                """INSERT INTO classification_feedback
                   (session_id, verdict, note, source, model,
                    classifier_version, prompt_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, source) DO UPDATE SET
                       verdict = excluded.verdict,
                       note = excluded.note,
                       model = excluded.model,
                       classifier_version = excluded.classifier_version,
                       prompt_version = excluded.prompt_version,
                       created_at = excluded.created_at
                """,
                (session_id, verdict,
                 str(note).strip() if note else None, source,
                 str(model).strip() if model else None,
                 str(classifier_version).strip() if classifier_version else None,
                 str(prompt_version).strip() if prompt_version else None,
                 now),
            )
            row = self._connection.execute(
                "SELECT * FROM classification_feedback WHERE session_id = ? AND source = ?",
                (session_id, source),
            ).fetchone()
        return dict(row) if row else {}

    def get_feedback(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM classification_feedback WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (str(session_id),),
            ).fetchone()
        return dict(row) if row else None

    def feedback_quality(self, model: Optional[str] = None) -> Dict[str, Any]:
        """Accuracy aggregates. No labels -> accuracy None (never invented)."""
        clauses, parameters = [], []
        if model:
            clauses.append("model = ?")
            parameters.append(str(model))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT verdict, COUNT(*) AS total FROM classification_feedback{} GROUP BY verdict".format(where),
                parameters,
            ).fetchall()
            per_model = []
            if model is None:
                per_model = [
                    {"model": item["model"], "feedback_count": int(item["total"])}
                    for item in self._connection.execute(
                        "SELECT model, COUNT(*) AS total FROM classification_feedback GROUP BY model"
                    ).fetchall()
                ]
        counts = {str(item["verdict"]): int(item["total"]) for item in rows}
        correct = counts.get("correct", 0)
        incorrect = counts.get("incorrect", 0)
        total = correct + incorrect
        payload = {
            "model": model,
            "feedback_count": total,
            "correct_count": correct,
            "incorrect_count": incorrect,
            "accuracy": (round(correct / total, 4) if total else None),
        }
        if model is None:
            with self._lock:
                names = [
                    item["model"]
                    for item in self._connection.execute(
                        "SELECT DISTINCT model FROM classification_feedback"
                    ).fetchall()
                ]
            payload["per_model"] = [
                self.feedback_quality(name) for name in names
            ]
        return payload

    def insert_intent(self, intent: Intent) -> bool:
        if not isinstance(intent, Intent):
            raise TypeError("SQLiteStore expects Intent instances")
        values = (
            intent.id,
            intent.text,
            intent.goal,
            intent.topic,
            intent.project,
            json.dumps(list(intent.keywords), ensure_ascii=False),
            intent.success_criteria,
            intent.confidence,
            intent.provider,
            intent.model,
            intent.created_at.isoformat().replace("+00:00", "Z"),
        )
        with self._lock:
            existed = self._connection.execute(
                "SELECT 1 FROM intents WHERE id = ?", (intent.id,)
            ).fetchone() is not None
            self._connection.execute(
                """
                INSERT OR IGNORE INTO intents
                (id, text, goal, topic, project, keywords_json, success_criteria,
                 confidence, provider, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return not existed

    def query_intents(self, limit: int = 1000) -> List[Intent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM intents ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Intent(
                text=row["text"],
                goal=row["goal"],
                topic=row["topic"],
                project=row["project"],
                keywords=tuple(json.loads(row["keywords_json"] or "[]")),
                success_criteria=row["success_criteria"],
                confidence=row["confidence"],
                provider=row["provider"],
                model=row["model"],
                created_at=row["created_at"],
                intent_id=row["id"],
            )
            for row in rows
        ]

    def insert_alignment(self, alignment: AlignmentResult) -> bool:
        if not isinstance(alignment, AlignmentResult):
            raise TypeError("SQLiteStore expects AlignmentResult instances")
        alignment_id = "{}:{}".format(alignment.session_id, alignment.intent_id)
        values = (
            alignment_id,
            alignment.session_id,
            alignment.intent_id,
            int(alignment.aligned),
            alignment.score,
            alignment.confidence,
            alignment.reason,
        )
        with self._lock:
            existed = self._connection.execute(
                "SELECT 1 FROM alignments WHERE id = ?", (alignment_id,)
            ).fetchone() is not None
            self._connection.execute(
                """
                INSERT INTO alignments
                (id, session_id, intent_id, aligned, score, confidence, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    aligned = excluded.aligned,
                    score = excluded.score,
                    confidence = excluded.confidence,
                    reason = excluded.reason
                """,
                values,
            )
        return not existed

    def insert_alignments(self, alignments: Iterable[AlignmentResult]) -> int:
        created = 0
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                for alignment in alignments:
                    if self.insert_alignment(alignment):
                        created += 1
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return created

    def query_alignments(
        self,
        intent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 1000,
    ) -> List[AlignmentResult]:
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses = []
        parameters = []
        if intent_id:
            clauses.append("intent_id = ?")
            parameters.append(intent_id)
        if session_id:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM alignments{} ORDER BY created_at DESC LIMIT ?".format(where)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [
            AlignmentResult(
                session_id=row["session_id"],
                intent_id=row["intent_id"],
                aligned=bool(row["aligned"]),
                score=row["score"],
                confidence=row["confidence"],
                reason=row["reason"],
            )
            for row in rows
        ]

    def insert_behavior_observation(self, observation: BehaviorObservation) -> bool:
        if not isinstance(observation, BehaviorObservation):
            raise TypeError("SQLiteStore expects BehaviorObservation instances")
        values = (
            observation.session_id,
            observation.state.value,
            observation.started_at.isoformat().replace("+00:00", "Z"),
            observation.confidence,
            observation.distraction_score,
            int(observation.actionable),
            observation.reason,
            observation.previous_state.value if observation.previous_state else None,
        )
        with self._lock:
            existed = self._connection.execute(
                "SELECT 1 FROM behavior_observations WHERE session_id = ?",
                (observation.session_id,),
            ).fetchone() is not None
            self._connection.execute(
                """
                INSERT INTO behavior_observations
                (session_id, state, started_at, confidence, distraction_score,
                 actionable, reason, previous_state)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state = excluded.state,
                    started_at = excluded.started_at,
                    confidence = excluded.confidence,
                    distraction_score = excluded.distraction_score,
                    actionable = excluded.actionable,
                    reason = excluded.reason,
                    previous_state = excluded.previous_state
                """,
                values,
            )
        return not existed

    def insert_behavior_observations(self, observations: Iterable[BehaviorObservation]) -> int:
        created = 0
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                for observation in observations:
                    if self.insert_behavior_observation(observation):
                        created += 1
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return created

    def query_behavior_observations(
        self,
        state: Optional[str] = None,
        actionable: Optional[bool] = None,
        limit: int = 1000,
    ) -> List[BehaviorObservation]:
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses = []
        parameters = []
        if state:
            clauses.append("state = ?")
            parameters.append(BehaviorState(state).value)
        if actionable is not None:
            clauses.append("actionable = ?")
            parameters.append(int(actionable))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM behavior_observations{} ORDER BY started_at DESC LIMIT ?".format(where)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [
            BehaviorObservation(
                session_id=row["session_id"],
                state=BehaviorState(row["state"]),
                started_at=row["started_at"],
                confidence=row["confidence"],
                distraction_score=row["distraction_score"],
                actionable=bool(row["actionable"]),
                reason=row["reason"],
                previous_state=BehaviorState(row["previous_state"]) if row["previous_state"] else None,
            )
            for row in rows
        ]

    def insert_intervention(self, intervention: Intervention) -> bool:
        if not isinstance(intervention, Intervention):
            raise TypeError("SQLiteStore expects Intervention instances")
        values = (
            intervention.id,
            intervention.session_id,
            intervention.mode.value if intervention.mode else None,
            intervention.status.value,
            intervention.reason,
            json.dumps(dict(intervention.payload), ensure_ascii=False, sort_keys=True, default=str),
            intervention.created_at.isoformat().replace("+00:00", "Z"),
            intervention.executed_at.isoformat().replace("+00:00", "Z") if intervention.executed_at else None,
        )
        with self._lock:
            existed = self._connection.execute(
                "SELECT 1 FROM interventions WHERE id = ?", (intervention.id,)
            ).fetchone() is not None
            self._connection.execute(
                """
                INSERT INTO interventions
                (id, session_id, mode, status, reason, payload_json, created_at, executed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    mode = excluded.mode,
                    status = excluded.status,
                    reason = excluded.reason,
                    payload_json = excluded.payload_json,
                    executed_at = excluded.executed_at
                """,
                values,
            )
        return not existed

    def record_intervention_action(
        self,
        intervention_id: str,
        action: str,
        state: Optional[str] = None,
        target: Optional[dict] = None,
        metadata: Optional[dict] = None,
        created_at: Optional[Any] = None,
    ) -> int:
        """Persist browser/desktop delivery and user interaction telemetry."""

        if not str(intervention_id).strip():
            raise ValueError("intervention_id cannot be empty")
        if not str(action).strip():
            raise ValueError("action cannot be empty")
        timestamp = coerce_timestamp(created_at or datetime.now(timezone.utc))
        action_name = str(action).strip().upper()
        action_names = {
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
            "SENT": "sent",
            "DESKTOP_NOTIFICATION": "desktop_notification",
            "SUPPRESSED": "suppressed",
            "QUEUED": "queued",
            "NO_EXACT_TARGET": "no_exact_target",
        }
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT INTO intervention_actions
                (intervention_id, action, state, target_json, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(intervention_id),
                    action_names.get(action_name, action_name.casefold()),
                    str(state).strip().upper() if state else None,
                    json.dumps(dict(target or {}), ensure_ascii=False, sort_keys=True, default=str),
                    json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True, default=str),
                    timestamp.isoformat().replace("+00:00", "Z"),
                ),
            )
            return int(cursor.lastrowid)

    def query_intervention_actions(
        self,
        intervention_id: Optional[str] = None,
        limit: int = 1000,
    ) -> List[dict]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if intervention_id:
            query = "SELECT * FROM intervention_actions WHERE intervention_id = ? ORDER BY created_at ASC, id ASC LIMIT ?"
            parameters = (str(intervention_id), limit)
        else:
            query = "SELECT * FROM intervention_actions ORDER BY created_at DESC, id DESC LIMIT ?"
            parameters = (limit,)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [
            {
                "id": int(row["id"]),
                "intervention_id": row["intervention_id"],
                "action": row["action"],
                "state": row["state"],
                "target": json.loads(row["target_json"] or "{}"),
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def query_interventions(
        self,
        session_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Intervention]:
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses = []
        parameters = []
        if session_id:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        if status:
            clauses.append("status = ?")
            parameters.append(InterventionStatus(status).value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM interventions{} ORDER BY created_at DESC LIMIT ?".format(where)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [
            Intervention(
                session_id=row["session_id"],
                mode=InterventionMode(row["mode"]) if row["mode"] else None,
                status=InterventionStatus(row["status"]),
                reason=row["reason"],
                payload=json.loads(row["payload_json"] or "{}"),
                created_at=row["created_at"],
                executed_at=row["executed_at"] if row["executed_at"] else None,
                intervention_id=row["id"],
            )
            for row in rows
        ]

    def insert_meme(self, meme: MemePayload) -> bool:
        if not isinstance(meme, MemePayload):
            raise TypeError("SQLiteStore expects MemePayload instances")
        values = (
            meme.id, meme.session_id, meme.template, meme.severity, meme.top,
            meme.bottom, meme.tone, meme.provider, meme.model,
            meme.created_at.isoformat().replace("+00:00", "Z"),
        )
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO memes
                (id, session_id, template, severity, top, bottom, tone, provider, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return cursor.rowcount == 1

    def query_memes(self, session_id: Optional[str] = None, limit: int = 1000) -> List[MemePayload]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if session_id:
            query = "SELECT * FROM memes WHERE session_id = ? ORDER BY created_at DESC LIMIT ?"
            parameters = [session_id, limit]
        else:
            query = "SELECT * FROM memes ORDER BY created_at DESC LIMIT ?"
            parameters = [limit]
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [
            MemePayload(
                session_id=row["session_id"], template=row["template"], severity=row["severity"],
                top=row["top"], bottom=row["bottom"], tone=row["tone"], provider=row["provider"],
                model=row["model"], created_at=row["created_at"], meme_id=row["id"],
            )
            for row in rows
        ]

    def insert_outcome(self, outcome: InterventionOutcome) -> bool:
        if not isinstance(outcome, InterventionOutcome):
            raise TypeError("SQLiteStore expects InterventionOutcome instances")
        values = (
            outcome.id, outcome.intervention_id,
            outcome.intervention_time.isoformat().replace("+00:00", "Z"),
            outcome.recovery_status.value,
            outcome.recovery_time.isoformat().replace("+00:00", "Z") if outcome.recovery_time else None,
            outcome.recovery_session_id, outcome.recovery_duration_seconds,
            outcome.intervention_type, outcome.meme_id,
        )
        with self._lock:
            existed = self._connection.execute(
                "SELECT 1 FROM intervention_outcomes WHERE id = ?", (outcome.id,)
            ).fetchone() is not None
            self._connection.execute(
                """
                INSERT INTO intervention_outcomes
                (id, intervention_id, intervention_time, recovery_status, recovery_time,
                 recovery_session_id, recovery_duration_seconds, intervention_type, meme_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    recovery_status = excluded.recovery_status,
                    recovery_time = excluded.recovery_time,
                    recovery_session_id = excluded.recovery_session_id,
                    recovery_duration_seconds = excluded.recovery_duration_seconds,
                    meme_id = excluded.meme_id
                """,
                values,
            )
        return not existed

    def query_outcomes(
        self,
        intervention_id: Optional[str] = None,
        recovery_status: Optional[str] = None,
        limit: int = 1000,
    ) -> List[InterventionOutcome]:
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses = []
        parameters = []
        if intervention_id:
            clauses.append("intervention_id = ?")
            parameters.append(intervention_id)
        if recovery_status:
            clauses.append("recovery_status = ?")
            parameters.append(RecoveryStatus(recovery_status).value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM intervention_outcomes{} ORDER BY intervention_time DESC LIMIT ?".format(where)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [
            InterventionOutcome(
                intervention_id=row["intervention_id"], intervention_time=row["intervention_time"],
                recovery_status=RecoveryStatus(row["recovery_status"]),
                recovery_time=row["recovery_time"] if row["recovery_time"] else None,
                recovery_session_id=row["recovery_session_id"],
                recovery_duration_seconds=row["recovery_duration_seconds"],
                intervention_type=row["intervention_type"], meme_id=row["meme_id"], outcome_id=row["id"],
            )
            for row in rows
        ]

    def insert_detection(self, record: "DetectionRecord") -> bool:
        """Persist one meaningful detection decision (idempotent).

        Deterministic IDs make retried evaluations converge: a repeat
        insert of the same decision is a no-op returning False.
        """
        from noema.application.realtime.records import DetectionRecord

        if not isinstance(record, DetectionRecord):
            raise TypeError("SQLiteStore expects DetectionRecord instances")
        values = (
            record.detection_id,
            record.timestamp.isoformat().replace("+00:00", "Z"),
            record.session_id,
            record.window_start.isoformat().replace("+00:00", "Z"),
            record.window_end.isoformat().replace("+00:00", "Z"),
            int(record.window_minutes),
            record.feature_version,
            float(record.candidate_score),
            record.signals_json,
            record.tracker_state,
            record.model_verdict,
            record.model_confidence,
            record.severity,
            record.recommended_intervention,
            record.provider,
            record.model,
            record.latency_ms,
            record.decision,
            record.reason,
        )
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO distraction_detections
                (detection_id, timestamp, session_id, window_start, window_end,
                 window_minutes, feature_version, candidate_score, signals_json,
                 tracker_state, model_verdict, model_confidence, severity,
                 recommended_intervention, provider, model, latency_ms,
                 decision, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, values,
            )
        return cursor.rowcount == 1

    def query_detections(
        self,
        session_id: Optional[str] = None,
        limit: int = 100,
    ) -> List["DetectionRecord"]:
        from noema.application.realtime.records import DetectionRecord

        if limit < 1:
            raise ValueError("limit must be positive")
        if session_id:
            query = "SELECT * FROM distraction_detections WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?"
            parameters: list = [str(session_id), limit]
        else:
            query = "SELECT * FROM distraction_detections ORDER BY timestamp DESC LIMIT ?"
            parameters = [limit]
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [DetectionRecord.from_row(row) for row in rows]

    def insert_memory(self, memory: Memory) -> bool:
        if not isinstance(memory, Memory):
            raise TypeError("SQLiteStore expects Memory instances")
        values = (
            memory.id, memory.text, memory.kind.value, memory.confidence,
            memory.evidence_count, json.dumps(list(memory.evidence_refs)),
            memory.created_at.isoformat().replace("+00:00", "Z"),
        )
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO memories
                (id, text, kind, confidence, evidence_count, evidence_refs_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """, values,
            )
        return cursor.rowcount == 1

    def query_memories(self, kind: Optional[str] = None, limit: int = 1000) -> List[Memory]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if kind:
            query = "SELECT * FROM memories WHERE kind = ? ORDER BY created_at DESC LIMIT ?"
            parameters = [MemoryKind(kind).value, limit]
        else:
            query = "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?"
            parameters = [limit]
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [
            Memory(
                text=row["text"], kind=MemoryKind(row["kind"]), confidence=row["confidence"],
                evidence_count=row["evidence_count"], evidence_refs=tuple(json.loads(row["evidence_refs_json"] or "[]")),
                created_at=row["created_at"], memory_id=row["id"],
            )
            for row in rows
        ]

    def insert_meaningful_session(self, session: MeaningfulSession) -> bool:
        if not isinstance(session, MeaningfulSession):
            raise TypeError("SQLiteStore expects MeaningfulSession instances")
        requested_id = session.id
        values = (
            requested_id,
            session.start_time.isoformat().replace("+00:00", "Z"),
            session.end_time.isoformat().replace("+00:00", "Z"),
            session.duration,
            json.dumps(list(session.device_set)),
            json.dumps(list(session.activity_session_ids)),
            session.primary_project,
            session.primary_task,
            session.primary_topic,
            json.dumps(list(session.activities)),
            session.dominant_category,
            session.dominant_activity_type,
            session.intent_id,
            session.alignment_score,
            session.focus_score,
            session.distraction_score,
            session.context_switch_count,
            session.active_duration_seconds,
            session.afk_duration_seconds,
            session.status.value,
            session.confidence,
            json.dumps(session.summary, ensure_ascii=False, default=str) if session.summary is not None else None,
            session.outcome.value,
            json.dumps(list(session.evidence), ensure_ascii=False),
            session.evidence_quality.value if hasattr(session, 'evidence_quality') and session.evidence_quality else "absent",
            json.dumps([phase.to_dict() for phase in session.phases], ensure_ascii=False),
            session.browser,
            session.browser_window_id,
            session.browser_tab_id,
        )
        with self._lock:
            existing = self._scan_meaningful_overlap_locked()
            return self._insert_one_meaningful_locked(session, values, existing)

    def _scan_meaningful_overlap_locked(self) -> list:
        """Snapshot (id, activity-session-id set) pairs once for bulk upsert.

        Same O(n^2) trap as activity sessions: without this snapshot every
        inserted row re-scanned the whole table and stalled dashboard reads.
        """
        existing = []
        for row in self._connection.execute(
            "SELECT id, activity_session_ids_json, start_time, end_time FROM meaningful_sessions"
        ).fetchall():
            try:
                ids = set(json.loads(row["activity_session_ids_json"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                ids = set()
            existing.append([str(row["id"]), ids, str(row["start_time"] or ""), str(row["end_time"] or "")])
        return existing

    def _insert_one_meaningful_locked(self, session: MeaningfulSession, values: tuple, existing: list) -> bool:
        requested_id = session.id
        with self._lock:
            persisted_id = requested_id
            session_ids = set(session.activity_session_ids)
            session_start = session.start_time.isoformat().replace("+00:00", "Z")
            session_end = session.end_time.isoformat().replace("+00:00", "Z")
            overlapping_ids = []
            if session_ids:
                for existing_id, existing_ids, existing_start, existing_end in existing:
                    if session_ids.intersection(existing_ids) and existing_start < session_end and session_start < existing_end:
                        overlapping_ids.append(existing_id)
                        if persisted_id == requested_id:
                            persisted_id = existing_id
            if persisted_id != requested_id:
                values = (persisted_id,) + values[1:]
            existed = self._connection.execute(
                "SELECT 1 FROM meaningful_sessions WHERE id = ?", (persisted_id,)
            ).fetchone() is not None
            self._connection.execute(
                """
                INSERT INTO meaningful_sessions
                (id, start_time, end_time, duration, device_set_json, activity_session_ids_json,
                 primary_project, primary_task, primary_topic, activities_json,
                 dominant_category, dominant_activity_type, intent_id, alignment_score,
                 focus_score, distraction_score, context_switch_count,
                 active_duration_seconds, afk_duration_seconds,
                 status, confidence,
                 summary_json, outcome, evidence_json, evidence_quality, phases_json, browser,
                 browser_window_id, browser_tab_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    start_time = excluded.start_time, end_time = excluded.end_time,
                    duration = excluded.duration, device_set_json = excluded.device_set_json,
                    activity_session_ids_json = excluded.activity_session_ids_json,
                    primary_project = excluded.primary_project, primary_task = excluded.primary_task,
                    primary_topic = excluded.primary_topic, activities_json = excluded.activities_json,
                    dominant_category = excluded.dominant_category,
                    dominant_activity_type = excluded.dominant_activity_type,
                    intent_id = excluded.intent_id, alignment_score = excluded.alignment_score,
                    focus_score = excluded.focus_score, distraction_score = excluded.distraction_score,
                    context_switch_count = excluded.context_switch_count,
                    active_duration_seconds = excluded.active_duration_seconds,
                    afk_duration_seconds = excluded.afk_duration_seconds,
                    status = excluded.status,
                    confidence = excluded.confidence, summary_json = excluded.summary_json,
                    outcome = excluded.outcome, evidence_json = excluded.evidence_json,
                    evidence_quality = excluded.evidence_quality,
                    phases_json = excluded.phases_json, browser = excluded.browser,
                    browser_window_id = excluded.browser_window_id,
                    browser_tab_id = excluded.browser_tab_id
                """,
                values,
            )
            stale_ids = [item for item in overlapping_ids if item != persisted_id]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                self._connection.execute(
                    "DELETE FROM meaningful_sessions WHERE id IN ({})".format(placeholders),
                    stale_ids,
                )
                stale_set = set(stale_ids)
                existing[:] = [entry for entry in existing if entry[0] not in stale_set]
            for entry in existing:
                if entry[0] == persisted_id:
                    entry[1] = session_ids
                    entry[2] = session_start
                    entry[3] = session_end
                    break
            else:
                existing.append([persisted_id, session_ids, session_start, session_end])
        return not existed

    def insert_meaningful_sessions(self, sessions: Iterable[MeaningfulSession]) -> int:
        created = 0
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                existing = self._scan_meaningful_overlap_locked()
                for session in sessions:
                    if not isinstance(session, MeaningfulSession):
                        raise TypeError("SQLiteStore expects MeaningfulSession instances")
                    values = (
                        session.id,
                        session.start_time.isoformat().replace("+00:00", "Z"),
                        session.end_time.isoformat().replace("+00:00", "Z"),
                        session.duration,
                        json.dumps(list(session.device_set)),
                        json.dumps(list(session.activity_session_ids)),
                        session.primary_project,
                        session.primary_task,
                        session.primary_topic,
                        json.dumps(list(session.activities)),
                        session.dominant_category,
                        session.dominant_activity_type,
                        session.intent_id,
                        session.alignment_score,
                        session.focus_score,
                        session.distraction_score,
                        session.context_switch_count,
                        session.active_duration_seconds,
                        session.afk_duration_seconds,
                        session.status.value,
                        session.confidence,
                        json.dumps(session.summary, ensure_ascii=False, default=str) if session.summary is not None else None,
                        session.outcome.value,
                        json.dumps(list(session.evidence), ensure_ascii=False),
                        session.evidence_quality.value if hasattr(session, 'evidence_quality') and session.evidence_quality else "absent",
                        json.dumps([phase.to_dict() for phase in session.phases], ensure_ascii=False),
                        session.browser,
                        session.browser_window_id,
                        session.browser_tab_id,
                    )
                    if self._insert_one_meaningful_locked(session, values, existing):
                        created += 1
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return created

    @staticmethod
    def _row_to_meaningful_session(row: sqlite3.Row) -> MeaningfulSession:
        phase_data = json.loads(row["phases_json"] or "[]")
        phases = tuple(
            SessionPhase(
                phase_type=item["phase_type"], start_time=item["start_time"], end_time=item["end_time"],
                activity_session_ids=tuple(item.get("activity_session_ids", [])), confidence=item.get("confidence", 0.0),
            )
            for item in phase_data
        )
        # evidence_quality may not exist in older databases
        try:
            eq_value = row["evidence_quality"]
        except (IndexError, KeyError):
            eq_value = "absent"
        from noema.domain.meaningful.models import EvidenceQuality
        try:
            evidence_quality = EvidenceQuality(eq_value) if eq_value else EvidenceQuality.ABSENT
        except ValueError:
            evidence_quality = EvidenceQuality.ABSENT
        return MeaningfulSession(
            start_time=row["start_time"], end_time=row["end_time"],
            device_set=tuple(json.loads(row["device_set_json"] or "[]")),
            activity_session_ids=tuple(json.loads(row["activity_session_ids_json"] or "[]")),
            primary_project=row["primary_project"], primary_task=row["primary_task"], primary_topic=row["primary_topic"],
            activities=tuple(json.loads(row["activities_json"] or "[]")),
            dominant_category=row["dominant_category"], dominant_activity_type=row["dominant_activity_type"],
            intent_id=row["intent_id"], alignment_score=row["alignment_score"], focus_score=row["focus_score"],
            distraction_score=row["distraction_score"], context_switch_count=row["context_switch_count"],
            active_duration_seconds=row["active_duration_seconds"] or 0.0,
            afk_duration_seconds=row["afk_duration_seconds"] or 0.0,
            status=MeaningfulSessionStatus(row["status"]), confidence=row["confidence"],
            summary=json.loads(row["summary_json"]) if row["summary_json"] else None,
            outcome=MeaningfulSessionOutcome(row["outcome"]),
            evidence=tuple(json.loads(row["evidence_json"] or "[]")),
            evidence_quality=evidence_quality,
            phases=phases, session_id=row["id"],
            browser=row["browser"], browser_window_id=row["browser_window_id"],
            browser_tab_id=row["browser_tab_id"],
        )

    def query_meaningful_sessions(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        project: Optional[str] = None,
        intent_id: Optional[str] = None,
        limit: int = 1000,
    ) -> List[MeaningfulSession]:
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses = []
        parameters = []
        if start is not None:
            clauses.append("end_time > ?")
            parameters.append(coerce_timestamp(start).isoformat().replace("+00:00", "Z"))
        if end is not None:
            clauses.append("start_time < ?")
            parameters.append(coerce_timestamp(end).isoformat().replace("+00:00", "Z"))
        if project:
            clauses.append("primary_project = ?")
            parameters.append(project)
        if intent_id:
            clauses.append("intent_id = ?")
            parameters.append(intent_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM meaningful_sessions{} ORDER BY start_time ASC, id ASC LIMIT ?".format(where)
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._row_to_meaningful_session(row) for row in rows]
