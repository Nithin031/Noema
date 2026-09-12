import io
import json
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import pytest
except ModuleNotFoundError:  # pragma: no cover - local harness fallback
    class _PytestCompat:
        @staticmethod
        @contextmanager
        def raises(expected):
            try:
                yield
            except expected:
                return
            raise AssertionError("expected {}".format(expected.__name__))
    pytest = _PytestCompat()

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.runtime.autostart import build_task_scheduler_xml, build_user_autostart_command
from noema.runtime import (
    NoemaDaemon,
    AlreadyRunningError,
    DaemonConfig,
    SingleInstanceLock,
)
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classifier
from noema.infrastructure.ollama import OllamaError


def _test_path(name):
    return Path.cwd() / "tests" / "noema" / name


class OfflineOllama:
    model = "offline"

    def generate_json(self, prompt):
        raise OllamaError("offline")


class FakeActivityWatch:
    def __init__(self):
        self.calls = []

    def list_buckets(self):
        return {"web_laptop": {"hostname": "laptop"}}

    def get_events(self, bucket_id, start=None, end=None):
        self.calls.append((start, end))
        return [{
            "id": "youtube-event",
            "timestamp": "2026-09-05T10:00:00Z",
            "duration": 600,
            "data": {
                "app": "Firefox",
                "browser": "firefox",
                "browserWindowId": "17",
                "browserTabId": "42",
                "title": "YouTube",
                "url": "https://youtube.com/watch?v=1",
            },
        }]


def call_app(app, method, path, body=b""):
    result = {}

    def start_response(status, headers):
        result["status"] = status

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    response = b"".join(app(environ, start_response))
    return result["status"], json.loads(response.decode("utf-8"))


def test_daemon_runs_ordered_pipeline_and_exposes_health():
    store = SQLiteStore()
    activitywatch = FakeActivityWatch()
    service = NoemaService(
        ActivityWatchAdapter(activitywatch),
        store,
        classifier=Classifier(client=OfflineOllama()),
    )
    config = DaemonConfig(db_path=":memory:", lock_path=str(_test_path(".daemon-cycle.lock")))
    daemon = NoemaDaemon(service, config)
    result = daemon.run_once(now="2026-09-05T10:10:00Z")

    assert result["ingest"]["ingested"]["accepted"] == 1
    assert result["ingest"]["raw_sessions"] == 1
    assert result["semantics"]["raw_classifications"] == 0
    assert result["semantics"]["classifications"] == 1
    assert result["behavior"]["actionable"] == 0
    assert result["behavior"]["interventions_considered"] == 0
    assert result["outcomes"]["executed_interventions"] == 0
    assert len(store.query_meaningful_sessions()) == 1
    assert len(store.query_interventions()) == 0
    assert len(store.query_outcomes()) == 0

    app = create_app(service, daemon=daemon)
    status, payload = call_app(app, "GET", "/api/daemon/health")
    assert status.startswith("200")
    assert payload["status"] == "stopped"
    assert set(payload["workers"]) == {
        "ingest", "semantics", "behavior", "outcomes", "daily_report", "realtime"
    }
    daemon.run_once(now="2026-09-05T10:11:00Z")
    assert len(activitywatch.calls) == 2
    assert activitywatch.calls[1][0].isoformat().startswith("2026-09-05T10:08:00")
    assert store.get_state("daemon.last_ingest_at").startswith("2026-09-05T10:11:00")
    store.close()


def test_active_lock_rejects_second_owner_and_release_allows_restart():
    lock_path = str(_test_path(".daemon-lock.lock"))
    Path(lock_path).unlink(missing_ok=True)
    try:
        first = SingleInstanceLock(lock_path)
        second = SingleInstanceLock(lock_path)
        first.acquire()
        assert first.owned
        with pytest.raises(AlreadyRunningError):
            second.acquire()
        first.release()
        assert not first.owned
        second.acquire()
        assert second.owned
    finally:
        second.release()
        first.release()
        Path(lock_path).unlink(missing_ok=True)


def test_stale_lock_with_dead_pid_is_recovered_and_can_start_again():
    lock_path = str(_test_path(".daemon-stale-lock.lock"))
    stale = Path(lock_path)
    recovered = SingleInstanceLock(lock_path)
    stale.unlink(missing_ok=True)
    try:
        stale.write_text(str(2147483647), encoding="utf-8")
        recovered.acquire()
        assert recovered.owned
    finally:
        recovered.release()
        stale.unlink(missing_ok=True)


def test_windows_autostart_command_is_hidden_and_uses_daemon_module():
    command = build_user_autostart_command(
        r'"C:\Python\python.exe" -m noema',
        str(Path.cwd()),
    )
    assert "WindowsPowerShell" in command
    assert "-WindowStyle Hidden" in command
    assert "noema" in command
    assert "-m noema.runtime" not in command
    assert "Set-Location" in command


