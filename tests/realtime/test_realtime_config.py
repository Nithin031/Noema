"""Real-time + intervention configuration: defaults, env, validation."""

import os
from datetime import datetime, timezone

from noema.runtime import DaemonConfig


def test_realtime_and_policy_defaults():
    config = DaemonConfig()
    assert config.realtime_enabled is True
    assert config.realtime_eval_interval_seconds == 60.0
    assert config.fast_model_timeout_seconds == 20.0
    assert config.detector_enter_threshold == 0.70
    assert config.detector_exit_threshold == 0.50
    assert config.detector_min_active_seconds == 120.0
    assert config.detector_cooldown_seconds == 1800.0
    assert config.detector_recovery_seconds == 600.0
    assert config.intervention_mode == "NOTIFICATION"
    assert config.intervention_cooldown_seconds == 900.0
    assert config.intervention_require_actionable is True
    assert config.intervention_meme_cooldown_seconds == 3600.0
    assert config.intervention_holdout_cooldown_seconds == 300.0
    assert config.intervention_max_ineffective_streak == 3
    dumped = config.to_dict()
    assert dumped["realtime_enabled"] is True
    assert dumped["intervention_mode"] == "NOTIFICATION"


def test_realtime_env_overrides():
    previous = dict(os.environ)
    os.environ["NOEMA_REALTIME_ENABLED"] = "false"
    os.environ["NOEMA_REALTIME_EVAL_SECONDS"] = "30"
    os.environ["NOEMA_DETECTOR_ENTER"] = "0.8"
    os.environ["NOEMA_DETECTOR_EXIT"] = "0.4"
    os.environ["NOEMA_INTERVENTION_MODE"] = "meme"
    os.environ["NOEMA_INTERVENTION_COOLDOWN"] = "60"
    try:
        config = DaemonConfig.from_environment()
        assert config.realtime_enabled is False
        assert config.realtime_eval_interval_seconds == 30.0
        assert config.detector_enter_threshold == 0.8
        assert config.detector_exit_threshold == 0.4
        assert config.intervention_mode == "MEME"
        assert config.intervention_cooldown_seconds == 60.0
    finally:
        os.environ.clear()
        os.environ.update(previous)


def test_legacy_env_names_still_configure_with_canonical_winning():
    previous = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("NOEMA_") or key.startswith("AI_ACTIVITY_OS_"):
            os.environ.pop(key, None)
    os.environ["AI_ACTIVITY_OS_REALTIME_ENABLED"] = "false"
    os.environ["AI_ACTIVITY_OS_PROVIDER"] = "ollama"
    try:
        legacy = DaemonConfig.from_environment()
        assert legacy.realtime_enabled is False
        assert legacy.provider == "ollama"
        os.environ["NOEMA_REALTIME_ENABLED"] = "true"
        os.environ["NOEMA_PROVIDER"] = "hosted"
        canonical = DaemonConfig.from_environment()
        assert canonical.realtime_enabled is True
        assert canonical.provider == "hosted"
    finally:
        os.environ.clear()
        os.environ.update(previous)


def test_realtime_validation_rejects_bad_values():
    for kwargs in ({"detector_enter_threshold": 0.4, "detector_exit_threshold": 0.5},
                   {"detector_enter_threshold": 1.5},
                   {"realtime_eval_interval_seconds": 0},
                   {"intervention_mode": "TELEGRAM"},
                   {"intervention_cooldown_seconds": -1},
                   {"intervention_max_ineffective_streak": 0}):
        try:
            DaemonConfig(**kwargs)
        except ValueError:
            continue
        raise AssertionError("expected ValueError for {}".format(kwargs))


def test_telemetry_url_is_canonical_with_legacy_fallbacks():
    assert DaemonConfig().telemetry_url == "http://127.0.0.1:5600"
    legacy_json = DaemonConfig.from_mapping({"activitywatch_url": "http://127.0.0.1:5999"})
    assert legacy_json.telemetry_url == "http://127.0.0.1:5999"
    both = DaemonConfig.from_mapping({
        "activitywatch_url": "http://127.0.0.1:5999",
        "telemetry_url": "http://127.0.0.1:5600",
    })
    assert both.telemetry_url == "http://127.0.0.1:5600"
    assert "telemetry_url" in DaemonConfig().to_dict()
    assert "activitywatch_url" not in DaemonConfig().to_dict()


def test_legacy_env_and_flag_still_configure_telemetry_url():
    import os

    from noema.cli.daemon import build_parser

    previous = dict(os.environ)
    os.environ.pop("NOEMA_TELEMETRY_URL", None)
    os.environ.pop("AI_ACTIVITY_TELEMETRY_URL", None)
    os.environ["AI_ACTIVITY_WATCH_URL"] = "http://127.0.0.1:5999"
    try:
        assert DaemonConfig.from_environment().telemetry_url == "http://127.0.0.1:5999"
        os.environ["NOEMA_TELEMETRY_URL"] = "http://127.0.0.1:5600"
        assert DaemonConfig.from_environment().telemetry_url == "http://127.0.0.1:5600"
    finally:
        os.environ.clear()
        os.environ.update(previous)
    parsed = build_parser().parse_args(["--activitywatch-url", "http://127.0.0.1:5999"])
    assert parsed.telemetry_url == "http://127.0.0.1:5999"
    parsed = build_parser().parse_args(["--telemetry-url", "http://127.0.0.1:5600"])
    assert parsed.telemetry_url == "http://127.0.0.1:5600"


def test_timezone_name_defaults_validates_and_round_trips():
    assert DaemonConfig().timezone_name == "Asia/Kolkata"
    assert DaemonConfig(timezone_name="UTC").timezone_name == "UTC"
    assert DaemonConfig.from_mapping({"timezone_name": "UTC"}).timezone_name == "UTC"
    assert DaemonConfig().to_dict()["timezone_name"] == "Asia/Kolkata"
    try:
        DaemonConfig(timezone_name="Not/AZone")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for bad timezone")


def test_daily_report_dedup_survives_restart():
    from noema.api import NoemaService
    from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
    from noema.runtime import NoemaDaemon
    from noema.infrastructure.database import SQLiteStore

    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    config = DaemonConfig(db_path=":memory:", lock_path=":memory:")
    first = NoemaDaemon(service, config)
    emitted = first._daily_report_stage(datetime.now(timezone.utc))
    assert emitted["emitted"] is True
    restarted = NoemaDaemon(service, config)
    repeated = restarted._daily_report_stage(datetime.now(timezone.utc))
    assert repeated["emitted"] is False
    store.close()
