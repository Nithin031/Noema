"""Configuration for the long-running Noema daemon."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo


def _positive(name: str, value: Any, minimum: float = 0.001) -> float:
    result = float(value)
    if result < minimum:
        raise ValueError("{} must be at least {}".format(name, minimum))
    return result


def _boolean(value: Any) -> bool:
    """Parse JSON booleans and environment-string booleans consistently."""

    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _default_db_path() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return str(Path(local_app_data) / "Noema" / "noema.sqlite3")
    return str(Path.cwd() / "noema.sqlite3")


def _model_list(value: Any, default: Any, name: str) -> List[str]:
    """Normalize a ranked model list from a list or comma-separated string."""
    if value is None:
        items = list(default)
    elif isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    else:
        try:
            items = [str(item).strip() for item in value]
        except TypeError as exc:
            raise ValueError("{} must be a list or comma-separated string".format(name)) from exc
    items = [item for item in items if item]
    if not items:
        raise ValueError("{} must name at least one model".format(name))
    return items


def load_dotenv(path: Any = None, override: bool = False) -> Dict[str, str]:
    """Load ``KEY=VALUE`` pairs from a ``.env`` file into ``os.environ``.

    This is a small stdlib-only reader (no third-party dependency).
    It supports blank lines, ``#`` comments, an optional ``export`` prefix,
    and single/double-quoted values. By default existing environment
    variables are left untouched, so explicitly exported variables still
    take precedence over the file. Returns the pairs read from the file.
    """

    env_path = Path(path).expanduser() if path is not None else Path.cwd() / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    loaded: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        if not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = value.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")
        elif "#" in value:
            # Strip trailing inline comments on unquoted values.
            value = value.split("#", 1)[0].strip()
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


@dataclass
class DaemonConfig:
    """Operational settings with safe local-only defaults.

    The daemon observes the desktop through Noema-native collectors (with
    the ActivityWatch adapter available as an optional compatibility
    source) and exposes its own API on loopback.  A
    JSON file can override these values and can be reloaded while the daemon
    is running through ``NoemaDaemon.reload_config`` or the local API.
    """

    # Canonical address of the legacy external telemetry source
    # (ActivityWatch adapter: optional compatibility path; native Noema
    # collectors are the primary path, see ``native_enabled`` below). The
    # historical ``activitywatch_url`` JSON key and ``AI_ACTIVITY_WATCH_URL``
    # variable are still accepted and mapped here, so existing files keep
    # working.
    telemetry_url: str = "http://127.0.0.1:5600"
    # Noema-native telemetry collectors (preferred production path). The
    # ActivityWatch adapter stays available as an optional compatibility
    # source; either source (or both) can feed the pipeline.
    native_enabled: bool = True
    native_poll_seconds: float = 1.0
    native_heartbeat_seconds: float = 5.0
    native_presence_heartbeat_seconds: float = 5.0
    provider: str = "hosted"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2:3b"
    # A cold local model load can take tens of seconds on a laptop. Keep this
    # bounded, but do not silently turn every first request into a heuristic.
    ollama_timeout_seconds: float = 60.0
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-3.5-flash"
    gemini_api_key_env: str = "GEMINI_API_KEY"
    gemini_timeout_seconds: float = 15.0
    # Ranked Gemini fallback models (comma-separated string also accepted).
    gemini_models: Any = None
    # OpenRouter primary tier (OpenRouter -> Gemini -> Ollama).
    openrouter_enabled: bool = True
    openrouter_api_key_env: str = "OPENROUTER_API_KEY"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_free_models: Any = None
    openrouter_timeout_seconds: float = 60.0
    openrouter_title: str = "Noema"
    # Local final fallback. False removes Ollama from the chain entirely.
    ollama_enabled: bool = True
    db_path: str = _default_db_path()
    lock_path: Optional[str] = None
    config_path: Optional[str] = None
    host: str = "127.0.0.1"
    port: int = 8765
    websocket_port: int = 8766
    # ActivityWatch is telemetry, so keep this loop independent and near
    # real-time. Semantic analysis never runs from this loop.
    lookback_seconds: float = 60.0
    incremental_overlap_seconds: float = 120.0
    ingest_interval_seconds: float = 1.0
    # Behavior evaluation is deterministic and independent of Ollama.
    behavior_interval_seconds: float = 5 * 60.0
    # Autonomous classification cadence. The semantic worker wakes on this
    # schedule, classifies everything pending, then sleeps again.
    classification_interval_seconds: float = 20 * 60.0
    severe_classification_interval_seconds: float = 5 * 60.0
    classification_batch_size: int = 20
    # User-presence detection (aw-watcher-afk style). Presence answers "was
    # the user interacting?" from input timing only; window content is never
    # presence evidence. AFK_TIMEOUT_SECONDS defaults to 3 minutes.
    afk_timeout_seconds: float = 180.0
    afk_poll_interval_seconds: float = 5.0
    # Optional media exception. No browser producer currently emits playback
    # state, so this is inert unless an extension sends explicit
    # audible/media_playing metadata. No audio detector is built.
    afk_media_exception: bool = False
    # Sessionizer merge thresholds, measured against live telemetry
    # (median event ~1s, ~70% under 5s, ~85% under 15s). Short events merge
    # into surrounding active runs; they are never deleted. Deliberately no
    # minimum-duration gate: a coherent active flow must merge whatever its
    # stops last, with longer detours separated downstream semantically.
    session_short_event_seconds: float = 5.0
    session_merge_gap_seconds: float = 15.0
    # Product timezone (IANA name) for quota days, daily summaries, and
    # calendar windows. Previously hardcoded to Asia/Kolkata everywhere.
    timezone_name: str = "Asia/Kolkata"
    # Failed attempts per session before it goes terminally failed (visible,
    # kept, never silently dropped). Retries back off 10/20/40/80/120 min.
    classification_max_retries: int = 5
    # Bump to re-queue successfully classified rows under a new policy.
    classification_version: str = "1"
    # Max sessions classified in a single semantic run. The pending backlog
    # is worked off newest-first in chunks of this size, so one giant
    # backlog can never wedge the worker into an hours-long run while fresh
    # activity waits. Remainder continues on the next cadence.
    classification_max_per_run: int = 60
    # The semantic worker is periodic and independent. Set this to false to
    # keep only manual ``Run Ollama now`` analysis.
    background_ai_enabled: bool = True
    # Real-time behavioral detection. A lightweight evaluation (rolling
    # windows + local scoring; a model call only when a candidate is
    # crossed) runs on this cadence, fully independent of the 20-minute
    # semantic scheduler. The two time scales must never be merged.
    realtime_enabled: bool = True
    realtime_eval_interval_seconds: float = 60.0
    fast_model_timeout_seconds: float = 20.0
    detector_enter_threshold: float = 0.70
    detector_exit_threshold: float = 0.50
    detector_min_active_seconds: float = 120.0
    detector_cooldown_seconds: float = 1800.0
    detector_recovery_seconds: float = 600.0
    # Intervention policy: mode, cooldowns, and ineffectiveness backoff.
    # MEME cools down slower than NOTIFICATION by design.
    intervention_mode: str = "NOTIFICATION"
    intervention_cooldown_seconds: float = 900.0
    intervention_require_actionable: bool = True
    intervention_meme_cooldown_seconds: float = 3600.0
    intervention_holdout_cooldown_seconds: float = 300.0
    intervention_max_ineffective_streak: int = 3
    outcome_interval_seconds: float = 5 * 60.0
    report_interval_seconds: float = 24 * 60 * 60.0
    query_limit: int = 1000
    execute_interventions: bool = True
    retry_base_seconds: float = 5.0
    retry_max_seconds: float = 5 * 60.0
    log_level: str = "INFO"
    # Observability retention (days). Applies ONLY to telemetry tables
    # (model invocations, operation timings, benchmark runs/results);
    # product activity/session/classification data is never pruned here.
    metrics_retention_days: int = 30

    def __post_init__(self) -> None:
        self.telemetry_url = str(self.telemetry_url).rstrip("/")
        self.native_enabled = _boolean(self.native_enabled)
        self.native_poll_seconds = _positive(
            "native_poll_seconds", self.native_poll_seconds
        )
        self.native_heartbeat_seconds = _positive(
            "native_heartbeat_seconds", self.native_heartbeat_seconds
        )
        self.native_presence_heartbeat_seconds = _positive(
            "native_presence_heartbeat_seconds",
            self.native_presence_heartbeat_seconds,
        )
        self.provider = str(self.provider or "hosted").strip().lower()
        if self.provider not in {"hosted", "ollama", "gemini"}:
            raise ValueError("provider must be hosted, ollama, or gemini")
        self.ollama_base_url = str(self.ollama_base_url).rstrip("/")
        self.ollama_model = str(self.ollama_model or "llama3.2:3b").strip()
        self.ollama_timeout_seconds = _positive(
            "ollama_timeout_seconds", self.ollama_timeout_seconds
        )
        self.gemini_base_url = str(self.gemini_base_url).rstrip("/")
        self.gemini_model = str(self.gemini_model or "gemini-3.5-flash").strip()
        self.gemini_api_key_env = str(self.gemini_api_key_env or "GEMINI_API_KEY").strip()
        if not self.gemini_api_key_env:
            raise ValueError("gemini_api_key_env cannot be empty")
        self.gemini_timeout_seconds = _positive(
            "gemini_timeout_seconds", self.gemini_timeout_seconds
        )
        from noema.infrastructure.providers import ProviderChain

        self.gemini_models = _model_list(
            self.gemini_models, ProviderChain.HOSTED_MODELS, "gemini_models"
        )
        self.openrouter_free_models = _model_list(
            self.openrouter_free_models, ProviderChain.OPENROUTER_MODELS,
            "openrouter_free_models",
        )
        self.openrouter_enabled = _boolean(self.openrouter_enabled)
        self.ollama_enabled = _boolean(self.ollama_enabled)
        self.openrouter_api_key_env = str(self.openrouter_api_key_env or "OPENROUTER_API_KEY").strip()
        if not self.openrouter_api_key_env:
            raise ValueError("openrouter_api_key_env cannot be empty")
        self.openrouter_base_url = str(self.openrouter_base_url).rstrip("/")
        self.openrouter_timeout_seconds = _positive(
            "openrouter_timeout_seconds", self.openrouter_timeout_seconds
        )
        self.openrouter_title = str(self.openrouter_title or "Noema").strip()
        self.db_path = str(Path(self.db_path).expanduser())
        if self.lock_path is None:
            self.lock_path = self.db_path + ".lock"
        else:
            self.lock_path = str(Path(self.lock_path).expanduser())
        if self.config_path is not None:
            self.config_path = str(Path(self.config_path).expanduser())
        self.host = str(self.host or "127.0.0.1")
        self.port = int(self.port)
        self.websocket_port = int(self.websocket_port)
        if not 1 <= self.port <= 65535 or not 1 <= self.websocket_port <= 65535:
            raise ValueError("HTTP and WebSocket ports must be between 1 and 65535")
        self.lookback_seconds = _positive("lookback_seconds", self.lookback_seconds)
        self.incremental_overlap_seconds = _positive(
            "incremental_overlap_seconds", self.incremental_overlap_seconds
        )
        self.ingest_interval_seconds = _positive("ingest_interval_seconds", self.ingest_interval_seconds)
        self.behavior_interval_seconds = _positive("behavior_interval_seconds", self.behavior_interval_seconds)
        self.classification_interval_seconds = _positive(
            "classification_interval_seconds", self.classification_interval_seconds
        )
        self.severe_classification_interval_seconds = _positive(
            "severe_classification_interval_seconds", self.severe_classification_interval_seconds
        )
        self.outcome_interval_seconds = _positive("outcome_interval_seconds", self.outcome_interval_seconds)
        self.report_interval_seconds = _positive("report_interval_seconds", self.report_interval_seconds)
        self.retry_base_seconds = _positive("retry_base_seconds", self.retry_base_seconds)
        self.retry_max_seconds = _positive("retry_max_seconds", self.retry_max_seconds)
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds cannot be lower than retry_base_seconds")
        self.query_limit = int(self.query_limit)
        if self.query_limit < 1:
            raise ValueError("query_limit must be positive")
        self.classification_batch_size = int(self.classification_batch_size)
        if self.classification_batch_size < 1:
            raise ValueError("classification_batch_size must be positive")
        self.classification_max_per_run = int(self.classification_max_per_run)
        if self.classification_max_per_run < 1:
            raise ValueError("classification_max_per_run must be positive")
        self.classification_max_retries = int(self.classification_max_retries)
        if self.classification_max_retries < 0:
            raise ValueError("classification_max_retries cannot be negative")
        self.classification_version = str(self.classification_version or "1").strip() or "1"
        self.afk_timeout_seconds = _positive("afk_timeout_seconds", self.afk_timeout_seconds)
        self.afk_poll_interval_seconds = _positive("afk_poll_interval_seconds", self.afk_poll_interval_seconds)
        self.afk_media_exception = _boolean(self.afk_media_exception)
        self.session_short_event_seconds = _positive("session_short_event_seconds", self.session_short_event_seconds)
        self.session_merge_gap_seconds = _positive("session_merge_gap_seconds", self.session_merge_gap_seconds)
        self.timezone_name = str(self.timezone_name or "Asia/Kolkata").strip()
        try:
            ZoneInfo(self.timezone_name)
        except Exception as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        self.background_ai_enabled = _boolean(self.background_ai_enabled)
        self.realtime_enabled = _boolean(self.realtime_enabled)
        self.realtime_eval_interval_seconds = _positive(
            "realtime_eval_interval_seconds", self.realtime_eval_interval_seconds)
        self.fast_model_timeout_seconds = _positive(
            "fast_model_timeout_seconds", self.fast_model_timeout_seconds)
        self.detector_enter_threshold = float(self.detector_enter_threshold)
        self.detector_exit_threshold = float(self.detector_exit_threshold)
        if not 0.0 < self.detector_exit_threshold < self.detector_enter_threshold < 1.0:
            raise ValueError("need 0 < detector exit < detector enter < 1")
        self.detector_min_active_seconds = _positive(
            "detector_min_active_seconds", self.detector_min_active_seconds,
            minimum=0.0)
        self.detector_cooldown_seconds = _positive(
            "detector_cooldown_seconds", self.detector_cooldown_seconds, minimum=0.0)
        self.detector_recovery_seconds = _positive(
            "detector_recovery_seconds", self.detector_recovery_seconds, minimum=0.0)
        self.intervention_mode = str(self.intervention_mode or "NOTIFICATION").strip().upper()
        if self.intervention_mode not in {"HOLDOUT", "NOTIFICATION", "MEME"}:
            raise ValueError("intervention_mode must be HOLDOUT, NOTIFICATION, or MEME")
        self.intervention_cooldown_seconds = _positive(
            "intervention_cooldown_seconds", self.intervention_cooldown_seconds, minimum=0.0)
        self.intervention_require_actionable = _boolean(self.intervention_require_actionable)
        self.intervention_meme_cooldown_seconds = _positive(
            "intervention_meme_cooldown_seconds", self.intervention_meme_cooldown_seconds,
            minimum=0.0)
        self.intervention_holdout_cooldown_seconds = _positive(
            "intervention_holdout_cooldown_seconds", self.intervention_holdout_cooldown_seconds,
            minimum=0.0)
        self.intervention_max_ineffective_streak = int(self.intervention_max_ineffective_streak)
        if self.intervention_max_ineffective_streak < 1:
            raise ValueError("intervention_max_ineffective_streak must be at least 1")
        self.execute_interventions = _boolean(self.execute_interventions)
        self.metrics_retention_days = int(self.metrics_retention_days)
        if self.metrics_retention_days < 1:
            raise ValueError("metrics_retention_days must be positive")
        self.log_level = str(self.log_level or "INFO").upper()

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any], base: Optional["DaemonConfig"] = None) -> "DaemonConfig":
        """Build config from JSON-like values, ignoring unknown keys."""

        names = set(cls.__dataclass_fields__.keys())
        selected = {key: value for key, value in values.items() if key in names}
        if "activitywatch_url" in values and "telemetry_url" not in selected:
            # Historical JSON key: accepted, canonical name wins on conflict.
            selected["telemetry_url"] = values["activitywatch_url"]
        if base is not None and "db_path" in selected and "lock_path" not in selected:
            # A database override should not accidentally keep the previous
            # database's lock file.
            selected["lock_path"] = None
        if base is not None:
            return replace(base, **selected)
        return cls(**selected)

    @classmethod
    def from_file(cls, path: Any, base: Optional["DaemonConfig"] = None) -> "DaemonConfig":
        config_path = Path(path).expanduser()
        with config_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, Mapping):
            raise ValueError("daemon config must be a JSON object")
        values = dict(payload)
        values["config_path"] = str(config_path)
        return cls.from_mapping(values, base=base)

    @classmethod
    def from_environment(cls, base: Optional["DaemonConfig"] = None) -> "DaemonConfig":
        """Apply a small, explicit environment override surface."""

        values: Dict[str, Any] = {}
        # Legacy names first so the canonical NOEMA_* name wins when both
        # are set. Only configuration loading understands legacy names.
        legacy_mapping = {
            "AI_ACTIVITY_WATCH_URL": "telemetry_url",
            "AI_ACTIVITY_OS_DB": "db_path",
            "AI_ACTIVITY_OS_CONFIG": "config_path",
            "AI_ACTIVITY_OS_HOST": "host",
            "AI_ACTIVITY_OS_PORT": "port",
            "AI_ACTIVITY_OS_WS_PORT": "websocket_port",
            "AI_ACTIVITY_OS_PROVIDER": "provider",
            "AI_ACTIVITY_OS_OLLAMA_MODEL": "ollama_model",
            "AI_ACTIVITY_OS_BACKGROUND_AI": "background_ai_enabled",
            "AI_ACTIVITY_OS_REALTIME_ENABLED": "realtime_enabled",
            "AI_ACTIVITY_OS_REALTIME_EVAL_SECONDS": "realtime_eval_interval_seconds",
            "AI_ACTIVITY_OS_FAST_MODEL_TIMEOUT_SECONDS": "fast_model_timeout_seconds",
            "AI_ACTIVITY_OS_DETECTOR_ENTER": "detector_enter_threshold",
            "AI_ACTIVITY_OS_DETECTOR_EXIT": "detector_exit_threshold",
            "AI_ACTIVITY_OS_DETECTOR_MIN_ACTIVE": "detector_min_active_seconds",
            "AI_ACTIVITY_OS_DETECTOR_COOLDOWN": "detector_cooldown_seconds",
            "AI_ACTIVITY_OS_DETECTOR_RECOVERY": "detector_recovery_seconds",
            "AI_ACTIVITY_OS_INTERVENTION_MODE": "intervention_mode",
            "AI_ACTIVITY_OS_INTERVENTION_COOLDOWN": "intervention_cooldown_seconds",
            "AI_ACTIVITY_OS_INTERVENTION_REQUIRE_ACTIONABLE": "intervention_require_actionable",
            "AI_ACTIVITY_OS_INTERVENTION_MEME_COOLDOWN": "intervention_meme_cooldown_seconds",
            "AI_ACTIVITY_OS_INTERVENTION_HOLDOUT_COOLDOWN": "intervention_holdout_cooldown_seconds",
            "AI_ACTIVITY_OS_INTERVENTION_MAX_STREAK": "intervention_max_ineffective_streak",
            "AI_ACTIVITY_OS_GEMINI_MODEL": "gemini_model",
            "AI_ACTIVITY_OS_GEMINI_API_KEY_ENV": "gemini_api_key_env",
            "AI_ACTIVITY_OS_GEMINI_MODELS": "gemini_models",
            "AI_ACTIVITY_OS_OPENROUTER_ENABLED": "openrouter_enabled",
            "AI_ACTIVITY_OS_OPENROUTER_API_KEY_ENV": "openrouter_api_key_env",
            "AI_ACTIVITY_OS_OPENROUTER_TIMEOUT_SECONDS": "openrouter_timeout_seconds",
            "AI_ACTIVITY_OS_OLLAMA_ENABLED": "ollama_enabled",
            "AI_ACTIVITY_OS_LOOKBACK_SECONDS": "lookback_seconds",
            "AI_ACTIVITY_OS_MAX_PER_RUN": "classification_max_per_run",
            "AI_ACTIVITY_OS_CLASSIFICATION_MAX_RETRIES": "classification_max_retries",
            "AI_ACTIVITY_OS_CLASSIFICATION_VERSION": "classification_version",
            "AI_ACTIVITY_OS_TIMEZONE": "timezone_name",
            "AI_ACTIVITY_OS_METRICS_RETENTION_DAYS": "metrics_retention_days",
            "AI_ACTIVITY_OS_CLASSIFICATION_INTERVAL_MINUTES": "classification_interval_minutes",
        }
        for environment_name, config_name in legacy_mapping.items():
            if os.environ.get(environment_name):
                if config_name == "classification_interval_minutes":
                    try:
                        values["classification_interval_seconds"] = float(os.environ[environment_name]) * 60.0
                    except (TypeError, ValueError) as exc:
                        raise ValueError("AI_ACTIVITY_OS_CLASSIFICATION_INTERVAL_MINUTES must be numeric") from exc
                else:
                    values[config_name] = os.environ[environment_name]
        mapping = {
            "NOEMA_TELEMETRY_URL": "telemetry_url",
            "NOEMA_NATIVE_ENABLED": "native_enabled",
            "NOEMA_NATIVE_POLL_SECONDS": "native_poll_seconds",
            "NOEMA_NATIVE_HEARTBEAT_SECONDS": "native_heartbeat_seconds",
            "NOEMA_NATIVE_PRESENCE_HEARTBEAT_SECONDS": "native_presence_heartbeat_seconds",
            "NOEMA_DB": "db_path",
            "NOEMA_CONFIG": "config_path",
            "NOEMA_HOST": "host",
            "NOEMA_PORT": "port",
            "NOEMA_WS_PORT": "websocket_port",
            "NOEMA_PROVIDER": "provider",
            "NOEMA_OLLAMA_MODEL": "ollama_model",
            "NOEMA_BACKGROUND_AI": "background_ai_enabled",
            "NOEMA_REALTIME_ENABLED": "realtime_enabled",
            "NOEMA_REALTIME_EVAL_SECONDS": "realtime_eval_interval_seconds",
            "NOEMA_FAST_MODEL_TIMEOUT_SECONDS": "fast_model_timeout_seconds",
            "NOEMA_DETECTOR_ENTER": "detector_enter_threshold",
            "NOEMA_DETECTOR_EXIT": "detector_exit_threshold",
            "NOEMA_DETECTOR_MIN_ACTIVE": "detector_min_active_seconds",
            "NOEMA_DETECTOR_COOLDOWN": "detector_cooldown_seconds",
            "NOEMA_DETECTOR_RECOVERY": "detector_recovery_seconds",
            "NOEMA_INTERVENTION_MODE": "intervention_mode",
            "NOEMA_INTERVENTION_COOLDOWN": "intervention_cooldown_seconds",
            "NOEMA_INTERVENTION_REQUIRE_ACTIONABLE": "intervention_require_actionable",
            "NOEMA_INTERVENTION_MEME_COOLDOWN": "intervention_meme_cooldown_seconds",
            "NOEMA_INTERVENTION_HOLDOUT_COOLDOWN": "intervention_holdout_cooldown_seconds",
            "NOEMA_INTERVENTION_MAX_STREAK": "intervention_max_ineffective_streak",
            "NOEMA_GEMINI_MODEL": "gemini_model",
            "NOEMA_GEMINI_API_KEY_ENV": "gemini_api_key_env",
            "NOEMA_GEMINI_MODELS": "gemini_models",
            "NOEMA_OPENROUTER_ENABLED": "openrouter_enabled",
            "NOEMA_OPENROUTER_API_KEY_ENV": "openrouter_api_key_env",
            "OPENROUTER_FREE_MODELS": "openrouter_free_models",
            "NOEMA_OPENROUTER_TIMEOUT_SECONDS": "openrouter_timeout_seconds",
            "NOEMA_OLLAMA_ENABLED": "ollama_enabled",
            "NOEMA_LOOKBACK_SECONDS": "lookback_seconds",
            "NOEMA_MAX_PER_RUN": "classification_max_per_run",
            "NOEMA_CLASSIFICATION_MAX_RETRIES": "classification_max_retries",
            "NOEMA_CLASSIFICATION_VERSION": "classification_version",
            "AFK_TIMEOUT_SECONDS": "afk_timeout_seconds",
            "AFK_POLL_INTERVAL_SECONDS": "afk_poll_interval_seconds",
            "AFK_MEDIA_EXCEPTION": "afk_media_exception",
            "SESSION_SHORT_EVENT_SECONDS": "session_short_event_seconds",
            "SESSION_MERGE_GAP_SECONDS": "session_merge_gap_seconds",
            "NOEMA_TIMEZONE": "timezone_name",
            "NOEMA_METRICS_RETENTION_DAYS": "metrics_retention_days",
        }
        for environment_name, config_name in mapping.items():
            if os.environ.get(environment_name):
                values[config_name] = os.environ[environment_name]
        minutes = os.environ.get("NOEMA_CLASSIFICATION_INTERVAL_MINUTES")
        if minutes:
            try:
                values["classification_interval_seconds"] = float(minutes) * 60.0
            except (TypeError, ValueError) as exc:
                raise ValueError("NOEMA_CLASSIFICATION_INTERVAL_MINUTES must be numeric") from exc
        return cls.from_mapping(values, base=base)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "telemetry_url": self.telemetry_url,
            "native_enabled": self.native_enabled,
            "native_poll_seconds": self.native_poll_seconds,
            "native_heartbeat_seconds": self.native_heartbeat_seconds,
            "native_presence_heartbeat_seconds": self.native_presence_heartbeat_seconds,
            "provider": self.provider,
            "ollama_base_url": self.ollama_base_url,
            "ollama_model": self.ollama_model,
            "ollama_timeout_seconds": self.ollama_timeout_seconds,
            "gemini_base_url": self.gemini_base_url,
            "gemini_model": self.gemini_model,
            "gemini_api_key_env": self.gemini_api_key_env,
            "gemini_timeout_seconds": self.gemini_timeout_seconds,
            "gemini_models": list(self.gemini_models),
            "openrouter_enabled": self.openrouter_enabled,
            "openrouter_api_key_env": self.openrouter_api_key_env,
            "openrouter_base_url": self.openrouter_base_url,
            "openrouter_free_models": list(self.openrouter_free_models),
            "openrouter_timeout_seconds": self.openrouter_timeout_seconds,
            "openrouter_title": self.openrouter_title,
            "ollama_enabled": self.ollama_enabled,
            "db_path": self.db_path,
            "lock_path": self.lock_path,
            "config_path": self.config_path,
            "host": self.host,
            "port": self.port,
            "websocket_port": self.websocket_port,
            "lookback_seconds": self.lookback_seconds,
            "incremental_overlap_seconds": self.incremental_overlap_seconds,
            "ingest_interval_seconds": self.ingest_interval_seconds,
            "behavior_interval_seconds": self.behavior_interval_seconds,
            "classification_interval_seconds": self.classification_interval_seconds,
            "severe_classification_interval_seconds": self.severe_classification_interval_seconds,
            "classification_batch_size": self.classification_batch_size,
            "classification_max_per_run": self.classification_max_per_run,
            "classification_max_retries": self.classification_max_retries,
            "classification_version": self.classification_version,
            "afk_timeout_seconds": self.afk_timeout_seconds,
            "afk_poll_interval_seconds": self.afk_poll_interval_seconds,
            "afk_media_exception": self.afk_media_exception,
            "session_short_event_seconds": self.session_short_event_seconds,
            "session_merge_gap_seconds": self.session_merge_gap_seconds,
            "timezone_name": self.timezone_name,
            "background_ai_enabled": self.background_ai_enabled,
            "realtime_enabled": self.realtime_enabled,
            "realtime_eval_interval_seconds": self.realtime_eval_interval_seconds,
            "fast_model_timeout_seconds": self.fast_model_timeout_seconds,
            "detector_enter_threshold": self.detector_enter_threshold,
            "detector_exit_threshold": self.detector_exit_threshold,
            "detector_min_active_seconds": self.detector_min_active_seconds,
            "detector_cooldown_seconds": self.detector_cooldown_seconds,
            "detector_recovery_seconds": self.detector_recovery_seconds,
            "intervention_mode": self.intervention_mode,
            "intervention_cooldown_seconds": self.intervention_cooldown_seconds,
            "intervention_require_actionable": self.intervention_require_actionable,
            "intervention_meme_cooldown_seconds": self.intervention_meme_cooldown_seconds,
            "intervention_holdout_cooldown_seconds": self.intervention_holdout_cooldown_seconds,
            "intervention_max_ineffective_streak": self.intervention_max_ineffective_streak,
            "outcome_interval_seconds": self.outcome_interval_seconds,
            "report_interval_seconds": self.report_interval_seconds,
            "query_limit": self.query_limit,
            "execute_interventions": self.execute_interventions,
            "retry_base_seconds": self.retry_base_seconds,
            "retry_max_seconds": self.retry_max_seconds,
            "metrics_retention_days": self.metrics_retention_days,
            "log_level": self.log_level,
        }
