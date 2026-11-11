"""The daemon serves the built React app; the old bundled dashboard is gone."""

import io

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import (
    ActivityWatchAdapter,
    ActivityWatchClient,
)
from noema.infrastructure.database import SQLiteStore


def _get(app, path):
    result = {}

    def start_response(status, headers):
        result["status"] = status
        result["headers"] = dict(headers)

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "CONTENT_LENGTH": "0",
        "wsgi.input": io.BytesIO(b""),
    }
    body = b"".join(app(environ, start_response))
    return result["status"], result["headers"], body


def _app(frontend_dist):
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(ActivityWatchClient("http://127.0.0.1:9")), store)
    app = create_app(service)
    app.frontend_dist = frontend_dist
    return store, app


def test_built_frontend_is_served_at_root_and_assets(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<div id=root>riso</div>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    store, app = _app(tmp_path)
    for path in ("/", "/dashboard", "/dashboard/"):
        status, _, body = _get(app, path)
        assert status.startswith("200"), path
        assert b"riso" in body
    status, headers, body = _get(app, "/assets/app.js")
    assert status.startswith("200")
    assert "javascript" in headers["Content-Type"]
    store.close()


def test_unbuilt_frontend_gets_a_clear_page_not_the_old_dashboard(tmp_path):
    store, app = _app(tmp_path / "does-not-exist")
    status, headers, body = _get(app, "/")
    assert status.startswith("200")
    assert headers["Content-Type"].startswith("text/html")
    assert b"npm run build" in body
    store.close()


def test_old_bundled_dashboard_assets_are_gone(tmp_path):
    store, app = _app(tmp_path / "does-not-exist")
    for path in ("/app.js", "/site.css", "/stages.js", "/tokens.css"):
        status, _, _ = _get(app, path)
        assert status.startswith("404"), path
    store.close()


def test_frontend_path_traversal_is_contained(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<div>ok</div>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    store, app = _app(dist)
    status, _, body = _get(app, "/assets/../../secret.txt")
    assert b"TOPSECRET" not in body
    store.close()
