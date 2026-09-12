"""Persistence for observability records on the product SQLite database.

Tables live in the same database (same connection, WAL preserved,
``:memory:``-friendly) but are strictly separate from domain truth:
nothing here is ever read as activity, classification, or outcome data.
Retention pruning touches ONLY these tables, never product tables.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from noema.observability.models import (
    BenchmarkResult,
    BenchmarkRun,
    InvocationRecord,
    OperationRecord,
)

SCHEMA = """
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

OBSERVABILITY_TABLES = (
    "model_invocations",
    "operation_timings",
    "benchmark_runs",
    "benchmark_results",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class TelemetryRepository:
    """Query/persist layer over the observability tables.

    Operates on the product store's connection/lock (same WAL, same
    thread-safety) without touching domain tables.
    """

    def __init__(self, store: Any):
        self._store = store
        self.ensure_schema()

    @property
    def _connection(self):  # pragma: no cover - trivial accessor
        return self._store._connection

    @property
    def _lock(self):  # pragma: no cover - trivial accessor
        return self._store._lock

    def ensure_schema(self) -> None:
        with self._lock:
            self._connection.executescript(SCHEMA)

    # -- invocations ----------------------------------------------------

    def insert_invocation(self, record: InvocationRecord) -> bool:
        values = (
            record.invocation_id, record.request_id, record.timestamp,
            record.provider, record.model, record.purpose, record.pipeline,
            record.operation, record.kind, record.session_ids_json,
            int(record.batch_size), int(record.attempt_number),
            int(record.fallback_depth), record.fallback_from,
            record.fallback_reason, int(bool(record.success)),
            record.error_type, record.error_code,
            (record.error_message[:500] if record.error_message else None),
            record.input_tokens, record.output_tokens, record.total_tokens,
            record.token_basis, record.latency_ms, record.ttft_ms,
            record.generation_ms, record.tps, record.tps_basis,
            record.context_window, record.max_output_tokens,
            record.quota_scope, record.quota_before_json,
            record.quota_after_json, record.cost, record.cost_basis,
            record.batch_budget_tokens, record.batch_input_tokens,
            record.batch_utilization, record.detection_id,
            record.intervention_id, record.prompt_version,
            record.classifier_version, record.created_at,
        )
        with self._lock:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO model_invocations
                   (invocation_id, request_id, timestamp, provider, model,
                    purpose, pipeline, operation, kind, session_ids_json,
                    batch_size, attempt_number, fallback_depth, fallback_from,
                    fallback_reason, success, error_type, error_code,
                    error_message, input_tokens, output_tokens, total_tokens,
                    token_basis, latency_ms, ttft_ms, generation_ms, tps,
                    tps_basis, context_window, max_output_tokens, quota_scope,
                    quota_before_json, quota_after_json, cost, cost_basis,
                     batch_budget_tokens, batch_input_tokens,
                     batch_utilization, detection_id, intervention_id,
                     prompt_version, classifier_version,
                     created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            return cursor.rowcount == 1

    def query_invocations(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        purpose: Optional[str] = None,
        pipeline: Optional[str] = None,
        operation: Optional[str] = None,
        kind: Optional[str] = None,
        since: Optional[str] = None,
        success: Optional[bool] = None,
        limit: int = 100000,
    ) -> List[InvocationRecord]:
        clauses, parameters = [], []
        if provider is not None:
            clauses.append("provider = ?")
            parameters.append(str(provider))
        if model is not None:
            clauses.append("model = ?")
            parameters.append(str(model))
        if purpose is not None:
            clauses.append("purpose = ?")
            parameters.append(str(purpose))
        if pipeline is not None:
            clauses.append("pipeline = ?")
            parameters.append(str(pipeline))
        if operation is not None:
            clauses.append("operation = ?")
            parameters.append(str(operation))
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(str(kind))
        if since is not None:
            clauses.append("timestamp >= ?")
            parameters.append(str(since))
        if success is not None:
            clauses.append("success = ?")
            parameters.append(1 if success else 0)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM model_invocations{} ORDER BY timestamp ASC LIMIT ?".format(where)
        parameters.append(max(1, int(limit)))
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [InvocationRecord.from_row(dict(row)) for row in rows]

    # -- operations ------------------------------------------------------

    def insert_operation(self, record: OperationRecord) -> int:
        with self._lock:
            cursor = self._connection.execute(
                """INSERT INTO operation_timings
                   (timestamp, kind, name, duration_ms, success, error,
                    rows_affected, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.timestamp, record.kind, record.name,
                 record.duration_ms, int(bool(record.success)), record.error,
                 record.rows_affected, record.metadata_json,
                 _utcnow_iso()),
            )
            return int(cursor.lastrowid)

    def query_operations(
        self,
        kind: Optional[str] = None,
        name: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100000,
    ) -> List[Dict[str, Any]]:
        clauses, parameters = [], []
        if kind is not None:
            clauses.append("kind = ?")
            parameters.append(str(kind))
        if name is not None:
            clauses.append("name = ?")
            parameters.append(str(name))
        if since is not None:
            clauses.append("timestamp >= ?")
            parameters.append(str(since))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = "SELECT * FROM operation_timings{} ORDER BY timestamp ASC LIMIT ?".format(where)
        parameters.append(max(1, int(limit)))
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.get("metadata_json") or "{}")
            except ValueError:
                item["metadata"] = {}
            out.append(item)
        return out

    # -- benchmarks ------------------------------------------------------

    def insert_benchmark_run(self, run: BenchmarkRun) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO benchmark_runs
                   (run_id, timestamp, environment_json, test_set, purpose,
                    iterations, duration_ms, status, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run.run_id, run.timestamp, run.environment_json,
                 run.test_set, run.purpose, int(run.iterations),
                 run.duration_ms, run.status, run.note, run.created_at),
            )
            return cursor.rowcount == 1

    def insert_benchmark_result(self, result: BenchmarkResult) -> int:
        with self._lock:
            cursor = self._connection.execute(
                """INSERT INTO benchmark_results
                   (run_id, provider, model, fixture, iterations,
                    success_count, schema_valid_count, failure_count,
                    mean_latency_ms, p50_latency_ms, p95_latency_ms,
                    mean_out_tokens, mean_tps, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (result.run_id, result.provider, result.model,
                 result.fixture, int(result.iterations),
                 int(result.success_count), int(result.schema_valid_count),
                 int(result.failure_count), result.mean_latency_ms,
                 result.p50_latency_ms, result.p95_latency_ms,
                 result.mean_out_tokens, result.mean_tps,
                 result.created_at),
            )
            return int(cursor.lastrowid)

    def list_benchmark_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM benchmark_runs ORDER BY timestamp DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["environment"] = json.loads(item.get("environment_json") or "{}")
            except ValueError:
                item["environment"] = {}
            out.append(item)
        return out

    def get_benchmark_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM benchmark_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            results = self._connection.execute(
                "SELECT * FROM benchmark_results WHERE run_id = ? ORDER BY provider, model, fixture",
                (str(run_id),),
            ).fetchall()
        if row is None:
            return None
        payload = dict(row)
        try:
            payload["environment"] = json.loads(payload.get("environment_json") or "{}")
        except ValueError:
            payload["environment"] = {}
        payload["results"] = [dict(item) for item in results]
        return payload

    # -- retention --------------------------------------------------------

    def prune(self, older_than_days: float) -> Dict[str, int]:
        """Delete observability rows older than the cutoff.

        ONLY observability tables. Product tables are never touched here.
        Timestamp columns store UTC ISO; string comparison is chronological.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0.0, float(older_than_days))))
        cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
        removed: Dict[str, int] = {}
        with self._lock:
            for table in ("model_invocations", "operation_timings", "benchmark_runs"):
                cursor = self._connection.execute(
                    "DELETE FROM {} WHERE timestamp < ?".format(table), (cutoff_iso,)
                )
                removed[table] = cursor.rowcount
            # Results have no timestamp of their own; they follow their run.
            cursor = self._connection.execute(
                "DELETE FROM benchmark_results WHERE run_id NOT IN "
                "(SELECT run_id FROM benchmark_runs)"
            )
            removed["benchmark_results"] = cursor.rowcount
        return removed

    def count_tables(self) -> Dict[str, int]:
        with self._lock:
            return {
                table: self._connection.execute(
                    "SELECT COUNT(*) AS n FROM {}".format(table)).fetchone()["n"]
                for table in OBSERVABILITY_TABLES
            }
