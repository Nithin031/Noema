"""API safe-error mapping: source/db/internal failures never leak stacks."""

import io
import json

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import (
    ActivityWatchAdapter,
    ActivityWatchClient,
)
from noema.infrastructure.database import SQLiteStore


def call_app(app, method, path, body=b""):
    result = {}

    def start_response(status, headers):
        result["status"] = status

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path.split("?", 1)[0],
        "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    raw = b"".join(app(environ, start_response))
    return result["status"], json.loads(raw.decode("utf-8"))


def _service():
    store = SQLiteStore()
    adapter = ActivityWatchAdapter(ActivityWatchClient("http://127.0.0.1:9"))
    service = NoemaService(adapter, store)
    return store, service


def test_source_unavailable_is_503_without_stack():
    store, service = _service()
    app = create_app(service)
    # Points at an unroutable collector; reconciliation must not raise.
    status, payload = call_app(
        app, "GET", "/api/debug/reconciliation?application=Code.exe")
    assert status.startswith("503"), (status, payload)
    assert payload == {"error": "source_unavailable"}
    store.close()


def test_unknown_route_is_404_and_bad_request_is_400():
    store, service = _service()
    app = create_app(service)
    status, payload = call_app(app, "GET", "/api/nope-unknown")
    assert status.startswith("404")
    assert payload == {"error": "not_found"}
    status, payload = call_app(app, "GET", "/api/events?limit=notanint")
    assert status.startswith("400")
    assert "error" in payload
    assert "Traceback" not in json.dumps(payload)
    store.close()


def test_daemon_gated_routes_are_503_without_daemon():
    store, service = _service()
    app = create_app(service)
    status, payload = call_app(app, "GET", "/api/daemon/health")
    assert status.startswith("503")
    assert payload["status"] == "unavailable"
    status, payload = call_app(app, "POST", "/api/realtime/evaluate", body=b"{}")
    assert status.startswith("503")
    store.close()