def test_windows_task_scheduler_xml_starts_hidden_limited_and_restarts_on_failure():
    xml = build_task_scheduler_xml(
        r"C:\Python\python.exe",
        ["-m", "noema.runtime", "--config", r"C:\AI Activity OS\daemon.json"],
        str(Path.cwd()),
        user_id="test-user",
    )

    assert "InteractiveToken" in xml
    assert "LeastPrivilege" in xml
    assert "PT30S" in xml
    assert "RestartOnFailure" in xml
    assert "PT60S" in xml
    assert "IgnoreNew" in xml
    assert "noema.runtime" in xml
    assert "daemon.json" in xml


def test_task_scheduler_command_can_run_the_supervisor():
    from noema.cli.daemon import _daemon_arguments

    args = type("Args", (), {
        "config": None,
        "telemetry_url": None,
        "db": None,
        "lock": None,
        "host": None,
        "port": None,
        "websocket_port": None,
        "log_level": None,
    })()
    assert _daemon_arguments(args, watchdog=True) == [
        "-m", "noema", "--watchdog"
    ]


def test_default_cadence_separates_telemetry_and_semantic_windows():
    config = DaemonConfig()
    assert config.ingest_interval_seconds == 1
    assert config.behavior_interval_seconds == 300
    assert config.classification_interval_seconds == 1200
    assert config.severe_classification_interval_seconds == 300
    assert config.classification_batch_size == 20
    assert config.classification_max_retries == 5
    assert config.classification_version == "1"
    assert config.background_ai_enabled is True


def test_second_daemon_is_rejected_until_first_daemon_releases():
    lock_path = str(_test_path(".daemon-process-lock.lock"))
    Path(lock_path).unlink(missing_ok=True)
    first_store = SQLiteStore()
    second_store = SQLiteStore()
    first = NoemaDaemon(
        NoemaService(
            ActivityWatchAdapter(FakeActivityWatch()),
            first_store,
            classifier=Classifier(client=OfflineOllama()),
        ),
        DaemonConfig(db_path=":memory:", lock_path=lock_path, report_interval_seconds=3600),
    )
    second = NoemaDaemon(
        NoemaService(ActivityWatchAdapter(), second_store),
        DaemonConfig(db_path=":memory:", lock_path=lock_path, report_interval_seconds=3600),
    )
    try:
        first.start()
        assert first.running
        with pytest.raises(AlreadyRunningError):
            second.start()
        first.stop()
        second.start()
        assert second.running
    finally:
        first.stop()
        second.stop()
        first_store.close()
        second_store.close()
        Path(lock_path).unlink(missing_ok=True)


def test_daemon_reloads_json_configuration():
    config_path = _test_path(".daemon-config.json")
    config_path.write_text(
        json.dumps({"behavior_interval_seconds": 12, "query_limit": 25}),
        encoding="utf-8",
    )
    try:
        config = DaemonConfig.from_file(config_path)
        assert config.behavior_interval_seconds == 12
        assert config.query_limit == 25

        store = SQLiteStore()
        service = NoemaService(ActivityWatchAdapter(), store)
        daemon = NoemaDaemon(service, config)
        config_path.write_text(
            json.dumps({"behavior_interval_seconds": 3, "query_limit": 10}),
            encoding="utf-8",
        )
        result = daemon.reload_config()
        assert result["reloaded"] is True
        assert daemon.config.behavior_interval_seconds == 3
        assert daemon.health_dict()["workers"]["behavior"]["interval_seconds"] == 3
        config_path.write_text(
            json.dumps({"provider": "gemini", "gemini_api_key_env": "TEST_ONLY_KEY"}),
            encoding="utf-8",
        )
        provider_reload = daemon.reload_config()
        assert provider_reload["reloaded"] is False
        assert "require daemon restart" in provider_reload["reason"]
        assert daemon.config.provider == "hosted"
        store.close()
    finally:
        config_path.unlink(missing_ok=True)


def test_daemon_background_loop_survives_and_stops():
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(FakeActivityWatch()),
        store,
        classifier=Classifier(client=OfflineOllama()),
    )
    daemon = NoemaDaemon(
        service,
        DaemonConfig(
            db_path=":memory:",
            lock_path=str(_test_path(".daemon-loop.lock")),
            ingest_interval_seconds=0.02,
            behavior_interval_seconds=0.02,
            classification_interval_seconds=0.02,
            outcome_interval_seconds=0.02,
            report_interval_seconds=3600,
        ),
    )
    daemon.start()
    time.sleep(0.12)
    daemon.stop()
    health = daemon.health_dict()
    assert health["status"] == "stopped"
    assert health["workers"]["ingest"]["run_count"] >= 1
    assert not daemon.running
    store.close()


