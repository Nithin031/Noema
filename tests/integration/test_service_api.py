import io
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classifier
from noema.application.classification import Classification
from noema.domain.intent import IntentEngine
from noema.infrastructure.ollama import OllamaError
from noema.domain.privacy import PrivacyFilter, PrivacyPolicy
from noema.domain.activity import ActivityEvent
from noema.domain.sessions import Sessionizer


def bucket():
    return {
        "id": "web_laptop",
        "hostname": "laptop",
        "events": [
            {
                "id": "ok",
                "timestamp": "2026-09-04T10:00:00Z",
                "duration": 5,
                "data": {"app": "Chrome", "url": "https://example.com"},
            },
            {
                "id": "private",
                "timestamp": "2026-09-04T10:01:00Z",
                "duration": 5,
                "data": {"app": "Chrome", "incognito": True},
            },
        ],
    }


def call_app(app, method, path, body=b""):
    result = {}

    def start_response(status, headers):
        result["status"] = status
        result["headers"] = headers

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path.split("?", 1)[0],
        "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    response = b"".join(app(environ, start_response))
    return result["status"], json.loads(response.decode("utf-8"))


def test_service_and_local_api():
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(),
        store,
        PrivacyFilter(PrivacyPolicy(blocked_apps=set())),
    )
    result = service.ingest_bucket(bucket())
    assert result.to_dict() == {
        "accepted": 1,
        "discarded": 1,
        "duplicates": 0,
        "processed": 2,
    }

    app = create_app(service)
    status, health = call_app(app, "GET", "/health")
    assert status.startswith("200")
    assert health == {"status": "ok"}

    status, events = call_app(app, "GET", "/api/events?limit=10")
    assert status.startswith("200")
    assert len(events["events"]) == 1
    assert events["events"][0]["domain"] == "example.com"

    sessions = service.sessionize_stored_events()
    assert len(sessions) == 1
    store.insert_classification(Classification(
        sessions[0].id,
        "work/research",
        topic="example domain",
        activity_type="reading",
        productivity="productive",
        confidence=0.9,
        provider="ollama", model="test", source="ollama",
        classification_status="classified",
    ))
    status, session_payload = call_app(app, "GET", "/api/sessions")
    assert status.startswith("200")
    assert len(session_payload["sessions"]) == 1

    status, events = call_app(app, "GET", "/api/events?limit=10")
    assert status.startswith("200")
    assert events["events"][0]["category"] == "productive"
    assert events["events"][0]["productivity"] == "productive"

    status, ingested = call_app(
        app,
        "POST",
        "/api/ingest/activitywatch",
        json.dumps({"bucket": bucket()}).encode("utf-8"),
    )
    assert status.startswith("200")
    assert ingested["duplicates"] == 1
    assert ingested["discarded"] == 1
    store.close()


class OfflineOllama:
    model = "offline"

    def generate_json(self, prompt):
        raise OllamaError("offline")


class FakeActivityWatch:
    def list_buckets(self):
        return {"window_laptop": {"hostname": "laptop"}}

    def get_events(self, bucket_id, start=None, end=None):
        return [{
            "id": "cycle-event",
            "timestamp": "2026-09-04T10:00:00Z",
            "duration": 600,
            "data": {"app": "VS Code", "title": "reward.py"},
        }]


def test_autonomous_cycle_uses_meaningful_sessions_after_raw_bootstrap():
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(FakeActivityWatch()),
        store,
        classifier=Classifier(client=OfflineOllama()),
        intent_engine=IntentEngine(client=OfflineOllama()),
    )
    service.capture_intent("Implement code for Unitree A1")

    cycle = service.run_autonomous_cycle()

    assert len(cycle.sessions) == 1
    assert cycle.sessions[0].activity_session_ids
    assert cycle.classifications[0].session_id == cycle.sessions[0].id
    assert cycle.observations[0].session_id == cycle.sessions[0].id
    assert cycle.observations[0].state.value == "NORMAL"
    assert store.query_meaningful_sessions()[0].id == cycle.sessions[0].id
    store.close()


def test_browser_api_registers_events_and_records_actions():
    store = SQLiteStore()
    app = create_app(NoemaService(ActivityWatchAdapter(), store))
    status, registration = call_app(
        app, "POST", "/api/browser/register",
        json.dumps({
            "browser": "firefox", "device_id": "laptop-01",
            "extension_instance_id": "extension-api",
        }).encode("utf-8"),
    )
    assert status.startswith("201")
    assert registration["browser"] == "firefox"
    status, current = call_app(
        app, "POST", "/api/browser/event",
        json.dumps({
            "extension_instance_id": "extension-api",
            "event": {"event_type": "tab_activated", "window_id": "17", "tab_id": "42"},
        }).encode("utf-8"),
    )
    assert status.startswith("200")
    assert current["current_tab_id"] == "42"
    status, action = call_app(
        app, "POST", "/api/interventions/int-api/action",
        json.dumps({"action": "DISMISS_WORKING"}).encode("utf-8"),
    )
    assert status.startswith("200")
    assert action["action"] == "dismissed_as_working"
    assert store.query_intervention_actions("int-api")[0]["action"] == "dismissed_as_working"
    store.close()


