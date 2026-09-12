"""Command-line entry point for the Noema daemon."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional, Sequence

from noema.api import NoemaService, serve
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter, ActivityWatchClient
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classifier
from noema.infrastructure.providers import FAST_DISTRACTION_MODEL, GeminiProvider, ProviderChain, OllamaProvider, OpenRouterProvider
from noema.domain.intent import IntentEngine
from noema.domain.intervention import (
    InterventionEngine,
    InterventionMode,
    InterventionPolicy,
)
from noema.domain.meme import MemeIntelligence
from noema.domain.meaningful import MeaningfulSessionSummarizer
from noema.observability.recorder import TelemetryRecorder
from noema.infrastructure.ollama import OllamaClient
from noema.application.realtime import (
    DetectionTracker,
    DetectorConfig,
    DistractionCandidateDetector,
    FastModelVerifier,
    VerificationConfig,
)

from noema.runtime.autostart import (
    install_task_scheduler,
    install_user_autostart,
    query_task_scheduler,
    uninstall_task_scheduler,
    uninstall_user_autostart,
)
from noema.config.settings import DaemonConfig, load_dotenv
from noema.runtime.daemon import NoemaDaemon
from noema.runtime.watchdog import run_watchdog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Noema local daemon")
    parser.add_argument("--config", help="JSON daemon config file")
    parser.add_argument(
        "--telemetry-url", "--activitywatch-url", dest="telemetry_url",
        help="telemetry source base URL (--activitywatch-url is a deprecated alias)",
    )
    parser.add_argument("--db")
    parser.add_argument("--lock")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--websocket-port", type=int)
    parser.add_argument("--log-level")
    parser.add_argument("--once", action="store_true", help="run one ordered pipeline cycle and exit")
    parser.add_argument("--install-autostart", action="store_true")
    parser.add_argument("--uninstall-autostart", action="store_true")
    parser.add_argument("--install-task-scheduler", action="store_true")
    parser.add_argument("--uninstall-task-scheduler", action="store_true")
    parser.add_argument("--query-task-scheduler", action="store_true")
    parser.add_argument(
        "--watchdog",
        action="store_true",
        help="supervise the daemon child and restart it after an unexpected exit",
    )
    return parser


def _config(args: argparse.Namespace) -> DaemonConfig:
    # The repo .env file is the single file-based settings source; load it
    # first so from_environment() below picks it up. Explicitly exported
    # environment variables still take precedence over the file.
    load_dotenv()
    config = DaemonConfig.from_environment()
    if args.config:
        config = DaemonConfig.from_file(args.config, base=config)
    overrides = {
        "telemetry_url": args.telemetry_url,
        "db_path": args.db,
        "lock_path": args.lock,
        "host": args.host,
        "port": args.port,
        "websocket_port": args.websocket_port,
        "log_level": args.log_level,
    }
    selected = {key: value for key, value in overrides.items() if value is not None}
    if args.db and not args.lock:
        selected["lock_path"] = None
    return replace(config, **selected)


def _autostart_command(args: argparse.Namespace) -> str:
    command = '"{}" -m noema'.format(sys.executable)
    command += " --watchdog"
    if args.config:
        command += ' --config "{}"'.format(str(Path(args.config).expanduser().resolve()))
    if args.telemetry_url:
        command += ' --telemetry-url "{}"'.format(args.telemetry_url)
    if args.db:
        command += ' --db "{}"'.format(str(Path(args.db).expanduser().resolve()))
    if args.lock:
        command += ' --lock "{}"'.format(str(Path(args.lock).expanduser().resolve()))
    if args.host:
        command += ' --host "{}"'.format(args.host)
    if args.port:
        command += " --port {}".format(args.port)
    if args.websocket_port:
        command += " --websocket-port {}".format(args.websocket_port)
    if args.log_level:
        command += ' --log-level "{}"'.format(args.log_level)
    return command


def _daemon_arguments(args: argparse.Namespace, watchdog: bool = False) -> list:
    arguments = ["-m", "noema"]
    if watchdog:
        arguments.append("--watchdog")
    if args.config:
        arguments += ["--config", str(Path(args.config).expanduser().resolve())]
    if args.telemetry_url:
        arguments += ["--telemetry-url", args.telemetry_url]
    if args.db:
        arguments += ["--db", str(Path(args.db).expanduser().resolve())]
    if args.lock:
        arguments += ["--lock", str(Path(args.lock).expanduser().resolve())]
    if args.host:
        arguments += ["--host", args.host]
    if args.port:
        arguments += ["--port", str(args.port)]
    if args.websocket_port:
        arguments += ["--websocket-port", str(args.websocket_port)]
    if args.log_level:
        arguments += ["--log-level", args.log_level]
    return arguments


def _openrouter_providers(config: DaemonConfig) -> list:
    """Build the OpenRouter tier, or nothing when it cannot serve.

    A missing API key disables the tier quietly (TEST 14): the chain falls
    through to Gemini/Ollama and the application keeps working. The key
    itself is only ever read from the environment at request time.
    """
    if not config.openrouter_enabled:
        return []
    if not os.environ.get(config.openrouter_api_key_env, "").strip():
        return []
    return [
        OpenRouterProvider(
            model=model,
            api_key_env=config.openrouter_api_key_env,
            base_url=config.openrouter_base_url,
            timeout=config.openrouter_timeout_seconds,
            title=config.openrouter_title,
        )
        for model in config.openrouter_free_models
    ]


def _classifier(config: DaemonConfig) -> Classifier:
    ollama = OllamaProvider(
        OllamaClient(
            base_url=config.ollama_base_url,
            model=config.ollama_model,
            timeout=config.ollama_timeout_seconds,
        )
    ) if config.ollama_enabled else None
    hosted = [
        GeminiProvider(
            model=model,
            api_key_env=config.gemini_api_key_env,
            base_url=config.gemini_base_url,
            timeout=config.gemini_timeout_seconds,
        )
        for model in config.gemini_models
    ]
    openrouter = _openrouter_providers(config)
    # Persisted quota ledger lives next to the database so daily
    # request/token counts survive daemon restarts.
    usage_path = str(Path(config.db_path).expanduser().with_suffix(".usage.json"))
    if config.provider == "gemini":
        # Hosted-only mode: the local model is never consulted, so every
        # served classification in the UI carries hosted provenance and
        # failures stay pending instead of falling back.
        provider = ProviderChain(hosted=hosted, ollama=ollama,
                                       usage_path=usage_path, include_ollama=False,
                                       day_timezone=config.timezone_name,
                                       openrouter=openrouter)
    elif config.provider == "ollama":
        provider = ollama or OllamaProvider()
    else:
        provider = ProviderChain(hosted=hosted, ollama=ollama,
                                       usage_path=usage_path,
                                       include_ollama=ollama is not None,
                                       day_timezone=config.timezone_name,
                                       openrouter=openrouter)
    return Classifier(provider=provider)


def _intervention_engine(config: DaemonConfig) -> InterventionEngine:
    """Build the intervention policy from config (no code edits needed)."""
    return InterventionEngine(InterventionPolicy(
        enabled=True,
        mode=InterventionMode(config.intervention_mode),
        cooldown_seconds=config.intervention_cooldown_seconds,
        require_actionable=config.intervention_require_actionable,
        mode_cooldowns={
            "MEME": config.intervention_meme_cooldown_seconds,
            "NOTIFICATION": config.intervention_cooldown_seconds,
            "HOLDOUT": config.intervention_holdout_cooldown_seconds,
        },
        max_ineffective_streak=config.intervention_max_ineffective_streak,
    ))


def _realtime_setup(service: NoemaService, config: DaemonConfig,
                    classifier: Classifier) -> None:
    """Wire the real-time time scale: detector, tracker, fast verifier.

    The verifier reuses the existing provider abstraction on a separate
    fast path (fast OpenRouter model → first Gemini model → Ollama tail).
    Quota state is shared with the normal chain through the chain's
    rate-limit table + persisted ledger, keyed by model name.
    """
    service.realtime_detector = DistractionCandidateDetector(DetectorConfig(
        enter_threshold=config.detector_enter_threshold,
        exit_threshold=config.detector_exit_threshold,
        min_active_seconds=config.detector_min_active_seconds,
        cooldown_seconds=config.detector_cooldown_seconds,
        recovery_seconds=config.detector_recovery_seconds,
    ))
    service.detection_tracker = DetectionTracker(
        config=service.realtime_detector.config)
    chain = getattr(classifier, "provider", None)
    fast_openrouter = None
    if config.openrouter_enabled:
        if os.environ.get(config.openrouter_api_key_env, "").strip():
            fast_openrouter = OpenRouterProvider(
                model=FAST_DISTRACTION_MODEL,
                api_key_env=config.openrouter_api_key_env,
                base_url=config.openrouter_base_url,
                timeout=config.fast_model_timeout_seconds,
                title=config.openrouter_title,
            )
    fast_gemini = None
    try:
        first_gemini = (config.gemini_models or [None])[0]
    except (AttributeError, TypeError):
        first_gemini = None
    if first_gemini:
        fast_gemini = GeminiProvider(
            model=first_gemini,
            api_key_env=config.gemini_api_key_env,
            base_url=config.gemini_base_url,
            timeout=config.fast_model_timeout_seconds,
        )
    service.fast_verifier = FastModelVerifier(
        chain=chain if isinstance(chain, ProviderChain) else None,
        fast_openrouter=fast_openrouter,
        fast_gemini=fast_gemini,
        config=VerificationConfig(timeout_seconds=config.fast_model_timeout_seconds),
    )


def _local_ollama_client(config: DaemonConfig) -> OllamaClient:
    """Use one configured local Ollama model for every generative feature."""
    return OllamaClient(
        base_url=config.ollama_base_url,
        model=config.ollama_model,
        timeout=config.ollama_timeout_seconds,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] in ("benchmark", "metrics", "telemetry"):
        # Benchmark/metrics/telemetry subcommands share config loading with
        # the daemon but never start it. Dispatched before flag parsing so
        # the daemon parser stays untouched.
        if raw[0] == "telemetry":
            from noema.cli.telemetry import main_telemetry

            return main_telemetry(raw[1:])
        from noema.cli.benchmark import main_benchmark, main_metrics

        handler = main_benchmark if raw[0] == "benchmark" else main_metrics
        return handler(raw[1:])
    args = build_parser().parse_args(argv)
    if args.install_autostart:
        print(install_user_autostart(
            command=_autostart_command(args),
            working_directory=str(Path.cwd()),
        ))
        return 0
    if args.uninstall_autostart:
        print("removed" if uninstall_user_autostart() else "not installed")
        return 0
    if args.install_task_scheduler:
        print(install_task_scheduler(
            executable=sys.executable,
            arguments=_daemon_arguments(args, watchdog=True),
            working_directory=str(Path.cwd()),
        ))
        return 0
    if args.uninstall_task_scheduler:
        print("removed" if uninstall_task_scheduler() else "not installed")
        return 0
    if args.query_task_scheduler:
        print(query_task_scheduler())
        return 0

    if args.watchdog:
        return run_watchdog(_daemon_arguments(args))

    config = _config(args)
    store = SQLiteStore(config.db_path)
    ollama_client = _local_ollama_client(config)
    from noema.domain.presence import PresenceDetector
    from noema.domain.sessions import Sessionizer
    classifier = _classifier(config)
    service = NoemaService(
        ActivityWatchAdapter(ActivityWatchClient(config.telemetry_url)),
        store,
        sessionizer=Sessionizer(
            merge_gap_seconds=config.session_merge_gap_seconds,
            short_event_seconds=config.session_short_event_seconds,
        ),
        presence_detector=PresenceDetector(
            timeout_seconds=config.afk_timeout_seconds,
            poll_interval_seconds=config.afk_poll_interval_seconds,
            media_exception=config.afk_media_exception,
        ),
        classifier=classifier,
        intent_engine=IntentEngine(client=ollama_client),
        intervention_engine=_intervention_engine(config),
        meme_intelligence=MemeIntelligence(client=ollama_client),
        meaningful_session_summarizer=MeaningfulSessionSummarizer(client=ollama_client),
    )
    _realtime_setup(service, config, classifier)
    # Observability: one recorder observes chain, classifier, service,
    # workers, and API. Product code paths are unchanged when absent;
    # here it is always attached for production telemetry.
    recorder = TelemetryRecorder(store)
    service.telemetry = recorder
    classifier.observer = recorder
    chain = getattr(classifier, "provider", None)
    if isinstance(chain, ProviderChain):
        chain.observer = recorder
    daemon = NoemaDaemon(service, config)
    try:
        if args.once:
            print(daemon.run_once(include_daily_report=True))
            return 0
        daemon.start()
        serve(
            service,
            host=config.host,
            port=config.port,
            websocket_port=config.websocket_port,
            daemon=daemon,
        )
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        daemon.stop()
        store.close()