def test_semantics_processes_newest_chunk_first_and_continues_next_run():
    from noema.domain.activity import ActivityEvent

    class BulkProvider:
        name = "ollama"
        model = "chunk-test"

        def classify(self, session, prompt):
            return {
                "category": "productive", "subcategory": "research",
                "activity": "Chunked work", "signal": "Chunked drain verification.",
                "confidence": 0.8, "productivity": "productive",
            }

    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(FakeActivityWatch()),
        store,
        classifier=Classifier(provider=BulkProvider()),
    )
    for i in range(5):
        service.ingest_events([ActivityEvent(
            timestamp="2026-09-06T{:02d}:00:00Z".format(8 + i), duration=600,
            device="laptop", app="App", title="Window {}".format(i),
            bucket_id="w", source_event_id="chunk-{}".format(i),
        )])
    service.sessionize_stored_events()
    service.build_meaningful_sessions(start="2026-09-06T00:00:00Z", end="2026-09-07T00:00:00Z")
    assert len(store.query_meaningful_sessions(limit=100000)) == 5
    daemon = NoemaDaemon(
        service,
        DaemonConfig(
            db_path=":memory:",
            lock_path=str(_test_path(".daemon-chunk.lock")),
            classification_batch_size=10,
            classification_max_per_run=2,
        ),
    )
    try:
        first = daemon.run_once(now="2026-09-06T12:00:00Z")
        assert first["semantics"]["classifications"] == 2
        assert first["semantics"]["total_pending"] == 5
        assert first["semantics"]["remaining_pending"] == 3
        done_ids = {item.session_id for item in store.query_classifications(limit=100000)}
        assert len(done_ids) == 2
        second = daemon.run_once(now="2026-09-06T12:30:00Z")
        assert second["semantics"]["classifications"] == 2
        assert len({item.session_id for item in store.query_classifications(limit=100000)}) == 4
        # Newest-first: the latest window is classified before older backlog.
        assert done_ids != {item.session_id for item in store.query_classifications(limit=100000)}
    finally:
        store.close()
        Path(_test_path(".daemon-chunk.lock")).unlink(missing_ok=True)


def test_ingestion_is_independent_from_periodic_semantics_and_health_is_separate():
    class CountingOllama(OfflineOllama):
        def __init__(self):
            self.calls = 0

        def generate_json(self, prompt):
            self.calls += 1
            return super().generate_json(prompt)

    client = CountingOllama()
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(FakeActivityWatch()),
        store,
        classifier=Classifier(client=client),
    )
    daemon = NoemaDaemon(
        service,
        DaemonConfig(
            db_path=":memory:",
            lock_path=str(_test_path(".daemon-independent.lock")),
            ingest_interval_seconds=0.01,
            classification_interval_seconds=60,
            behavior_interval_seconds=60,
            outcome_interval_seconds=60,
            report_interval_seconds=3600,
            background_ai_enabled=True,
        ),
    )
    try:
        daemon.start()
        time.sleep(0.08)
        health = daemon.health_dict()
        assert health["daemon_running"] is True
        assert health["source_connected"] is True
        assert health["workers"]["ingest"]["run_count"] >= 2
        assert health["workers"]["semantics"]["run_count"] == 0
        assert health["next_ollama_analysis"] is not None
        assert client.calls == 0
    finally:
        daemon.stop()
        store.close()
        Path(_test_path(".daemon-independent.lock")).unlink(missing_ok=True)


def test_stop_ollama_model_without_executable_is_honest_and_path_free(monkeypatch):
    import shutil as _shutil

    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(FakeActivityWatch()),
        store,
        classifier=Classifier(client=OfflineOllama()),
    )
    config = DaemonConfig(db_path=":memory:", lock_path=str(_test_path(".daemon-stop.lock")))
    daemon = NoemaDaemon(service, config)
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    try:
        result = daemon.stop_ollama_model()
        assert result["stopped"] is False
        assert result["model"] == config.ollama_model
        assert "not found on PATH" in result["message"]
        assert "Users" not in result["message"] and "nithi" not in result["message"]
    finally:
        store.close()
        Path(_test_path(".daemon-stop.lock")).unlink(missing_ok=True)


def test_stop_ollama_model_reports_subprocess_failure_without_raising(monkeypatch):
    import shutil as _shutil

    import noema.runtime.daemon as _daemon_module

    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(FakeActivityWatch()),
        store,
        classifier=Classifier(client=OfflineOllama()),
    )
    config = DaemonConfig(db_path=":memory:", lock_path=str(_test_path(".daemon-stop-fail.lock")))
    daemon = NoemaDaemon(service, config)
    monkeypatch.setattr(_shutil, "which", lambda name: r"C:\fake\ollama.exe")

    def _boom(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(_daemon_module.subprocess, "run", _boom)
    try:
        result = daemon.stop_ollama_model()
        assert result["stopped"] is False
        assert "could not run ollama stop" in result["message"]
    finally:
        store.close()
        Path(_test_path(".daemon-stop-fail.lock")).unlink(missing_ok=True)
