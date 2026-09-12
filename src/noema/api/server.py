"""Dependency-free local WSGI API for the Generation 0 foundation."""

from __future__ import annotations

import json
import mimetypes
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http import HTTPStatus
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server
from socketserver import ThreadingMixIn

from noema.application.pipeline import NoemaService
from noema.domain.normalization import application_identity
from noema.domain.activity import coerce_timestamp
from noema.infrastructure.sync import SyncEnvelope
from noema.infrastructure.browser.websocket import serve_websocket


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class NoemaApp:
    """Local-only HTTP surface around ``NoemaService``.

    Routes:

    * ``GET /health``
    * ``GET /api/events`` (``start``, ``end``, ``device``, ``domain``, ``limit``)
    * ``POST /api/ingest/activitywatch`` with a bucket or list of buckets
    """

    def __init__(self, service: NoemaService, daemon: Any = None):
        self.service = service
        self.daemon = daemon
        self._source_today_cache = None
        self._source_today_cached_at = 0.0

    def _response(self, start_response: Callable, status: HTTPStatus, payload: Any):
        body = _json_bytes(payload)
        start_response(
            "{} {}".format(status.value, status.phrase),
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Headers", "Content-Type"),
                ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ],
        )
        return [body]

    def _static_response(self, start_response: Callable, filename: str, content_type: str):
        """Serve the bundled local dashboard without exposing filesystem paths."""
        dashboard_dir = Path(__file__).resolve().parent / "dashboard"
        allowed = {"index.html": "text/html; charset=utf-8", "dashboard.css": "text/css; charset=utf-8", "dashboard.js": "application/javascript; charset=utf-8"}
        if filename not in allowed:
            return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        body = (dashboard_dir / filename).read_bytes()
        start_response(
            "200 OK",
            [("Content-Type", content_type), ("Content-Length", str(len(body))),
             ("Cache-Control", "no-store"), ("Access-Control-Allow-Origin", "*")],
        )
        return [body]

    def _frontend_response(self, start_response: Callable, path: str):
        """Serve the compiled React app from the local daemon when available."""
        repo_root = Path(__file__).resolve().parents[3]
        dist_dir = next(
            (
                candidate.resolve()
                for candidate in (repo_root / "web" / "dist",)
                if candidate.is_dir()
            ),
            None,
        )
        if dist_dir is None:
            return None
        relative = path.lstrip("/") or "index.html"
        candidate = (dist_dir / relative).resolve()
        try:
            candidate.relative_to(dist_dir)
        except ValueError:
            return None
        if not candidate.is_file():
            # Client-side navigation still belongs to the React entry point.
            candidate = dist_dir / "index.html"
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        start_response(
            "200 OK",
            [("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith(("text/", "application/javascript")) else "")),
             ("Content-Length", str(len(body))), ("Cache-Control", "no-cache"),
             ("Access-Control-Allow-Origin", "*")],
        )
        return [body]

    def _aw_query(self, query_lines: List[str], start: datetime, end: datetime) -> Any:
        """Run an ActivityWatch query via its POST /api/0/query/ endpoint."""
        import json as _json
        from urllib.request import Request as _Req
        from urllib.request import urlopen as _urlopen

        client = getattr(self.service.adapter, "client", None)
        if client is None:
            return None
        timeperiod = "{}/{}".format(start.isoformat(), end.isoformat())
        body = _json.dumps({"timeperiods": [timeperiod], "query": ["\n".join(query_lines)]}).encode("utf-8")
        req = _Req(
            client.base_url + "/api/0/query/",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with _urlopen(req, timeout=client.timeout) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    def _product_timezone(self) -> str:
        """Product timezone for calendar windows; daemon config, else default."""
        daemon = getattr(self, "daemon", None)
        config = getattr(daemon, "config", None)
        return str(getattr(config, "timezone_name", None) or "Asia/Kolkata")

    def _source_today(self) -> Dict[str, Any]:
        """Use the source query API with AFK intersection for exact live view.

        This matches the source's own Summary page by filtering window
        events against not-afk periods, eliminating double-counting.
        """
        if self._source_today_cache is not None and time.monotonic() - self._source_today_cached_at < 5:
            return self._source_today_cache
        client = getattr(self.service.adapter, "client", None)
        if client is None:
            return {"available": False, "reason": "telemetry source client is not configured"}
        # The dashboard is a local calendar-day view. Keep this explicit
        # instead of relying on the process timezone so the API and UI
        # agree on what "Today" means.
        local_zone = ZoneInfo(self._product_timezone())
        now = datetime.now(local_zone)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            # --- Top applications (merged by app) ---
            app_result = self._aw_query([
                'afk_events = query_bucket(find_bucket("aw-watcher-afk_"));',
                'window_events = query_bucket(find_bucket("aw-watcher-window_"));',
                'window_events = filter_period_intersect(window_events, filter_keyvals(afk_events, "status", ["not-afk"]));',
                'app_events = merge_events_by_keys(window_events, ["app"]);',
                'RETURN = sort_by_duration(app_events);',
            ], start, now)
            # --- Top titles (merged by app+title) ---
            title_result = self._aw_query([
                'afk_events = query_bucket(find_bucket("aw-watcher-afk_"));',
                'window_events = query_bucket(find_bucket("aw-watcher-window_"));',
                'window_events = filter_period_intersect(window_events, filter_keyvals(afk_events, "status", ["not-afk"]));',
                'title_events = merge_events_by_keys(window_events, ["app", "title"]);',
                'RETURN = sort_by_duration(title_events);',
            ], start, now)

            apps_raw = app_result[0] if app_result and isinstance(app_result, list) else []
            titles_raw = title_result[0] if title_result and isinstance(title_result, list) else []

            active_seconds = sum(
                max(0.0, float(e.get("duration", 0) or 0)) for e in apps_raw if isinstance(e, dict)
            )
            top_apps = []
            for e in apps_raw[:15]:
                if not isinstance(e, dict):
                    continue
                data = e.get("data", {}) or {}
                raw_name = str(data.get("app") or "Other")
                application_id, name = application_identity(raw_name)
                top_apps.append({"name": name, "application_id": application_id, "raw_name": raw_name, "seconds": round(max(0.0, float(e.get("duration", 0) or 0)), 1)})
            top_titles = []
            for e in titles_raw[:15]:
                if not isinstance(e, dict):
                    continue
                data = e.get("data", {}) or {}
                name = str(data.get("title") or "Unknown")
                app = str(data.get("app") or "")
                top_titles.append({"name": name, "app": app, "seconds": round(max(0.0, float(e.get("duration", 0) or 0)), 1)})

            latest = None
            try:
                buckets = client.list_buckets()
                items = buckets.items() if isinstance(buckets, dict) else []
                for bucket_id, bucket in items:
                    if not isinstance(bucket, dict) or bucket.get("type") not in {"currentwindow", "current_window"}:
                        continue
                    events = client.get_events(str(bucket_id), start=start, end=now)
                    if isinstance(events, dict):
                        events = events.get("events", [])
                    for event in (events or [])[:1]:
                        ts = event.get("timestamp") if isinstance(event, dict) else None
                        if ts:
                            latest = ts
            except Exception:
                pass

            payload = {
                "available": True,
                "date": start.date().isoformat(),
                "active_seconds": round(active_seconds, 1),
                "latest_event_at": latest,
                "top_applications": top_apps,
                "top_titles": top_titles,
            }
            self._source_today_cache = payload
            self._source_today_cached_at = time.monotonic()
            return payload
        except Exception as exc:
            return {"available": False, "reason": "ActivityWatch is unavailable: {}".format(exc)}

    @staticmethod
    def _reconcile_today_with_source(daily: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Keep calendar totals equal to the source while preserving AI labels.

        The local normalized timeline can lag while the source continues
        collecting. Any tracked interval without a correlated normalized event
        is explicitly neutral, never an uncategorized/unknown bucket.
        """

        if not snapshot.get("available"):
            return daily
        try:
            source_total = max(0.0, float(snapshot.get("active_seconds", 0) or 0))
            local_total = max(0.0, float(daily.get("total_tracked_seconds", 0) or 0))
        except (TypeError, ValueError):
            return daily
        gap = source_total - local_total
        if gap <= 0.5:
            daily["source_active_seconds"] = round(source_total, 1)
            daily["normalization_gap_seconds"] = 0.0
            return daily

        neutral = float(daily.get("neutral_seconds", daily.get("neutral_time", 0)) or 0) + gap
        daily["total_time"] = round(source_total, 1)
        daily["total_tracked_seconds"] = round(source_total, 1)
        daily["screen_on_time"] = round(source_total, 1)
        daily["neutral_time"] = round(neutral, 1)
        daily["neutral_seconds"] = round(neutral, 1)
        productive = float(daily.get("productive_seconds", daily.get("productive_time", 0)) or 0)
        percentage = productive / source_total * 100 if source_total else None
        daily["productivity_score"] = round(percentage) if percentage is not None else None
        daily["productivity_percentage"] = round(percentage, 1) if percentage is not None else None
        daily["source_active_seconds"] = round(source_total, 1)
        daily["normalization_gap_seconds"] = round(gap, 1)
        daily["normalization_note"] = "Source tracked time without a matching normalized event is counted as neutral until the local timeline catches up."

        local_categories = {
            str(item.get("name")): item.get("category", "neutral")
            for item in daily.get("applications", [])
        }
        applications = []
        for item in snapshot.get("top_applications", []):
            name = str(item.get("name") or "Other")
            applications.append({
                "name": name,
                "seconds": round(float(item.get("seconds", 0) or 0), 1),
                "category": local_categories.get(name, "neutral"),
            })
        if applications:
            daily["applications"] = applications
            daily["top_applications"] = applications[:10]
        return daily

    def _classification_status(self) -> Dict[str, Any]:
        """Single scheduler-truth payload for the Command Center.

        Combines daemon worker state (last/next run, in-progress count),
        stored coverage counts (classified/pending/failed over meaningful
        sessions), the provider/model that actually served last, and the
        active model's remaining quota. Everything derives from backend
        state; the frontend displays it without reinterpretation.
        """

        daemon = self.daemon
        config = daemon.config
        worker = daemon._workers.get("semantics")
        last_result = dict(getattr(worker, "last_result", None) or {})
        due = daemon._next_due.get("semantics", float("inf"))
        running = daemon.running and config.background_ai_enabled
        if not config.background_ai_enabled:
            status = "disabled"
        elif getattr(daemon, "_semantics_in_progress", 0):
            status = "running"
        elif getattr(worker, "status", "") == "error":
            status = "error"
        else:
            status = "idle"
        interval_minutes = round(config.classification_interval_seconds / 60.0, 1)
        next_run_at = None
        if running and due != float("inf"):
            wait = max(0.0, due - time.monotonic())
            next_run_at = (datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat().replace("+00:00", "Z")
        coverage = self.service.classification_coverage(
            classifier_version=config.classification_version,
            max_retries=config.classification_max_retries,
        )
        provider = getattr(getattr(self.service, "classifier", None), "provider", None)
        last_provider = getattr(provider, "last_provider", None) or getattr(provider, "name", None)
        last_model = getattr(provider, "last_model", None) or getattr(provider, "model", None)
        quota_table = self.service.provider_quotas()
        quota = {"model": last_model, "requestsUsed": None, "requestsLimit": None, "requestsRemaining": None}
        for row in quota_table.get("models", []):
            if last_model and row.get("model") != last_model:
                continue
            quota = {
                "model": row.get("model"),
                "requestsUsed": (row.get("rpd") or {}).get("used"),
                "requestsLimit": (row.get("rpd") or {}).get("limit"),
                "requestsRemaining": (row.get("rpd") or {}).get("left"),
            }
            break
        worker_error = getattr(worker, "last_error", None)
        try:
            presence = self.service.presence_snapshot()
        except (AttributeError, OSError, TypeError, ValueError):
            presence = {"state": "unknown", "last_input_at": None,
                        "idle_seconds": None, "stale": True}
        return {
            "enabled": bool(config.background_ai_enabled),
            "intervalMinutes": interval_minutes,
            "status": status,
            "presence": presence,
            "lastRunAt": (
                worker.last_success_at.isoformat().replace("+00:00", "Z")
                if getattr(worker, "last_success_at", None) else None
            ),
            "lastRunDurationSeconds": last_result.get("duration_seconds"),
            "nextRunAt": next_run_at,
            "processedLastRun": last_result.get("processed", 0),
            "failedLastRun": last_result.get("failed", 0),
            "lastError": worker_error,
            "pendingCount": coverage["pending"],
            "processingCount": int(getattr(daemon, "_semantics_in_progress", 0) or 0),
            "failedCount": coverage["failed"],
            "classifiedCount": coverage["classified"],
            "totalEligibleCount": coverage["total_eligible"],
            "coveragePercent": coverage["coverage_percent"],
            "latestClassificationAt": coverage["latest_classification_at"],
            "provider": last_provider,
            "model": last_model,
            "quota": quota,
        }

    @staticmethod
    def _dashboard_window(query: Dict[str, List[str]]) -> tuple[datetime, datetime, str, str]:
        timezone_name = query.get("timezone", ["Asia/Kolkata"])[0] or "Asia/Kolkata"
        try:
            zone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        requested = query.get("date", [None])[0]
        if requested:
            try:
                selected = datetime.strptime(requested, "%Y-%m-%d").replace(tzinfo=zone)
            except ValueError as exc:
                raise ValueError("date must use YYYY-MM-DD") from exc
        else:
            selected = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
        period = query.get("range", ["today"])[0].casefold()
        if period == "today":
            start = selected
            end = selected + timedelta(days=1)
        elif period == "yesterday":
            end = selected
            start = end - timedelta(days=1)
        elif period in {"7d", "7days"}:
            start = selected - timedelta(days=6)
            end = selected + timedelta(days=1)
        elif period in {"30d", "30days"}:
            start = selected - timedelta(days=29)
            end = selected + timedelta(days=1)
        elif period in {"24h", "24hours", "last24h"}:
            end = datetime.now(zone)
            start = end - timedelta(hours=24)
        else:
            raise ValueError("range must be today, yesterday, 7d, 30d, or 24h")
        return start, end, timezone_name, period

    @staticmethod
    def _json_body(environ: Dict[str, Any]) -> Any:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ.get("wsgi.input", BytesIO()).read(length)
        if not raw:
            raise ValueError("request body is required")
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _int_query(query: Dict[str, List[str]], name: str, default: int) -> int:
        try:
            value = int(query.get(name, [str(default)])[0])
        except ValueError as exc:
            raise ValueError("{} must be an integer".format(name)) from exc
        if value < 1:
            raise ValueError("{} must be positive".format(name))
        return value

    def __call__(self, environ: Dict[str, Any], start_response: Callable):
        """Time every request into telemetry, then dispatch unchanged."""
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        started = time.perf_counter()
        status_holder: Dict[str, Any] = {}
        error: Optional[str] = None

        def tracking_start_response(status: str, headers: Any, exc_info: Any = None) -> None:
            status_holder["status"] = status
            return start_response(status, headers, exc_info) if exc_info is not None else start_response(status, headers)

        try:
            return self._dispatch(environ, tracking_start_response)
        except Exception as exc:
            error = "{}: {}".format(type(exc).__name__, str(exc)[:200])
            raise
        finally:
            try:
                recorder = getattr(getattr(self, "service", None), "telemetry", None)
                if recorder is not None:
                    code = str(status_holder.get("status") or "")[:3]
                    recorder.record_operation(
                        "api_request", "{} {}".format(method, path),
                        duration_ms=round((time.perf_counter() - started) * 1000, 1),
                        success=error is None and code.startswith("2"),
                        error=error or (None if code.startswith("2") else "HTTP {}".format(code or "?")),
                    )
            except Exception:
                pass

    def _dispatch(self, environ: Dict[str, Any], start_response: Callable):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False)

        try:
            if method == "OPTIONS":
                return self._response(start_response, HTTPStatus.NO_CONTENT, {})

            if method == "GET" and (path == "/" or path in {"/dashboard", "/dashboard/"} or path.startswith("/assets/")):
                frontend = self._frontend_response(start_response, path)
                if frontend is not None:
                    return frontend

            if method == "GET" and path in {"/", "/dashboard", "/dashboard/"}:
                return self._static_response(start_response, "index.html", "text/html; charset=utf-8")
            if method == "GET" and path == "/dashboard.css":
                return self._static_response(start_response, "dashboard.css", "text/css; charset=utf-8")
            if method == "GET" and path == "/dashboard.js":
                return self._static_response(start_response, "dashboard.js", "application/javascript; charset=utf-8")

            if method == "GET" and path == "/health":
                return self._response(start_response, HTTPStatus.OK, {"status": "ok"})

            if method == "GET" and path in {"/api/daemon/health", "/daemon/health"}:
                if self.daemon is None:
                    return self._response(
                        start_response,
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"status": "unavailable", "reason": "daemon is not attached"},
                    )
                return self._response(start_response, HTTPStatus.OK, self.daemon.health_dict())

            if method == "GET" and path in {"/api/classification/status", "/classification/status"}:
                if self.daemon is None:
                    return self._response(
                        start_response,
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"status": "unavailable", "reason": "daemon is not attached"},
                    )
                return self._response(start_response, HTTPStatus.OK, self._classification_status())

            if method == "GET" and path in {"/api/dashboard/summary", "/dashboard/summary"}:
                start, end, timezone_name, period = self._dashboard_window(query)
                latest = self.service.store.latest_event_timestamp()
                daily = self.service.daily_summary(start.astimezone(timezone.utc), end.astimezone(timezone.utc), timezone_name)
                daily["range"] = period
                source_today = self._source_today()
                if period == "today":
                    daily = self._reconcile_today_with_source(daily, source_today)
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"counts": {
                        "events": self.service.store.count(),
                        "sessions": self.service.store.count_sessions(),
                        "meaningful_sessions": self.service.store.count_meaningful_sessions(),
                        "classifications": self.service.store.count_classifications(),
                    }, "latest_event_at": latest.isoformat().replace("+00:00", "Z") if latest else None, "daily": daily, "source_today": source_today},
                )

            if method == "GET" and path in {"/api/dashboard/current", "/dashboard/current"}:
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    self.service.current_activity(),
                )

            if method == "GET" and path in {"/api/dashboard/recent-activity", "/dashboard/recent-activity"}:
                start, end, timezone_name, period = self._dashboard_window(query)
                activities = self.service.recent_activity(
                    start.astimezone(timezone.utc),
                    end.astimezone(timezone.utc),
                    limit=self._int_query(query, "limit", 100),
                )
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"range": period, "timezone": timezone_name, "activities": activities},
                )

            if method == "GET" and path in {"/api/presence/current", "/presence/current"}:
                return self._response(
                    start_response, HTTPStatus.OK, self.service.presence_snapshot())

            if method == "GET" and path in {"/api/distraction/fast-check", "/distraction/fast-check"}:
                # Advisory only: read-only single call, never writes, never
                # touches the scheduler. Always 200 with an explicit payload.
                return self._response(
                    start_response, HTTPStatus.OK, self.service.fast_distraction_check())

            if method == "GET" and path in {"/api/dashboard/recent-classifications", "/dashboard/recent-classifications"}:
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"classifications": self.service.recent_classifications(
                        limit=self._int_query(query, "limit", 12))},
                )

            if method == "GET" and path in {"/api/debug/classifier", "/debug/classifier"}:
                debug = self.service.classifier_debug()
                if self.daemon is not None:
                    health = self.daemon.health_dict()
                    debug["ollama_available"] = bool(health.get("ollama_connected"))
                    debug["configured_model"] = health.get("config", {}).get("ollama_model") or debug.get("configured_model")
                else:
                    debug["ollama_available"] = False
                return self._response(start_response, HTTPStatus.OK, debug)

            if method == "GET" and path in {"/api/debug/quotas", "/debug/quotas"}:
                return self._response(start_response, HTTPStatus.OK, self.service.provider_quotas())

            if method == "GET" and path in {"/api/debug/reconciliation", "/debug/reconciliation"}:
                requested_application = query.get("application", [None])[0]
                if not requested_application:
                    raise ValueError("application is required")
                timezone_name = query.get("timezone", ["Asia/Kolkata"])[0] or "Asia/Kolkata"
                zone = ZoneInfo(timezone_name)
                selected = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
                start = coerce_timestamp(query.get("start", [selected.isoformat()])[0])
                end = coerce_timestamp(query.get("end", [datetime.now(zone).isoformat()])[0])
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    self.service.reconciliation_report(requested_application, start, end),
                )

            if method == "POST" and path in {"/api/daemon/reload", "/daemon/reload"}:
                if self.daemon is None:
                    return self._response(
                        start_response,
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"status": "unavailable", "reason": "daemon is not attached"},
                    )
                return self._response(start_response, HTTPStatus.OK, self.daemon.reload_config())

            if method == "POST" and path in {"/api/ai/run", "/ai/run"}:
                if self.daemon is None:
                    return self._response(start_response, HTTPStatus.SERVICE_UNAVAILABLE, {"error": "daemon is not attached"})
                return self._response(start_response, HTTPStatus.OK, self.daemon.run_ai_now())

            if method == "GET" and path in {"/api/realtime/status", "/realtime/status"}:
                return self._response(
                    start_response, HTTPStatus.OK, self.service.realtime_status())

            if method == "GET" and path in {"/api/detections", "/detections"}:
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"detections": self.service.query_detections(
                        session_id=query.get("session_id", [None])[0],
                        limit=self._int_query(query, "limit", 20))},
                )

            if method == "POST" and path in {"/api/realtime/evaluate", "/realtime/evaluate"}:
                # Manual single pass through the daemon worker lane (shares
                # the pipeline lock; returns the lane result, never a fake).
                if self.daemon is None:
                    return self._response(start_response, HTTPStatus.SERVICE_UNAVAILABLE, {"error": "daemon is not attached"})
                return self._response(start_response, HTTPStatus.OK, self.daemon.run_realtime_now())

            if method == "GET" and path in {"/api/metrics/summary", "/metrics/summary"}:
                bundle = self.service.metrics_bundle(
                    days=self._int_query(query, "days", 30), daemon=self.daemon)
                return self._response(start_response, HTTPStatus.OK, {
                    "window_days": bundle["window_days"],
                    "recording": bundle["recording"],
                    "runtime": bundle["runtime"],
                    "invocations": bundle["invocations"],
                    "requests": bundle["requests"],
                    "attempts": bundle["attempts"],
                    "success_rate": bundle["success_rate"],
                    "models": bundle["models"],
                    "tokens": bundle["tokens"]["windows"],
                    "efficiency": bundle["tokens"]["efficiency"],
                    "quota": bundle["quota"],
                    "cost": bundle["cost"],
                    "realtime": {
                        "evaluations": bundle["realtime"]["evaluations"],
                        "candidates": bundle["realtime"]["candidates"],
                        "verifications": bundle["realtime"]["verifications"],
                        "confirmed": bundle["realtime"]["confirmed"],
                        "interventions_triggered": bundle["realtime"]["interventions_triggered"],
                        "recovery_rate": bundle["realtime"]["recovery_rate"],
                    },
                    "workers": bundle["workers"],
                    "retention": bundle["retention"],
                })

            if method == "GET" and path in {"/api/metrics/models", "/metrics/models"}:
                bundle = self.service.metrics_bundle(
                    days=self._int_query(query, "days", 30), daemon=self.daemon)
                return self._response(start_response, HTTPStatus.OK, {
                    "window_days": bundle["window_days"],
                    "models": bundle["models"],
                })

            if method == "GET" and path in {"/api/metrics/providers", "/metrics/providers"}:
                bundle = self.service.metrics_bundle(
                    days=self._int_query(query, "days", 30), daemon=self.daemon)
                by_provider: Dict[str, Any] = {}
                for row in bundle["models"]:
                    entry = by_provider.setdefault(row["provider"], {
                        "provider": row["provider"], "models": [],
                        "requests": 0, "successful_requests": 0,
                        "failed_requests": 0, "retries": 0, "fallbacks": 0,
                    })
                    entry["models"].append(row["model"])
                    for key in ("requests", "successful_requests", "failed_requests",
                                "retries", "fallbacks"):
                        entry[key] += row.get(key) or 0
                providers = []
                for entry in sorted(by_provider.values(), key=lambda item: item["provider"]):
                    total = entry["requests"]
                    entry["success_rate"] = (entry["successful_requests"] / total) if total else None
                    providers.append(entry)
                return self._response(start_response, HTTPStatus.OK, {
                    "window_days": bundle["window_days"],
                    "providers": providers,
                    "quota": bundle["quota"],
                })

            if method == "GET" and path in {"/api/metrics/tokens", "/metrics/tokens"}:
                bundle = self.service.metrics_bundle(
                    days=self._int_query(query, "days", 30), daemon=self.daemon)
                return self._response(start_response, HTTPStatus.OK, {
                    "window_days": bundle["window_days"],
                    "tokens": bundle["tokens"],
                    "cost": bundle["cost"],
                })

            if method == "GET" and path in {"/api/metrics/latency", "/metrics/latency"}:
                bundle = self.service.metrics_bundle(
                    days=self._int_query(query, "days", 30), daemon=self.daemon)
                return self._response(start_response, HTTPStatus.OK, {
                    "window_days": bundle["window_days"],
                    "latency": bundle["latency"],
                })

            if method == "GET" and path in {"/api/metrics/realtime", "/metrics/realtime"}:
                bundle = self.service.metrics_bundle(
                    days=self._int_query(query, "days", 30), daemon=self.daemon)
                return self._response(start_response, HTTPStatus.OK, {
                    "window_days": bundle["window_days"],
                    "realtime": bundle["realtime"],
                })

            if method == "GET" and path in {"/api/metrics/workers", "/metrics/workers"}:
                bundle = self.service.metrics_bundle(
                    days=self._int_query(query, "days", 30), daemon=self.daemon)
                return self._response(start_response, HTTPStatus.OK, {
                    "window_days": bundle["window_days"],
                    "workers": bundle["workers"],
                })

            if method == "GET" and path in {"/api/benchmarks", "/benchmarks"}:
                repository = self.service.telemetry_repository()
                return self._response(start_response, HTTPStatus.OK, {
                    "runs": repository.list_benchmark_runs(
                        limit=self._int_query(query, "limit", 20)),
                })

            if method == "GET" and (path in {"/api/benchmarks/", "/benchmarks/"} or path.startswith("/api/benchmarks/") or path.startswith("/benchmarks/")):
                run_id = path.rstrip("/").rsplit("/", 1)[-1]
                if not run_id or run_id == "benchmarks":
                    return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
                repository = self.service.telemetry_repository()
                run = repository.get_benchmark_run(run_id)
                if run is None:
                    return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return self._response(start_response, HTTPStatus.OK, {"run": run})

            if method == "POST" and path in {"/api/ollama/stop", "/ollama/stop"}:
                if self.daemon is None:
                    return self._response(start_response, HTTPStatus.SERVICE_UNAVAILABLE, {"error": "daemon is not attached"})
                return self._response(start_response, HTTPStatus.OK, self.daemon.stop_ollama_model())

            if method == "GET" and path in {"/api/events", "/events"}:
                events = (self.service.store.query_recent_events(
                    limit=self._int_query(query, "limit", 1000)
                ) if query.get("sort", [""])[0].casefold() == "desc" else self.service.query_events(
                    start=query.get("start", [None])[0],
                    end=query.get("end", [None])[0],
                    device=query.get("device", [None])[0],
                    domain=query.get("domain", [None])[0],
                    limit=self._int_query(query, "limit", 1000),
                ))
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"events": self.service.events_with_classifications(events)},
                )

            if method == "GET" and path in {"/api/sessions", "/sessions"}:
                sessions = self.service.query_sessions(
                    start=query.get("start", [None])[0],
                    end=query.get("end", [None])[0],
                    device=query.get("device", [None])[0],
                    domain=query.get("domain", [None])[0],
                    limit=self._int_query(query, "limit", 1000),
                )
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"sessions": [session.to_dict() for session in sessions]},
                )

            if method == "GET" and path in {"/api/meaningful-sessions", "/meaningful-sessions"}:
                sessions = self.service.query_meaningful_sessions(
                    start=query.get("start", [None])[0], end=query.get("end", [None])[0],
                    project=query.get("project", [None])[0], intent_id=query.get("intent_id", [None])[0],
                    limit=self._int_query(query, "limit", 1000),
                )
                return self._response(start_response, HTTPStatus.OK, {"sessions": [item.to_dict() for item in sessions]})

            if method == "GET" and path in {"/api/classifications", "/classifications"}:
                limit = self._int_query(query, "limit", 1000)
                if "range" in query or "date" in query:
                    start, end, _, _ = self._dashboard_window(query)
                    classifications = self.service.query_classifications_for_window(
                        start.astimezone(timezone.utc), end.astimezone(timezone.utc),
                        category=query.get("category", [None])[0],
                        productivity=query.get("productivity", [None])[0],
                        limit=limit,
                    )
                else:
                    classifications = self.service.query_classifications(
                        session_id=query.get("session_id", [None])[0],
                        category=query.get("category", [None])[0],
                        productivity=query.get("productivity", [None])[0],
                        limit=limit,
                    )
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"classifications": [item.to_dict() for item in classifications]},
                )

            if method == "POST" and path in {"/api/feedback", "/feedback"}:
                payload = self._json_body(environ)
                if not isinstance(payload, dict) or not payload.get("session_id") or not payload.get("verdict"):
                    raise ValueError("session_id and verdict are required")
                try:
                    row = self.service.record_feedback(
                        payload["session_id"], payload["verdict"],
                        note=payload.get("note"),
                        source=payload.get("source") or "user",
                    )
                except ValueError as exc:
                    return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return self._response(start_response, HTTPStatus.CREATED, {"feedback": row})

            if method == "GET" and path in {"/api/feedback", "/feedback"}:
                session_id = query.get("session_id", [None])[0]
                if not session_id:
                    raise ValueError("session_id is required")
                return self._response(
                    start_response, HTTPStatus.OK,
                    {"feedback": self.service.query_feedback(session_id)},
                )

            if method == "GET" and path in {"/api/feedback/quality", "/feedback/quality"}:
                return self._response(
                    start_response, HTTPStatus.OK,
                    self.service.feedback_quality(query.get("model", [None])[0]),
                )

            if method == "GET" and path in {"/api/intents", "/intents"}:
                intents = self.service.query_intents(
                    limit=self._int_query(query, "limit", 1000)
                )
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"intents": [item.to_dict() for item in intents]},
                )

            if method == "GET" and path in {"/api/alignments", "/alignments"}:
                alignments = self.service.query_alignments(
                    intent_id=query.get("intent_id", [None])[0],
                    session_id=query.get("session_id", [None])[0],
                    limit=self._int_query(query, "limit", 1000),
                )
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"alignments": [item.to_dict() for item in alignments]},
                )

            if method == "GET" and path in {"/api/behavior", "/behavior"}:
                observations = self.service.query_behavior(
                    state=query.get("state", [None])[0],
                    actionable=(query.get("actionable", [None])[0].lower() == "true")
                    if query.get("actionable")
                    else None,
                    limit=self._int_query(query, "limit", 1000),
                )
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"observations": [item.to_dict() for item in observations]},
                )

            if method == "GET" and path in {"/api/interventions", "/interventions"}:
                interventions = self.service.query_interventions(
                    session_id=query.get("session_id", [None])[0],
                    status=query.get("status", [None])[0],
                    limit=self._int_query(query, "limit", 1000),
                )
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"interventions": [item.to_dict() for item in interventions]},
                )

            if method == "GET" and path in {"/api/intervention-actions", "/intervention-actions"}:
                actions = self.service.query_intervention_actions(
                    intervention_id=query.get("intervention_id", [None])[0],
                    limit=self._int_query(query, "limit", 1000),
                )
                return self._response(start_response, HTTPStatus.OK, {"actions": actions})

            if method == "GET" and path in {"/api/browser/registrations", "/browser/registrations"}:
                return self._response(
                    start_response, HTTPStatus.OK,
                    {"registrations": self.service.browser_registrations()},
                )

            if method == "GET" and path in {"/api/browser/category", "/browser/category"}:
                required = ("device", "browser", "window_id", "tab_id")
                if any(not query.get(name, [None])[0] for name in required):
                    raise ValueError("device, browser, window_id, and tab_id are required")
                result = self.service.categorize_browser_tab(
                    query["device"][0], query["browser"][0],
                    query["window_id"][0], query["tab_id"][0], classify=False,
                )
                return self._response(start_response, HTTPStatus.OK, result)

            if method == "POST" and path in {"/api/browser/category", "/browser/category"}:
                payload = self._json_body(environ)
                required = ("device", "browser", "window_id", "tab_id")
                if not isinstance(payload, dict) or any(not payload.get(name) for name in required):
                    raise ValueError("device, browser, window_id, and tab_id are required")
                result = self.service.categorize_browser_tab(
                    payload["device"], payload["browser"],
                    payload["window_id"], payload["tab_id"], classify=True,
                )
                return self._response(start_response, HTTPStatus.OK, result)

            if method == "GET" and path in {"/api/browser/poll", "/browser/poll"}:
                instance_id = query.get("extension_instance_id", [None])[0]
                if not instance_id:
                    raise ValueError("extension_instance_id is required")
                try:
                    timeout = min(25.0, max(0.0, float(query.get("timeout", ["0"])[0])))
                except ValueError as exc:
                    raise ValueError("timeout must be a number") from exc
                messages = self.service.poll_browser(
                    instance_id, timeout, self._int_query(query, "limit", 10)
                )
                return self._response(start_response, HTTPStatus.OK, {"messages": messages})

            if method == "GET" and path in {"/api/memes", "/memes"}:
                memes = self.service.query_memes(
                    session_id=query.get("session_id", [None])[0],
                    limit=self._int_query(query, "limit", 1000),
                )
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"memes": [item.to_dict() for item in memes]},
                )

            if method == "GET" and path in {"/api/outcomes", "/outcomes"}:
                outcomes = self.service.query_outcomes(
                    intervention_id=query.get("intervention_id", [None])[0],
                    recovery_status=query.get("recovery_status", [None])[0],
                    limit=self._int_query(query, "limit", 1000),
                )
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"outcomes": [item.to_dict() for item in outcomes]},
                )

            if method == "GET" and path in {"/api/memories", "/memories"}:
                memories = self.service.query_memories(
                    kind=query.get("kind", [None])[0],
                    limit=self._int_query(query, "limit", 1000),
                )
                return self._response(start_response, HTTPStatus.OK, {"memories": [item.to_dict() for item in memories]})

            if method == "GET" and path in {"/api/personalization", "/personalization"}:
                return self._response(start_response, HTTPStatus.OK, self.service.personalization_profile().to_dict())

            if method == "GET" and path in {"/api/timeline", "/timeline"}:
                timeline = self.service.unified_timeline(
                    start=query.get("start", [None])[0], end=query.get("end", [None])[0],
                    device=query.get("device", [None])[0], limit=self._int_query(query, "limit", 1000),
                )
                return self._response(start_response, HTTPStatus.OK, timeline.to_dict())

            if method == "POST" and path in {"/api/autonomous/cycle", "/autonomous/cycle"}:
                payload = self._json_body(environ) if environ.get("CONTENT_LENGTH") else {}
                cycle = self.service.run_autonomous_cycle(
                    start=payload.get("start") if isinstance(payload, dict) else None,
                    end=payload.get("end") if isinstance(payload, dict) else None,
                    intent_id=payload.get("intent_id") if isinstance(payload, dict) else None,
                )
                return self._response(start_response, HTTPStatus.OK, cycle.to_dict())

            if method == "POST" and path in {"/api/ingest/activitywatch", "/ingest"}:
                payload = self._json_body(environ)
                if isinstance(payload, dict) and "buckets" in payload:
                    result = self.service.ingest_buckets(payload["buckets"])
                elif isinstance(payload, dict) and "bucket" in payload:
                    result = self.service.ingest_bucket(payload["bucket"])
                else:
                    result = self.service.ingest_buckets(payload)
                return self._response(start_response, HTTPStatus.OK, result.to_dict())

            if method == "POST" and path in {"/api/browser/register", "/browser/register"}:
                payload = self._json_body(environ)
                if not isinstance(payload, dict):
                    raise ValueError("request body must be an object")
                registration = self.service.register_browser(
                    payload.get("browser"), payload.get("device_id"),
                    payload.get("extension_instance_id"),
                )
                return self._response(start_response, HTTPStatus.CREATED, registration)

            if method == "POST" and path in {"/api/browser/event", "/browser/event"}:
                payload = self._json_body(environ)
                if not isinstance(payload, dict) or not payload.get("extension_instance_id"):
                    raise ValueError("extension_instance_id is required")
                event = payload.get("event") or payload
                registration = self.service.report_browser_event(
                    payload["extension_instance_id"], event
                )
                return self._response(start_response, HTTPStatus.OK, registration)

            if method == "POST" and path in {"/api/browser/unregister", "/browser/unregister"}:
                payload = self._json_body(environ)
                if not isinstance(payload, dict) or not payload.get("extension_instance_id"):
                    raise ValueError("extension_instance_id is required")
                removed = self.service.browser_bridge.unregister(payload["extension_instance_id"])
                return self._response(start_response, HTTPStatus.OK, {"removed": removed})

            if method == "POST" and path in {"/api/meaningful-sessions/build", "/meaningful-sessions/build"}:
                payload = self._json_body(environ) if environ.get("CONTENT_LENGTH") else {}
                if not isinstance(payload, dict):
                    raise ValueError("request body must be an object")
                sessions = self.service.build_meaningful_sessions(
                    start=payload.get("start"), end=payload.get("end"), intent_id=payload.get("intent_id"),
                    limit=int(payload.get("limit", 1000)), sequence_terminated=bool(payload.get("sequence_terminated", True)),
                    summarize=bool(payload.get("summarize", False)),
                )
                return self._response(start_response, HTTPStatus.OK, {"sessions": [item.to_dict() for item in sessions]})

            if method == "POST" and path in {"/api/intents", "/intents"}:
                payload = self._json_body(environ)
                text = payload.get("text") if isinstance(payload, dict) else payload
                intent = self.service.capture_intent(text)
                return self._response(start_response, HTTPStatus.CREATED, intent.to_dict())

            if method == "POST" and path in {"/api/align", "/align"}:
                payload = self._json_body(environ)
                if not isinstance(payload, dict) or not payload.get("session_id") or not payload.get("intent_id"):
                    raise ValueError("session_id and intent_id are required")
                alignment = self.service.align_stored_session(
                    payload["session_id"], payload["intent_id"]
                )
                return self._response(start_response, HTTPStatus.OK, alignment.to_dict())

            if method == "POST" and path in {"/api/behavior/evaluate", "/behavior/evaluate"}:
                payload = self._json_body(environ) if environ.get("CONTENT_LENGTH") else {}
                observations = self.service.evaluate_stored_behavior(
                    intent_id=payload.get("intent_id") if isinstance(payload, dict) else None
                )
                return self._response(
                    start_response,
                    HTTPStatus.OK,
                    {"observations": [item.to_dict() for item in observations]},
                )

            if method == "POST" and path in {"/api/interventions/consider", "/interventions/consider"}:
                payload = self._json_body(environ)
                if not isinstance(payload, dict) or not payload.get("session_id"):
                    raise ValueError("session_id is required")
                intervention = self.service.consider_intervention(
                    payload["session_id"], payload.get("intent_id"), bool(payload.get("execute"))
                )
                return self._response(start_response, HTTPStatus.OK, intervention.to_dict())

            if method == "POST" and path.startswith("/api/interventions/") and path.endswith("/action"):
                intervention_id = path[len("/api/interventions/"):-len("/action")]
                payload = self._json_body(environ)
                if not isinstance(payload, dict) or not payload.get("action"):
                    raise ValueError("action is required")
                result = self.service.record_intervention_action(
                    intervention_id,
                    payload["action"],
                    state=payload.get("state") or "INTERACTED",
                    target=payload.get("target"),
                    metadata=payload.get("metadata"),
                )
                return self._response(start_response, HTTPStatus.OK, result)

            if method == "POST" and path.startswith("/api/interventions/") and path.endswith("/state"):
                intervention_id = path[len("/api/interventions/"):-len("/state")]
                payload = self._json_body(environ)
                if not isinstance(payload, dict) or not payload.get("state"):
                    raise ValueError("state is required")
                result = self.service.record_intervention_action(
                    intervention_id,
                    payload["state"].lower(),
                    state=payload["state"],
                    target=payload.get("target"),
                    metadata=payload.get("metadata"),
                )
                return self._response(start_response, HTTPStatus.OK, result)

            if method == "POST" and path in {"/api/memes/generate", "/memes/generate"}:
                payload = self._json_body(environ)
                if not isinstance(payload, dict) or not payload.get("session_id"):
                    raise ValueError("session_id is required")
                meme = self.service.generate_meme(payload["session_id"], payload.get("intent_id"))
                return self._response(start_response, HTTPStatus.OK, meme.to_dict())

            if method == "POST" and path in {"/api/outcomes/measure", "/outcomes/measure"}:
                payload = self._json_body(environ)
                if not isinstance(payload, dict) or not payload.get("intervention_id"):
                    raise ValueError("intervention_id is required")
                outcome = self.service.measure_outcome(
                    payload["intervention_id"], payload.get("now"), payload.get("meme_id")
                )
                return self._response(start_response, HTTPStatus.OK, outcome.to_dict())

            if method == "POST" and path in {"/api/memories/derive", "/memories/derive"}:
                memories = self.service.derive_memories()
                return self._response(start_response, HTTPStatus.OK, {"memories": [item.to_dict() for item in memories]})

            if method == "POST" and path in {"/api/sync/import", "/sync/import"}:
                payload = self._json_body(environ)
                envelope = SyncEnvelope.from_json(json.dumps(payload))
                result = self.service.import_sync(envelope)
                return self._response(start_response, HTTPStatus.OK, result.to_dict())

            return self._response(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return self._response(start_response, HTTPStatus.BAD_REQUEST, {"error": str(exc)[:200]})
        except (ConnectionError, TimeoutError, OSError) as exc:
            # Telemetry source unavailable (ActivityWatch down, network).
            # Honest 503, never a fake 200 and never a stack trace.
            return self._response(
                start_response, HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "source_unavailable"})
        except sqlite3.Error:
            return self._response(
                start_response, HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "database_unavailable"})
        except Exception:
            # Never leak stacks, paths, keys, or provider internals.
            return self._response(
                start_response, HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_error"})


def create_app(service: NoemaService, daemon: Any = None) -> NoemaApp:
    return NoemaApp(service, daemon=daemon)


def serve(
    service: NoemaService,
    host: str = "127.0.0.1",
    port: int = 8765,
    websocket_port: int = 8766,
    daemon: Any = None,
) -> None:
    """Run the local API and browser WebSocket bridge.

    Both listeners are loopback-only by default.  The WebSocket thread is
    best-effort; the extension can use the HTTP long-poll endpoint if its port
    is unavailable.  ActivityWatch is never started or modified here.
    """

    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    websocket_thread = threading.Thread(
        target=serve_websocket,
        args=(service.browser_bridge, host, websocket_port),
        daemon=True,
        name="noema-browser-websocket",
    )
    websocket_thread.start()
    with make_server(
        host, port, create_app(service, daemon=daemon), server_class=ThreadingWSGIServer,
        handler_class=WSGIRequestHandler,
    ) as server:
        if daemon is not None and hasattr(daemon, "add_shutdown_callback"):
            daemon.add_shutdown_callback(server.shutdown)
        try:
            server.serve_forever()
        finally:
            if daemon is not None:
                daemon.stop()
