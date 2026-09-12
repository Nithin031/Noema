"""Restart-safe continuous orchestration for Noema."""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import signal
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from noema.domain.activity import coerce_timestamp
from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.domain.intervention import InterventionStatus

from noema.config.settings import DaemonConfig
from noema.runtime.lock import SingleInstanceLock
from noema.runtime.logging_utils import configure_logging, log_event


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat().replace("+00:00", "Z") if value else None


@dataclass
class WorkerHealth:
    name: str
    interval_seconds: float
    status: str = "waiting"
    run_count: int = 0
    error_count: int = 0
    last_started_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None
    last_error: Optional[str] = None
    last_result: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "status": self.status,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "last_started_at": _iso(self.last_started_at),
            "last_success_at": _iso(self.last_success_at),
            "last_error_at": _iso(self.last_error_at),
            "last_error": self.last_error,
            "last_result": dict(self.last_result),
        }


@dataclass
class DaemonHealth:
    status: str = "stopped"
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    config_reloaded_at: Optional[datetime] = None
    last_daily_report_at: Optional[datetime] = None
    restart_count: int = 0

    def to_dict(self, workers: Mapping[str, WorkerHealth], config: DaemonConfig) -> Dict[str, Any]:
        uptime = None
        if self.started_at and self.status == "running":
            uptime = max(0.0, (_utc_now() - self.started_at).total_seconds())
        return {
            "status": self.status,
            "pid": os.getpid(),
            "started_at": _iso(self.started_at),
            "stopped_at": _iso(self.stopped_at),
            "uptime_seconds": uptime,
            "config_reloaded_at": _iso(self.config_reloaded_at),
            "last_daily_report_at": _iso(self.last_daily_report_at),
            "restart_count": self.restart_count,
            "config": config.to_dict(),
            "workers": {name: worker.to_dict() for name, worker in workers.items()},
        }