def test_daily_summary_filters_calendar_day_and_deduplicates_overlaps():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    events = [
        ActivityEvent(
            timestamp="2026-09-05T18:20:00Z", duration=120,
            device="laptop", app="Firefox", domain="reddit.com", title="Reddit",
            bucket_id="window", source_event_id="yesterday",
        ),
        ActivityEvent(
            timestamp="2026-09-05T18:30:00Z", duration=60,
            device="laptop", app="Firefox", domain="instagram.com", title="Instagram",
            bucket_id="window", source_event_id="today-1",
        ),
        ActivityEvent(
            timestamp="2026-09-05T18:30:30Z", duration=60,
            device="laptop", app="Firefox", domain="instagram.com", title="Instagram",
            bucket_id="window", source_event_id="today-2",
        ),
    ]
    service.ingest_events(events)
    sessions = service.sessionize_stored_events()
    service.store.insert_classification(Classification(
        sessions[1].id, "media/social_media", topic="Instagram",
        activity_type="browsing", productivity="distracting", confidence=0.9,
        provider="ollama", model="test", source="ollama",
        classification_status="classified",
    ))

    zone = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 9, 6, tzinfo=zone)
    summary = service.daily_summary(start.astimezone(ZoneInfo("UTC")), (start.replace(day=7)).astimezone(ZoneInfo("UTC")), timezone_name="Asia/Kolkata")

    assert summary["date"] == "2026-09-06"
    assert summary["total_time"] == 90.0
    assert summary["distractive_time"] == 90.0
    assert summary["neutral_time"] == 0.0
    assert summary["distractions"] == [{"name": "instagram.com", "seconds": 90.0}]
    assert len(summary["timeline"]) == 1
    store.close()


def test_currentwindow_heartbeat_refreshes_duration_and_session():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    first = ActivityEvent(
        timestamp="2026-09-06T10:00:00Z", duration=61,
        device="laptop", app="RocketLeague.exe",
        title="Rocket League (64-bit, DX11, Cooked)",
        bucket_id="window", source_event_id="rocket-live",
        metadata={"activitywatch_bucket_type": "currentwindow"},
    )
    second = ActivityEvent(
        timestamp="2026-09-06T10:00:00Z", duration=509,
        device="laptop", app="RocketLeague.exe",
        title="Rocket League (64-bit, DX11, Cooked)",
        bucket_id="window", source_event_id="rocket-live",
        metadata={"activitywatch_bucket_type": "currentwindow"},
    )

    assert service.ingest_events([first]).accepted == 1
    refreshed = service.ingest_events([second])
    assert refreshed.updated == 1
    assert service.store.query(start="2026-09-06T09:59:00Z", end="2026-09-06T10:10:00Z", limit=10)[0].event.duration == 509

    sessions = service.sessionize_stored_events(
        start="2026-09-06T09:59:00Z", end="2026-09-06T10:10:00Z"
    )
    assert len(sessions) == 1
    assert sessions[0].duration == 509
    store.close()


def test_reconciliation_endpoint_reports_each_pipeline_layer():
    class FakeActivityWatch:
        def list_buckets(self):
            return {"window_laptop": {"hostname": "laptop", "type": "currentwindow"}}

        def get_events(self, bucket_id, start=None, end=None):
            return [{
                "id": "rocket-debug",
                "timestamp": "2026-09-06T10:00:00Z",
                "duration": 509,
                "data": {
                    "app": "RocketLeague.exe",
                    "title": "Rocket League (64-bit, DX11, Cooked)",
                },
            }]

    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(FakeActivityWatch()), store)
    start = datetime(2026, 9, 6, 9, 59, tzinfo=ZoneInfo("UTC"))
    end = datetime(2026, 9, 6, 10, 10, tzinfo=ZoneInfo("UTC"))
    service.ingest_telemetry(start=start, end=end)
    service.sessionize_stored_events(start=start, end=end)
    service.build_meaningful_sessions(start=start, end=end, sequence_terminated=False)

    status, payload = call_app(
        create_app(service),
        "GET",
        "/api/debug/reconciliation?application=Rocket%20League&start=2026-09-06T09:59:00Z&end=2026-09-06T10:10:00Z",
    )
    assert status.startswith("200")
    assert payload["application_id"] == "rocket_league"
    assert payload["raw_aw_duration"] == 509.0
    assert payload["normalized_union_seconds"] == 509.0
    assert payload["session_seconds"] == 509.0
    assert payload["differences"]["aw_union_minus_dashboard"] == 0.0
    assert payload["raw_events"][0]["event_start"]
    assert payload["raw_events"][0]["event_end"]
    store.close()
