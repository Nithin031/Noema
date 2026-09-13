import io
import json
from datetime import datetime, timedelta, timezone

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.domain.activity import ActivityEvent
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classifier
from noema.domain.sessions import Sessionizer


def call_app(app, path):
    result = {}

    def start_response(status, headers):
        result["status"] = status

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path.split("?", 1)[0],
        "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
        "CONTENT_LENGTH": "0",
        "wsgi.input": io.BytesIO(),
    }
    body = b"".join(app(environ, start_response))
    return result["status"], json.loads(body.decode("utf-8"))


class OllamaPayload:
    model = "test:3b"

    def generate_json(self, prompt):
        return {
            "category": "productive",
            "subcategory": "technical_research",
            "activity": "Researching ML model quantization",
            "signal": "The title identifies a machine-learning research topic.",
            "topic": "ML model quantization",
            "service": "browser",
            "intent_signal": "technical research",
            "activity_type": "research",
            "productivity": "productive",
            "confidence": 0.87,
        }


def make_session(title, timestamp="2026-09-06T10:00:00Z", domain="example.com"):
    event = ActivityEvent(
        timestamp=timestamp,
        duration=60,
        device="laptop",
        app="Firefox",
        title=title,
        domain=domain,
        url="https://{}/page".format(domain),
        bucket_id="window_laptop",
        source_event_id="{}-{}".format(timestamp, title),
    )
    return Sessionizer().sessionize([event])[0]


def test_ollama_job_records_real_provider_provenance_and_metrics():
    classifier = Classifier(client=OllamaPayload())
    result = classifier.classify_many([make_session("Unclear project dashboard")])[0]

    assert result.source == "ollama"
    assert result.provider == "ollama"
    assert result.model == "test:3b"
    assert result.service == "browser"
    debug = classifier.debug_telemetry()
    assert debug["jobs_last_24h"] == 1
    assert debug["ollama_jobs"] == 1
    assert debug["last_success"]["successful_calls"] == 1
    assert debug["last_success"]["input_session_count"] == 1


def test_recent_activity_inherits_parent_meaningful_classification():
    from noema.application.classification import Classification
    from noema.domain.meaningful import MeaningfulSession

    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    service.ingest_events([
        ActivityEvent(
            timestamp="2026-09-06T10:00:00Z", duration=60, device="laptop",
            app="Firefox", title="Docs page", domain="docs.python.org",
            bucket_id="window", source_event_id="raw-1",
        ),
    ])
    sessions = service.sessionize_stored_events()
    assert len(sessions) == 1
    meaningful = MeaningfulSession(
        start_time="2026-09-06T10:00:00Z", end_time="2026-09-06T10:01:00Z",
        device_set=("laptop",), activity_session_ids=(sessions[0].id,),
    )
    store.insert_meaningful_session(meaningful)
    store.insert_classification(Classification(
        meaningful.id, "productive", topic="research", activity_type="reading",
        productivity="productive", confidence=0.9, provider="gemini",
        model="gemini-2.5-flash", source="gemini", classification_status="classified",
    ))

    rows = service.recent_activity("2026-09-06T00:00:00Z", "2026-09-07T00:00:00Z")

    assert len(rows) == 1
    assert rows[0]["category"] == "productive"
    assert rows[0]["classification_source"] == "gemini"
    assert rows[0]["classification_status"] == "classified"
    store.close()


def test_today_and_recent_activity_are_session_scoped():
    # Rolling timestamps anchored to the IST calendar day (the product
    # timezone): the "old" event is always outside the current local day
    # and the "today" event always inside it, even across UTC midnight.
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    ist_midnight = datetime.now(ist).replace(hour=0, minute=0, second=0, microsecond=0)
    today_stamp = (ist_midnight + timedelta(hours=12)).isoformat().replace("+00:00", "Z")
    old_stamp = (ist_midnight - timedelta(hours=13)).isoformat().replace("+00:00", "Z")
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    service.ingest_events([
        ActivityEvent(
            timestamp=old_stamp, duration=30, device="laptop",
            app="Firefox", title="Old GitHub project", domain="github.com",
            bucket_id="window", source_event_id="old",
        ),
        ActivityEvent(
            timestamp=today_stamp, duration=45, device="laptop",
            app="Firefox", title="Instagram", domain="instagram.com",
            bucket_id="window", source_event_id="today",
        ),
    ])
    sessions = service.sessionize_stored_events()
    service.store.insert_classification(
        service.classifier.classify(sessions[0])
    )
    app = create_app(service)

    status, activities = call_app(app, "/api/dashboard/recent-activity?range=today&timezone=Asia%2FKolkata")
    assert status.startswith("200")
    assert len(activities["activities"]) == 1
    assert activities["activities"][0]["title"] == "Instagram"

    status, current = call_app(app, "/api/dashboard/current")
    assert status.startswith("200")
    assert current["application"]
    assert current["classification"]["status"] in {"pending", "classified"}

    status, debug = call_app(app, "/api/debug/classifier")
    assert status.startswith("200")
    for key in ("ollama_available", "configured_model", "jobs_last_24h", "pending_jobs"):
        assert key in debug
    store.close()
