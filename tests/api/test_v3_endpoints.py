"""V3 API contract: responses, break, intervention feed + feedback."""

import io
import json

from noema.api import NoemaService, create_app
from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.domain.sessions import ActivitySession
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore


def call_app(app, method, path, body=None):
    result = {}

    def start_response(status, headers):
        result["status"] = status

    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
    }
    response = b"".join(app(environ, start_response))
    return result["status"], json.loads(response.decode("utf-8"))


def wired():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    session = ActivitySession(
        start="2026-09-09T10:00:00Z", end="2026-09-09T10:30:00Z",
        device="laptop", app="Firefox", domain="youtube.com",
        title="cat videos", event_keys=("e1",))
    store.insert_session(session)
    store.insert_behavior_observations([BehaviorObservation(
        session_id=session.id, state=BehaviorState.DISTRACTED,
        started_at=session.start, confidence=0.9, distraction_score=0.9,
        actionable=True, reason="test")])
    intervention = service.consider_intervention(session.id, execute=False)
    return store, service, create_app(service), intervention


def test_responses_endpoints_serve_registry_and_effectiveness():
    store, _, app, _ = wired()
    try:
        status, payload = call_app(app, "GET", "/api/responses")
        assert status.startswith("200")
        assert len(payload["responses"]) >= 8
        assert all("recovery_rate" in row for row in payload["responses"])
        status, payload = call_app(app, "GET", "/api/responses/effectiveness")
        assert status.startswith("200")
        assert isinstance(payload["effectiveness"], list)
    finally:
        store.close()


def test_break_endpoint_and_intervention_break():
    store, _, app, intervention = wired()
    try:
        status, payload = call_app(app, "GET", "/api/break")
        assert status.startswith("200")
        assert payload["break"]["active"] is False
        status, payload = call_app(
            app, "POST", "/api/interventions/{}/break".format(intervention.id),
            {"minutes": 5})
        assert status.startswith("200")
        assert payload["break"]["active"] is True
        status, payload = call_app(app, "GET", "/api/break")
        assert payload["break"]["active"] is True
        status, payload = call_app(
            app, "POST", "/api/interventions/missing/break", {"minutes": 5})
        assert status.startswith("400")
    finally:
        store.close()


def test_intervention_feed_and_feedback():
    store, _, app, intervention = wired()
    try:
        status, payload = call_app(app, "GET", "/api/interventions/feed")
        assert status.startswith("200")
        assert len(payload["interventions"]) == 1
        row = payload["interventions"][0]
        for key in ("id", "mode", "status", "message", "delivered",
                    "interacted", "actions", "outcome", "feedback"):
            assert key in row, key
        assert row["response_id"]
        status, payload = call_app(
            app, "POST", "/api/interventions/{}/feedback".format(intervention.id),
            {"feedback_type": "USEFULNESS", "value": "HELPFUL"})
        assert status.startswith("201")
        assert payload["feedback"]["value"] == "HELPFUL"
        status, payload = call_app(
            app, "POST", "/api/interventions/{}/feedback".format(intervention.id),
            {"feedback_type": "USEFULNESS", "value": "MAYBE"})
        assert status.startswith("400")
        status, payload = call_app(
            app, "POST", "/api/interventions/missing/feedback",
            {"feedback_type": "USEFULNESS", "value": "HELPFUL"})
        assert status.startswith("400")
    finally:
        store.close()