class NoemaDaemon:
    """Run the existing service as a continuously supervised local process.

    ActivityWatch ingestion and semantic analysis are independent workers.
    Ingestion never waits for Ollama, while the semantic worker serializes its
    own jobs so two model calls cannot overlap. Maintenance workers retain a
    small scheduler for behavior, outcomes, and daily reporting.
    """

    def __init__(
        self,
        service: Any,
        config: Optional[DaemonConfig] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.service = service
        self.config = config or DaemonConfig()
        self.logger = logger or configure_logging(self.config.log_level)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ingest_thread: Optional[threading.Thread] = None
        self._semantic_thread: Optional[threading.Thread] = None
        self._health_lock = threading.RLock()
        self._pipeline_lock = threading.RLock()
        self._ingest_lock = threading.Lock()
        self._semantic_lock = threading.Lock()
        self._ollama_analysis_in_progress = False
        self._reload_lock = threading.RLock()
        self._lock = SingleInstanceLock(self.config.lock_path)
        self._registered_atexit = False
        self._config_mtime: Optional[float] = self._current_config_mtime()
        self._last_report_date: Optional[str] = None
        self._severe_activity = False
        self._ollama_probe_at = 0.0
        self._ollama_probe_ok: Optional[bool] = None
        self._initial_sync_pending = True
        self._ingest_cursor_at: Optional[datetime] = self._load_ingest_cursor()
        # Noema-native telemetry collectors (preferred production path).
        # Built lazily so unit tests can construct the daemon without OS
        # sensors; the ActivityWatch adapter path below is untouched.
        self._native_sources: Optional[Any] = None
        self._native_last_poll: float = 0.0
        self._native_connected: bool = False
        self._native_last_error: Optional[str] = None
        self._native_last_sources: List[Dict[str, Any]] = []
        self._health = DaemonHealth()
        self._semantics_in_progress = 0
        self.service.store.recover_processing_batches()
        self._shutdown_callbacks: List[Callable[[], Any]] = []
        self._shutdown_notified = False
        self._workers: Dict[str, WorkerHealth] = {
            "ingest": WorkerHealth("ingest", self.config.ingest_interval_seconds),
            "semantics": WorkerHealth("semantics", self.config.classification_interval_seconds),
            "behavior": WorkerHealth("behavior", self.config.behavior_interval_seconds),
            "outcomes": WorkerHealth("outcomes", self.config.outcome_interval_seconds),
            "daily_report": WorkerHealth("daily_report", self.config.report_interval_seconds),
            # Real-time detection is its own time scale: lightweight local
            # scoring on stored sessions, never merged into the 20-minute
            # semantic worker.
            "realtime": WorkerHealth("realtime", self.config.realtime_eval_interval_seconds),
        }
        if not self.config.background_ai_enabled:
            self._workers["semantics"].status = "disabled"
        if not self.config.realtime_enabled:
            self._workers["realtime"].status = "disabled"
        self._next_due: Dict[str, float] = {name: 0.0 for name in self._workers}

    @property
    def running(self) -> bool:
        with self._health_lock:
            return self._health.status == "running"

    def start(self) -> None:
        with self._health_lock:
            if self._health.status == "running":
                return
            self._lock.acquire()
            self._stop_event.clear()
            self._shutdown_notified = False
            self._health.status = "running"
            self._health.started_at = _utc_now()
            self._health.stopped_at = None
            self._health.restart_count += 1
            self._next_due = {name: 0.0 for name in self._workers}
            if self.config.background_ai_enabled:
                # First automatic pass runs seconds after startup so existing
                # pending work is picked up immediately (never later than one
                # interval); afterwards the regular cadence takes over.
                self._next_due["semantics"] = time.monotonic() + min(
                    15.0, self.config.classification_interval_seconds
                )
            else:
                self._next_due["semantics"] = float("inf")
            if not self._registered_atexit:
                atexit.register(self.stop)
                self._registered_atexit = True
            self._install_signal_handlers()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="noema-daemon",
                daemon=True,
            )
            self._ingest_thread = threading.Thread(
                target=self._run_ingest_loop,
                name="noema-ingestion",
                daemon=True,
            )
            self._semantic_thread = threading.Thread(
                target=self._run_semantic_loop,
                name="noema-semantics",
                daemon=True,
            )
            self._thread.start()
            self._ingest_thread.start()
            self._semantic_thread.start()
        log_event(self.logger, logging.INFO, "daemon_started", pid=os.getpid())

    def stop(self, timeout: float = 10.0) -> None:
        with self._health_lock:
            if self._health.status in {"stopped", "stopping"}:
                return
            self._health.status = "stopping"
            self._stop_event.set()
            threads = [self._thread, self._ingest_thread, self._semantic_thread]
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in threads:
            if thread and thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        with self._health_lock:
            self._health.status = "stopped"
            self._health.stopped_at = _utc_now()
            self._thread = None
            self._ingest_thread = None
            self._semantic_thread = None
            self._ollama_analysis_in_progress = False
            self._lock.release()
            collector, self._native_sources = self._native_sources, None
            callbacks = list(self._shutdown_callbacks)
            self._shutdown_callbacks = []
            self._shutdown_notified = True
        if collector is not None:
            try:
                collector.close()
            except Exception:
                pass
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log_event(self.logger, logging.ERROR, "shutdown_callback_failed", exc_info=True)
        log_event(self.logger, logging.INFO, "daemon_stopped", pid=os.getpid())

    def run_forever(self) -> None:
        """Own the daemon lifecycle until shutdown or a termination signal."""

        self.start()
        try:
            while self.running:
                self._stop_event.wait(1.0)
        finally:
            self.stop()

    def add_shutdown_callback(self, callback: Callable[[], Any]) -> None:
        """Register a callback used by an embedding server during shutdown."""

        with self._health_lock:
            if self._health.status == "stopped":
                return
            self._shutdown_callbacks.append(callback)

    def _install_signal_handlers(self) -> None:
        # Python only permits signal registration on the main thread.  The
        # daemon is also usable as an embedded component from a test/server.
        if threading.current_thread() is not threading.main_thread():
            return
        for name in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, name, None)
            if signum is not None:
                try:
                    signal.signal(signum, self._signal_stop)
                except (ValueError, OSError):
                    pass

    def _signal_stop(self, signum: int, frame: Any) -> None:
        log_event(self.logger, logging.INFO, "shutdown_signal", signal=signum)
        # serve_forever must be interrupted from another thread; calling its
        # shutdown method in the signal handler would deadlock the server.
        threading.Thread(
            target=self.stop,
            name="noema-shutdown",
            daemon=True,
        ).start()

    def _current_config_mtime(self) -> Optional[float]:
        if not self.config.config_path:
            return None
        try:
            return os.path.getmtime(self.config.config_path)
        except OSError:
            return None

    def reload_config(self) -> Dict[str, Any]:
        """Reload JSON config atomically and return its public snapshot."""

        if not self.config.config_path:
            return {"reloaded": False, "reason": "config_path is not configured", "config": self.config.to_dict()}
        with self._reload_lock:
            from noema.config.settings import DaemonConfig

            updated = DaemonConfig.from_file(self.config.config_path, base=self.config)
            background_changed = updated.background_ai_enabled != self.config.background_ai_enabled
            provider_settings = (
                "provider",
                "ollama_base_url",
                "ollama_model",
                "ollama_timeout_seconds",
                "ollama_enabled",
                "gemini_base_url",
                "gemini_model",
                "gemini_models",
                "gemini_api_key_env",
                "gemini_timeout_seconds",
                "openrouter_enabled",
                "openrouter_api_key_env",
                "openrouter_base_url",
                "openrouter_free_models",
                "openrouter_timeout_seconds",
                "openrouter_title",
            )
            if any(getattr(updated, name) != getattr(self.config, name) for name in provider_settings):
                # Provider instances own transport state and, for Gemini,
                # read credentials from the environment at construction or
                # request time. Keep a reload from showing one provider in
                # health while the service still executes another.
                return {
                    "reloaded": False,
                    "reason": "provider settings require daemon restart",
                    "config": self.config.to_dict(),
                }
            self.config = updated
            self.logger.setLevel(getattr(logging, self.config.log_level, logging.INFO))
            self._config_mtime = self._current_config_mtime()
            with self._health_lock:
                self._health.config_reloaded_at = _utc_now()
                for name, worker in self._workers.items():
                    worker.interval_seconds = self._interval_for(name)
                if background_changed:
                    self._next_due["semantics"] = (
                        time.monotonic() + self.config.classification_interval_seconds
                        if self.config.background_ai_enabled else float("inf")
                    )
            log_event(self.logger, logging.INFO, "config_reloaded", path=self.config.config_path)
            return {"reloaded": True, "config": self.config.to_dict()}

    def _maybe_reload_config(self) -> None:
        current = self._current_config_mtime()
        if current is not None and current != self._config_mtime:
            try:
                self.reload_config()
            except Exception:
                log_event(self.logger, logging.ERROR, "config_reload_failed", path=self.config.config_path, exc_info=True)

    def health_dict(self) -> Dict[str, Any]:
        with self._health_lock:
            payload = self._health.to_dict(self._workers, self.config)
            classifier = getattr(self.service, "classifier", None)
            provider = getattr(classifier, "provider", None)
            provider_name = getattr(provider, "name", self.config.provider)
            payload["provider"] = provider_name
            payload["provider_model"] = getattr(provider, "model", None)
            if provider_name == "hosted_chain":
                payload["provider_model"] = "hosted priority chain"
                provider_telemetry = provider.telemetry()
                payload["provider_chain"] = provider_telemetry.get("provider_chain", [])
                payload["provider_usage_today"] = provider_telemetry.get("usage_today", {})
                payload["gemini_configured"] = bool(
                    os.environ.get(self.config.gemini_api_key_env, "").strip()
                )
                payload["openrouter_configured"] = bool(
                    self.config.openrouter_enabled
                    and os.environ.get(self.config.openrouter_api_key_env, "").strip()
                )
            if provider_name == "ollama":
                now = time.monotonic()
                if now - self._ollama_probe_at >= 5.0 or self._ollama_probe_ok is None:
                    try:
                        request = Request(
                            self.config.ollama_base_url + "/api/tags",
                            headers={"Accept": "application/json"},
                            method="GET",
                        )
                        with urlopen(request, timeout=1.0):
                            self._ollama_probe_ok = True
                    except (OSError, TimeoutError):
                        self._ollama_probe_ok = False
                    self._ollama_probe_at = now
                if not self._ollama_probe_ok:
                    payload["provider_health"] = "unavailable"
                else:
                    payload["provider_health"] = "available"
            else:
                payload["provider_health"] = "configured"

            ingest_worker = self._workers.get("ingest")
            last_ingest_at = None
            last_activity_event = None
            try:
                stored_last_ingest = self.service.store.get_state("daemon.last_ingest_at")
                if stored_last_ingest:
                    last_ingest_at = coerce_timestamp(stored_last_ingest)
                recent = self.service.store.query_recent_events(limit=1)
                if recent:
                    last_activity_event = recent[0].event.end_timestamp
            except (AttributeError, OSError, TypeError, ValueError):
                pass
            if last_ingest_at is None and ingest_worker:
                last_ingest_at = ingest_worker.last_success_at
            freshness = None
            if last_activity_event is not None:
                freshness = round(max(0.0, (_utc_now() - last_activity_event).total_seconds()), 3)

            last_ollama_analysis = None
            try:
                stored_analysis = self.service.store.get_state("daemon.last_ollama_analysis_at")
                if stored_analysis:
                    last_ollama_analysis = coerce_timestamp(stored_analysis)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
            next_ollama_analysis = None
            if self.config.background_ai_enabled and self._health.status == "running":
                due = self._next_due.get("semantics", float("inf"))
                if due != float("inf"):
                    next_ollama_analysis = _utc_now() + timedelta(
                        seconds=max(0.0, due - time.monotonic())
                    )
            payload.update({
                "source_connected": bool(
                    (ingest_worker and ingest_worker.status in {"ok", "running"}
                     and ingest_worker.error_count == 0)
                    or self._native_connected
                ),
                "native_connected": bool(self._native_connected),
                "native_telemetry": {
                    "enabled": bool(self.config.native_enabled),
                    "connected": bool(self._native_connected),
                    "last_error": self._native_last_error,
                    "sources": list(self._native_last_sources),
                },
                "daemon_running": self._health.status == "running",
                "last_ingest_at": _iso(last_ingest_at),
                "last_activity_event": _iso(last_activity_event),
                "tracked_data_freshness": freshness,
                "ollama_connected": payload.get("provider_health") in {"available", "healthy"},
                "last_ollama_analysis": _iso(last_ollama_analysis),
                "next_ollama_analysis": _iso(next_ollama_analysis),
                "ollama_analysis_in_progress": self._ollama_analysis_in_progress,
                "last_ollama_job": self.service.classifier.last_job_telemetry()
                if hasattr(self.service.classifier, "last_job_telemetry") else None,
            })
            return payload

    def _interval_for(self, name: str) -> float:
        return {
            "ingest": self.config.ingest_interval_seconds,
            # Semantic analysis is deliberately a fixed completed-window
            # cadence. Severe behavior can affect policy/intervention, but it
            # must not turn Ollama into a heartbeat consumer.
            "semantics": self.config.classification_interval_seconds,
            "behavior": self.config.behavior_interval_seconds,
            "outcomes": self.config.outcome_interval_seconds,
            "daily_report": self.config.report_interval_seconds,
            "realtime": self.config.realtime_eval_interval_seconds,
        }[name]

    def _set_semantic_cadence(self, severe: bool) -> None:
        """Record severity without changing the independent AI cadence."""

        changed = bool(severe) != self._severe_activity
        self._severe_activity = bool(severe)
        with self._health_lock:
            self._workers["semantics"].interval_seconds = self._interval_for("semantics")
        if changed:
            log_event(
                self.logger,
                logging.INFO,
                "semantic_cadence_changed",
                severe=self._severe_activity,
                interval_seconds=self._interval_for("semantics"),
            )

    def _run_loop(self) -> None:
        maintenance_workers = ("behavior", "outcomes", "daily_report", "realtime")
        while not self._stop_event.is_set():
            self._maybe_reload_config()
            now = time.monotonic()
            ran = False
            for name in maintenance_workers:
                if now >= self._next_due[name]:
                    self._run_named_worker(name)
                    delay = self._worker_delay(name)
                    self._next_due[name] = time.monotonic() + delay
                    ran = True
            if not ran:
                self._stop_event.wait(0.25)

    def _worker_delay(self, name: str) -> float:
        worker = self._workers[name]
        delay = worker.interval_seconds
        if worker.error_count and worker.status == "error":
            exponent = min(worker.error_count - 1, 8)
            delay = min(
                self.config.retry_max_seconds,
                self.config.retry_base_seconds * (2 ** exponent),
            )
        return max(0.001, float(delay))

    def _run_ingest_loop(self) -> None:
        """Poll ActivityWatch without sharing a lock with Ollama."""

        while not self._stop_event.is_set():
            self._run_named_worker("ingest")
            delay = self._worker_delay("ingest")
            self._next_due["ingest"] = time.monotonic() + delay
            if self._stop_event.wait(delay):
                break

    def _run_semantic_loop(self) -> None:
        """Run one serialized completed-window semantic job per cadence."""

        while not self._stop_event.is_set():
            if not self.config.background_ai_enabled:
                with self._health_lock:
                    self._workers["semantics"].status = "disabled"
                if self._stop_event.wait(0.5):
                    break
                continue
            due = self._next_due.get("semantics", time.monotonic())
            if self._stop_event.wait(max(0.0, due - time.monotonic())):
                break
            ran = self._run_named_worker("semantics")
            # A manual request may own the semantic lock. Retry shortly after
            # it finishes instead of creating a concurrent model call.
            delay = self._worker_delay("semantics") if ran else 1.0
            self._next_due["semantics"] = time.monotonic() + delay

    def _run_named_worker(self, name: str) -> bool:
        worker = self._workers[name]
        if name == "semantics" and not self.config.background_ai_enabled:
            with self._health_lock:
                worker.status = "disabled"
            return False
        if name == "realtime" and not self.config.realtime_enabled:
            with self._health_lock:
                worker.status = "disabled"
            return False
        if name == "ingest":
            worker_lock = self._ingest_lock
        elif name == "semantics":
            worker_lock = self._semantic_lock
        else:
            worker_lock = self._pipeline_lock
        if not worker_lock.acquire(blocking=False):
            with self._health_lock:
                worker.status = "deferred"
            return False
        started = _utc_now()
        with self._health_lock:
            worker.status = "running"
            worker.run_count += 1
            worker.last_started_at = started
            if name == "semantics":
                self._ollama_analysis_in_progress = True
        try:
            stage_started = time.perf_counter()
            stage_error: Optional[str] = None
            try:
                result = {
                    "ingest": self._ingest_stage,
                    "semantics": self._semantics_stage,
                    "behavior": self._behavior_stage,
                    "outcomes": self._outcome_stage,
                    "daily_report": self._daily_report_stage,
                    "realtime": self._realtime_stage,
                }[name](started)
            except Exception as stage_exc:
                stage_error = "{}: {}".format(type(stage_exc).__name__, str(stage_exc)[:200])
                raise
            finally:
                # Reuse worker health for state; durations live in telemetry.
                recorder = getattr(getattr(self, "service", None), "telemetry", None)
                if recorder is not None:
                    try:
                        recorder.record_operation(
                            "worker_run", name,
                            duration_ms=round((time.perf_counter() - stage_started) * 1000, 1),
                            success=stage_error is None,
                            error=stage_error)
                    except Exception:
                        pass
            with self._health_lock:
                worker.status = "ok"
                worker.last_success_at = _utc_now()
                worker.last_error = None
                worker.last_result = result or {}
                worker.error_count = 0
            log_event(self.logger, logging.INFO, "worker_succeeded", worker=name, result=result or {})
            return True
        except Exception as exc:
            with self._health_lock:
                worker.status = "error"
                worker.error_count += 1
                worker.last_error_at = _utc_now()
                worker.last_error = "{}: {}".format(type(exc).__name__, exc)
            log_event(self.logger, logging.ERROR, "worker_failed", worker=name, error=str(exc), exc_info=True)
            return True
        finally:
            with self._health_lock:
                if name == "semantics":
                    self._ollama_analysis_in_progress = False
            worker_lock.release()

    def run_once(self, now: Optional[Any] = None, include_daily_report: bool = False) -> Dict[str, Any]:
        """Run the full ordered pipeline once; useful for smoke tests and jobs."""

        current = coerce_timestamp(now) if now is not None else _utc_now()
        with self._pipeline_lock:
            result = {
                "ingest": self._ingest_stage(current),
                "semantics": self._semantics_stage(current),
                "behavior": self._behavior_stage(current),
                "outcomes": self._outcome_stage(current),
            }
            if include_daily_report:
                result["daily_report"] = self._daily_report_stage(current)
        return result

    def run_ai_now(self) -> Dict[str, Any]:
        """Run one explicitly requested semantic pass without overlap."""

        current = _utc_now()
        with self._semantic_lock:
            with self._health_lock:
                self._ollama_analysis_in_progress = True
            try:
                result = self._semantics_stage(current)
            finally:
                with self._health_lock:
                    self._ollama_analysis_in_progress = False
        with self._health_lock:
            worker = self._workers["semantics"]
            worker.status = "ok" if self.config.background_ai_enabled else "manual"
            worker.run_count += 1
            worker.last_started_at = current
            worker.last_success_at = _utc_now()
            worker.last_result = result
            if self.config.background_ai_enabled:
                self._next_due["semantics"] = time.monotonic() + self.config.classification_interval_seconds
        return result

    def stop_ollama_model(self) -> Dict[str, Any]:
        """Unload the configured model so it releases laptop memory/VRAM."""

        executable = shutil.which("ollama")
        if executable is None:
            return {"stopped": False, "model": self.config.ollama_model,
                    "message": "ollama executable not found on PATH"}
        try:
            completed = subprocess.run(
                [executable, "stop", self.config.ollama_model],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, ValueError) as exc:
            return {"stopped": False, "model": self.config.ollama_model,
                    "message": "could not run ollama stop: {}".format(exc)}
        except Exception as exc:
            return {"stopped": False, "model": self.config.ollama_model,
                    "message": "{}: {}".format(type(exc).__name__, exc)}
        if completed.returncode != 0:
            return {"stopped": False, "model": self.config.ollama_model, "message": (completed.stderr or completed.stdout).strip()}
        return {"stopped": True, "model": self.config.ollama_model, "message": (completed.stdout or "model unloaded").strip()}

    def _window(self, now: datetime):
        return now - timedelta(seconds=self.config.lookback_seconds), now

    def _load_ingest_cursor(self) -> Optional[datetime]:
        try:
            stored = self.service.store.get_state("daemon.last_ingest_at")
            if stored:
                return coerce_timestamp(stored)
            return self.service.store.latest_event_timestamp()
        except (AttributeError, TypeError, ValueError, OSError):
            # Embedded or older store implementations can still run; they
            # simply start with the configured recovery lookback.
            return None

    def _incremental_window(self, now: datetime):
        # Once per local calendar day, backfill from midnight. This repairs a
        # daemon that was started late, restarted after a reset, or missed a
        # period while ActivityWatch continued collecting. Subsequent polls
        # return to the cheap cursor-plus-overlap window.
        local_now = now.astimezone(ZoneInfo(self.config.timezone_name))
        local_date = local_now.date().isoformat()
        try:
            backfilled_date = self.service.store.get_state("daemon.last_ingest_local_date")
        except (AttributeError, OSError):
            backfilled_date = None
        if self._initial_sync_pending or backfilled_date != local_date:
            local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            return local_start.astimezone(timezone.utc), now
        if self._ingest_cursor_at is None:
            start = now - timedelta(seconds=self.config.lookback_seconds)
        else:
            start = self._ingest_cursor_at - timedelta(
                seconds=self.config.incremental_overlap_seconds
            )
        # The same overlap is used for the ActivityWatch read and the local
        # session rebuild, preserving the boundary without doubling the read.
        return start, now

    def _native_collector_instance(self) -> Optional[Any]:
        """Lazily build the native collector set (None when disabled)."""
        if not self.config.native_enabled:
            return None
        if self._native_sources is None:
            from noema.infrastructure.activity_sources import (
                BrowserActivitySource,
                CompositeActivitySource,
                NativeAFKSource,
                NativeWindowsActivitySource,
            )

            bridge = getattr(self.service, "browser_bridge", None)
            snapshot_fn = None
            if bridge is not None and hasattr(bridge, "snapshot"):
                snapshot_fn = bridge.snapshot
            self._native_sources = CompositeActivitySource([
                NativeWindowsActivitySource(
                    heartbeat_seconds=self.config.native_heartbeat_seconds),
                NativeAFKSource(
                    timeout_seconds=self.config.afk_timeout_seconds,
                    heartbeat_seconds=self.config.native_presence_heartbeat_seconds),
                BrowserActivitySource(bridge_snapshot_fn=snapshot_fn),
            ])
        return self._native_sources

    def _ingest_native(self, now: datetime) -> Dict[str, Any]:
        """Poll native collectors and ingest their events independently.

        Never raises: every source failure is captured as an explicit
        diagnostic status. Native telemetry failing must not break the
        ActivityWatch path, and vice versa.
        """
        result: Dict[str, Any] = {
            "enabled": bool(self.config.native_enabled),
            "accepted": 0, "discarded": 0, "duplicates": 0, "updated": 0,
            "events": 0, "status": "disabled", "detail": "", "sources": [],
            "error": None,
        }
        collector = self._native_collector_instance()
        if collector is None:
            return result
        if (time.monotonic() - self._native_last_poll
                < max(0.0, float(self.config.native_poll_seconds))):
            result.update(status="throttled", detail="poll interval not elapsed",
                          sources=list(self._native_last_sources))
            return result
        self._native_last_poll = time.monotonic()
        try:
            poll = collector.poll(now)
        except Exception as exc:
            self._native_connected = False
            self._native_last_error = "{}: {}".format(type(exc).__name__, exc)
            log_event(self.logger, logging.ERROR, "native_source_failed",
                      error=self._native_last_error)
            result.update(status="error", error=self._native_last_error)
            return result
        try:
            health = collector.health()
            sources = health.get("sources", [])
        except Exception:
            sources = []
        self._native_last_sources = sources
        result["sources"] = sources
        result["status"] = poll.status
        result["detail"] = poll.detail
        if poll.events:
            try:
                ingested = self.service.ingest_events(poll.events)
            except (AttributeError, OSError, TypeError, ValueError) as exc:
                self._native_connected = False
                self._native_last_error = "{}: {}".format(type(exc).__name__, exc)
                log_event(self.logger, logging.ERROR, "native_ingest_failed",
                          error=self._native_last_error)
                result.update(status="error", error=self._native_last_error)
                return result
            result.update(accepted=ingested.accepted, discarded=ingested.discarded,
                          duplicates=ingested.duplicates,
                          updated=getattr(ingested, "updated", 0),
                          events=len(poll.events))
        ok = poll.status in {"ok", "degraded"} or bool(poll.events)
        self._native_connected = bool(ok)
        if not ok:
            self._native_last_error = poll.detail or poll.status
            if poll.status == "error":
                log_event(self.logger, logging.ERROR, "native_source_failed",
                          error=self._native_last_error)
        else:
            self._native_last_error = None
        return result

    def _ingest_stage(self, now: datetime) -> Dict[str, Any]:
        cursor_before = self._ingest_cursor_at
        start, end = self._incremental_window(now)
        aw_error: Optional[str] = None
        try:
            ingested = self.service.ingest_telemetry(start=start, end=end)
        except (ConnectionError, OSError, TimeoutError) as exc:
            # The legacy ActivityWatch source is optional: record the
            # outage explicitly and continue with native telemetry instead
            # of failing the whole ingest tick.
            aw_error = "{}: {}".format(type(exc).__name__, exc)
            log_event(self.logger, logging.WARNING, "aw_source_unavailable",
                      error=aw_error)
            from noema.application.pipeline import IngestionResult

            ingested = IngestionResult()
        native = self._ingest_native(now)
        native_healthy = bool(native.get("enabled")
                              and native.get("status") in {"ok", "degraded", "throttled"})
        if aw_error is not None and not native_healthy:
            # Nothing observed anywhere: preserve the historical failure
            # surfacing (worker error + backoff) instead of fake success.
            # A disabled or erroring native layer never masks the outage.
            raise ConnectionError(aw_error)
        # Rebuild today's sessions from the calendar boundary. A mutable AW
        # heartbeat may have started hours ago and only its duration changes;
        # rebuilding from the polling cursor would never revisit that start.
        local_start = now.astimezone(ZoneInfo(self.config.timezone_name)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)
        # Rebuild the current local day from midnight, but ONLY when stored
        # evidence actually changed. The ingestion cursor is intentionally
        # narrow, but a session rebuild cannot use that narrow window: doing
        # so leaves the older parts of a growing currentwindow heartbeat in
        # one persisted session and the newer parts in another. Rebuilding
        # thousands of sessions every second while nothing changed held the
        # store lock for minutes per day and stalled every dashboard read,
        # so duplicate-only polls skip the rebuild entirely.
        session_start = local_start
        aw_changed = bool(ingested.accepted or getattr(ingested, "updated", 0))
        native_changed = bool(native.get("accepted") or native.get("updated"))
        if self._initial_sync_pending or aw_changed or native_changed:
            raw_sessions = self.service.sessionize_stored_events(
                start=session_start, end=end, limit=max(self.config.query_limit, 100000)
            )
            meaningful = self.service.build_meaningful_sessions(
                start=session_start,
                end=end,
                limit=max(self.config.query_limit, 100000),
                sequence_terminated=False,
            )
            sessions_rebuilt = True
        else:
            raw_sessions = []
            meaningful = []
            sessions_rebuilt = False
        self._ingest_cursor_at = now
        self._initial_sync_pending = False
        try:
            self.service.store.set_state("daemon.last_ingest_at", now.isoformat())
            self.service.store.set_state(
                "daemon.last_ingest_local_date",
                now.astimezone(ZoneInfo(self.config.timezone_name)).date().isoformat(),
            )
        except AttributeError:
            pass
        ingest_payload = ingested.to_dict() if hasattr(ingested, "to_dict") else dict(ingested)
        ingest_payload["updated"] = getattr(ingested, "updated", 0)
        cycle = {
            "cursor_before": _iso(cursor_before),
            "query_start": _iso(start),
            "query_end": _iso(end),
            "cursor_after": _iso(now),
            "events_received": ingested.processed + int(native.get("events", 0) or 0),
            "events_new": ingested.accepted + int(native.get("accepted", 0) or 0),
            "events_updated": getattr(ingested, "updated", 0) + int(native.get("updated", 0) or 0),
            "events_duplicate": ingested.duplicates + int(native.get("duplicates", 0) or 0),
            "events_discarded": ingested.discarded + int(native.get("discarded", 0) or 0),
            "aw_error": aw_error,
            "native": {key: native.get(key) for key in (
                "enabled", "accepted", "discarded", "duplicates", "updated",
                "events", "status", "detail", "error")},
        }
        log_event(self.logger, logging.INFO, "ingestion_cycle", **cycle)
        return {
            "ingested": ingest_payload,
            "raw_sessions": len(raw_sessions),
            "raw_classifications": 0,
            "meaningful_sessions": len(meaningful),
            "sessions_rebuilt": sessions_rebuilt,
            **cycle,
        }

    def _current_intent(self) -> Optional[Any]:
        intents = self.service.store.query_intents(limit=1)
        return intents[0] if intents else None

    def _semantics_stage(self, now: datetime) -> Dict[str, Any]:
        # Semantic work consumes a completed window of stored evidence. It
        # never receives raw ActivityWatch heartbeats and it never runs from
        # the ingestion loop.
        last_analysis = None
        try:
            stored_last = self.service.store.get_state("daemon.last_ollama_analysis_at")
            if stored_last:
                last_analysis = coerce_timestamp(stored_last)
        except (AttributeError, OSError, TypeError, ValueError):
            last_analysis = None
        start = last_analysis or (now - timedelta(seconds=self.config.classification_interval_seconds))
        if start >= now:
            start = now - timedelta(seconds=self.config.classification_interval_seconds)
        end = now
        completed_end = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
        completed_start = completed_end - timedelta(minutes=5)
        batch_id = "batch_" + hashlib.sha256(
            (completed_start.isoformat() + "|" + completed_end.isoformat()).encode("utf-8")
        ).hexdigest()[:20]
        self.service.store.upsert_classification_batch(
            batch_id, completed_start, completed_end, {
                "start": _iso(start), "end": _iso(end),
                "session_ids": [], "activity_ids": [],
            }
        )
        claimed_batches = self.service.store.claim_classification_batches()

        # Presence transitions for this window (PART 59 logging). Logged at
        # most once per distinct transition thanks to the persisted cursor.
        try:
            timeline = self.service.build_presence_timeline(start, end, now=now)
            last_logged = self.service.store.get_state("presence.last_logged_transition")
            for stamp, state in timeline.transitions():
                if last_logged is None or stamp > last_logged:
                    log_event(self.logger, logging.INFO, "presence_transition",
                              state=state, at=stamp,
                              timeout_seconds=self.config.afk_timeout_seconds)
                    last_logged = stamp
            if last_logged is not None:
                self.service.store.set_state("presence.last_logged_transition", last_logged)
        except (AttributeError, OSError, TypeError, ValueError):
            pass

        meaningful = self.service.build_meaningful_sessions(
            start=start,
            end=end,
            limit=max(self.config.query_limit, 100000),
            sequence_terminated=False,
        )
        sessions = self.service.store.query_meaningful_sessions(
            start=start, end=end, limit=max(self.config.query_limit, 100000)
        )
        # The semantic cadence is a 20-minute job boundary that processes a
        # bounded chunk per run. Sessions outside the current window but
        # still eligible (the current-local-day backlog and anything older)
        # come from one unbounded scan: the backlog is a strict subset of
        # it, so a separate backlog query would only recompute the same
        # rows. Remainder is processed by later 20-minute jobs instead of
        # being stranded outside the next window.
        local_start = now.astimezone(ZoneInfo(self.config.timezone_name)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc)

        # Eligibility rules (missing, pending-due, retryable failed,
        # historic heuristic, version-stale) live in the service so the
        # scheduler, the status endpoint, and manual runs share one source
        # of truth. Successfully classified rows under the current version
        # and terminally failed rows are never re-queued: no quota waste.
        seen_ids: set = set()
        candidates = []
        for candidate in sorted(sessions, key=lambda item: item.start_time):
            if candidate.id not in seen_ids:
                seen_ids.add(candidate.id)
                candidates.append(candidate)
        try:
            older_sessions = self.service.store.query_meaningful_sessions(limit=100000)
        except (AttributeError, OSError, TypeError, ValueError):
            older_sessions = []
        for candidate in sorted(older_sessions, key=lambda item: item.start_time):
            if candidate.id not in seen_ids:
                seen_ids.add(candidate.id)
                candidates.append(candidate)
        pending = self.service.pending_classification_sessions(
            candidates,
            now=now,
            classifier_version=self.config.classification_version,
            max_retries=self.config.classification_max_retries,
        )
        total_pending = len(pending)
        # Process the pending backlog in bounded chunks, newest-first.
        # Each run takes at most classification_max_per_run so a giant
        # backlog can never wedge the worker into an hours-long run: fresh
        # activity classifies quickly in the UI while older remainder
        # continues next cadence. Individual failures keep retryable state
        # and never abort the chunk.
        pending.sort(key=lambda item: item.start_time, reverse=True)
        max_per_run = max(1, int(self.config.classification_max_per_run))
        chunk = pending[:max_per_run]
        log_event(
            self.logger, logging.INFO, "classification_cycle_started",
            pending=total_pending, chunk=len(chunk),
            interval_seconds=self.config.classification_interval_seconds,
            provider=getattr(getattr(getattr(self.service, "classifier", None), "provider", None), "name", None),
        )

        raw_sessions = self.service.store.query_sessions(
            start=local_start, end=end, limit=max(self.config.query_limit, 100000)
        )
        raw_by_id = {item.id: item for item in raw_sessions}
        if claimed_batches:
            self.service.store.update_classification_batch_payload(
                claimed_batches[0]["batch_id"],
                {
                    "start": _iso(start), "end": _iso(end),
                    "session_ids": [item.id for item in sessions],
                    "activity_ids": [item.id for item in raw_sessions],
                },
            )

        def context_for(batch: list) -> dict:
            batch_context = {}
            for meaningful_session in batch:
                batch_context[meaningful_session.id] = [
                    {
                        "application": raw_by_id[item_id].app,
                        "title": raw_by_id[item_id].title,
                        "domain": raw_by_id[item_id].domain,
                        "url": raw_by_id[item_id].url,
                        "duration_seconds": raw_by_id[item_id].duration,
                        "device": raw_by_id[item_id].device,
                        "browser": raw_by_id[item_id].browser,
                        "browser_window_id": raw_by_id[item_id].browser_window_id,
                        "browser_tab_id": raw_by_id[item_id].browser_tab_id,
                        "event_count": raw_by_id[item_id].event_count,
                    }
                    for item_id in meaningful_session.activity_session_ids
                    if item_id in raw_by_id
                ]
            return batch_context

        # Always emit telemetry for the semantic window, including an empty
        # window. This makes the cadence observable without invoking the
        # model per heartbeat or per dashboard refresh.
        batch_size = max(1, int(self.config.classification_batch_size))
        classifications: list = []
        ollama_jobs: list = []
        cycle_started_perf = time.perf_counter()
        self._semantics_in_progress = len(chunk)
        try:
            if not chunk:
                classifications = self.service.classify_meaningful_sessions(
                    [], context_by_session={},
                    classifier_version=self.config.classification_version,
                    max_retries=self.config.classification_max_retries,
                )
            else:
                for start_index in range(0, len(chunk), batch_size):
                    batch = chunk[start_index:start_index + batch_size]
                    classifications.extend(self.service.classify_meaningful_sessions(
                        batch,
                        context_by_session=context_for(batch),
                        classifier_version=self.config.classification_version,
                        max_retries=self.config.classification_max_retries,
                    ))
                    job = self.service.classifier.last_job_telemetry() \
                        if hasattr(self.service.classifier, "last_job_telemetry") else None
                    if job is not None:
                        ollama_jobs.append(job)
            for claimed_batch in claimed_batches:
                if any(item.classification_status != "classified" for item in classifications):
                    self.service.store.retry_classification_batch(
                        claimed_batch["batch_id"], "one or more classifications remain retryable"
                    )
                else:
                    self.service.store.complete_classification_batch(claimed_batch["batch_id"])
        except Exception as exc:
            for claimed_batch in claimed_batches:
                self.service.store.retry_classification_batch(claimed_batch["batch_id"], str(exc))
            raise
        finally:
            self._semantics_in_progress = 0
        ollama_telemetry = ollama_jobs[-1] if ollama_jobs else (
            self.service.classifier.last_job_telemetry()
            if hasattr(self.service.classifier, "last_job_telemetry") else None
        )
        intent = self._current_intent()
        aligned = 0
        if intent:
            existing_alignments = {
                item.session_id
                for item in self.service.store.query_alignments(intent_id=intent.id, limit=self.config.query_limit)
            }
            for session in sessions:
                if session.id not in existing_alignments:
                    self.service.align_meaningful_session(session, intent)
                    aligned += 1
        self.service.store.set_state("daemon.last_ollama_analysis_at", now.isoformat())
        failed = sum(1 for item in classifications if item.classification_status != "classified")
        cycle_seconds = round(time.perf_counter() - cycle_started_perf, 1)
        cycle_provider = (ollama_telemetry or {}).get("provider")
        cycle_model = (ollama_telemetry or {}).get("model")
        log_event(
            self.logger, logging.INFO, "classification_cycle_completed",
            processed=len(chunk), classified=len(chunk) - failed, failed=failed,
            duration_seconds=cycle_seconds, provider=cycle_provider, model=cycle_model,
            remaining_pending=total_pending - len(chunk),
            next_run_in_seconds=round(self.config.classification_interval_seconds, 1),
        )
        return {
            "window_start": _iso(start),
            "window_end": _iso(end),
            "semantic_snapshot_sessions": len(sessions),
            "raw_sessions": len(raw_sessions),
            "raw_classifications": 0,
            "meaningful_sessions": len(sessions),
            "meaningful_rebuilt": len(meaningful),
            "classifications": len(classifications),
            "processed": len(chunk),
            "failed": failed,
            "provider": cycle_provider,
            "model": cycle_model,
            "duration_seconds": cycle_seconds,
            "ollama_job": ollama_telemetry,
            "ollama_jobs": ollama_jobs,
            "batches": len(ollama_jobs),
            "total_pending": total_pending,
            "remaining_pending": total_pending - len(chunk),
            "alignments": aligned,
        }

    def _behavior_stage(self, now: datetime) -> Dict[str, Any]:
        start, end = self._window(now)
        intent = self._current_intent()
        observations = self.service.evaluate_stored_behavior(
            intent_id=intent.id if intent else None,
            start=start,
            end=end,
            limit=self.config.query_limit,
        )
        actionable = [item for item in observations if item.actionable]
        severe = any(
            item.actionable and item.state == BehaviorState.DISTRACTED
            for item in observations
        )
        self._set_semantic_cadence(severe)
        considered = 0
        if actionable:
            latest = sorted(actionable, key=lambda item: item.started_at)[-1]
            prior = self.service.query_interventions(session_id=latest.session_id, limit=20)
            already_handled = any(
                item.status in {InterventionStatus.PLANNED, InterventionStatus.EXECUTED}
                for item in prior
            )
            if not already_handled:
                self.service.consider_intervention(
                    latest.session_id,
                    intent_id=intent.id if intent else None,
                    execute=self.config.execute_interventions,
                )
                considered = 1
        return {
            "observations": len(observations),
            "actionable": len(actionable),
            "severe_activity": severe,
            "classification_interval_seconds": self._interval_for("semantics"),
            "interventions_considered": considered,
        }

    def _outcome_stage(self, now: datetime) -> Dict[str, Any]:
        interventions = self.service.query_interventions(status=InterventionStatus.EXECUTED.value, limit=self.config.query_limit)
        existing = {
            item.intervention_id: item
            for item in self.service.query_outcomes(limit=self.config.query_limit * 2)
        }
        measured = 0
        for intervention in interventions:
            outcome = existing.get(intervention.id)
            if outcome is None or str(outcome.recovery_status.value) == "PENDING":
                meme_id = None
                try:
                    memes = self.service.query_memes(
                        session_id=intervention.session_id, limit=1)
                    if memes:
                        meme_id = memes[0].id
                except (AttributeError, OSError, TypeError, ValueError):
                    meme_id = None
                self.service.measure_outcome(intervention.id, now=now, meme_id=meme_id)
                measured += 1
        return {"executed_interventions": len(interventions), "outcomes_measured": measured}

    def _realtime_stage(self, now: datetime) -> Dict[str, Any]:
        """One lightweight real-time evaluation (own cadence, own lock lane).

        Local feature math on stored sessions; a model call happens only
        when a fresh candidate is entered, and an intervention attempt only
        after a concerning verification plus existing behavior evidence.
        Never touches the semantic scheduler's state.
        """
        outcome = self.service.evaluate_realtime(
            now, execute_intervention=self.config.execute_interventions)
        return {
            "state": outcome.get("state"),
            "presence": outcome.get("presence"),
            "score": (outcome.get("decision") or {}).get("score"),
            "verification_concerning": (
                (outcome.get("verification") or {}).get("concerning")
                if outcome.get("verification") else None
            ),
            "intervention": outcome.get("intervention"),
            "detections_recorded": len(outcome.get("detections") or []),
        }

    def run_realtime_now(self) -> Dict[str, Any]:
        """Run one explicitly requested real-time pass without overlap."""

        current = _utc_now()
        with self._pipeline_lock:
            result = self._realtime_stage(current)
        with self._health_lock:
            worker = self._workers["realtime"]
            worker.status = "ok" if self.config.realtime_enabled else "manual"
            worker.run_count += 1
            worker.last_started_at = current
            worker.last_success_at = _utc_now()
            worker.last_result = result
        return result

    def _daily_report_stage(self, now: datetime) -> Dict[str, Any]:
        day = now.date().isoformat()
        # Restart-safe dedup: the emitted date persists in daemon_state, so
        # a restart cannot re-emit the same calendar day's report.
        try:
            last_report_date = self.service.store.get_state("daemon.last_report_date")
        except (AttributeError, OSError, TypeError, ValueError):
            last_report_date = None
        if self._last_report_date == day or last_report_date == day:
            self._last_report_date = day
            return {"emitted": False, "date": day}
        self._last_report_date = day
        try:
            self.service.store.set_state("daemon.last_report_date", day)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        with self._health_lock:
            self._health.last_daily_report_at = now
        summary = {
            "events": self.service.store.count(),
            "activity_sessions": self.service.store.count_sessions(),
            "meaningful_sessions": len(self.service.store.query_meaningful_sessions(limit=self.config.query_limit)),
            "interventions": len(self.service.query_interventions(limit=self.config.query_limit)),
            "outcomes": len(self.service.query_outcomes(limit=self.config.query_limit)),
        }
        log_event(self.logger, logging.INFO, "daily_report", date=day, **summary)
        return {"emitted": True, "date": day, **summary}
