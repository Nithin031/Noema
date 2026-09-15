"""V3 Phase 10 Meme Center API contract."""

import csv
import io
import json

from noema.api import NoemaService, create_app
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore


def call_app(app, method, path, body=None, query=""):
    result = {}
    headers = {}

    def start_response(status, response_headers):
        result["status"] = status
        headers.update(dict(response_headers))

    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(raw)),
        "wsgi.input": io.BytesIO(raw),
    }
    chunks = b"".join(app(environ, start_response))
    content_type = headers.get("Content-Type", "")
    if content_type.startswith("application/json"):
        return result["status"], headers, json.loads(chunks.decode("utf-8"))
    return result["status"], headers, chunks


def make_image(path, size=(32, 24)):
    from PIL import Image

    Image.new("RGB", size, (10, 200, 90)).save(path)


def wired(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    rows = [
        ("0", "image_1.jpg", "STUDY HARD", "Study hard", "positive"),
        ("1", "image_2.png", "REST NOW", "Rest now", "neutral"),
        ("2", "image_3.jpg", "DOOM SCROLL", "Doom scroll", "negative"),
    ]
    for _number, name, _ocr, _corrected, _sentiment in rows:
        make_image(str(images / name))
    csv_path = tmp_path / "labels.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["number", "image_name", "text_ocr", "text_corrected",
                         "overall_sentiment"])
        writer.writerows(rows)
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    service._meme_dataset_dir = str(images)
    stats = service.ingest_meme_dataset(
        str(csv_path), str(images), thumbs_dir=str(tmp_path / "thumbs"))
    assert stats["imported"] == 3
    return store, service, create_app(service)


def test_asset_list_search_filter_pagination(tmp_path):
    store, _, app = wired(tmp_path)
    try:
        status, _, payload = call_app(app, "GET", "/api/meme-assets")
        assert status.startswith("200")
        assert payload["total"] == 3
        assert len(payload["items"]) == 3
        status, _, payload = call_app(app, "GET", "/api/meme-assets", query="q=study")
        assert payload["total"] == 1
        status, _, payload = call_app(
            app, "GET", "/api/meme-assets", query="sentiment=neutral")
        assert payload["total"] == 1
        status, _, payload = call_app(
            app, "GET", "/api/meme-assets", query="limit=2&offset=0")
        assert len(payload["items"]) == 2 and payload["total"] == 3
        status, _, payload = call_app(
            app, "GET", "/api/meme-assets", query="limit=2&offset=2")
        assert len(payload["items"]) == 1
        status, _, payload = call_app(
            app, "GET", "/api/meme-assets", query="status=bogus")
        assert status.startswith("400")
    finally:
        store.close()


def test_asset_detail_image_and_thumb(tmp_path):
    store, service, app = wired(tmp_path)
    try:
        asset_id = service.query_meme_assets(search="study")["items"][0]["id"]
        status, _, payload = call_app(app, "GET", "/api/meme-assets/{}".format(asset_id))
        assert status.startswith("200")
        assert payload["asset"]["filename"] == "image_1.jpg"
        assert payload["asset"]["sentiment"] == "positive"
        assert payload["asset"]["responses"] == []
        status, headers, body = call_app(
            app, "GET", "/api/meme-assets/{}/thumb".format(asset_id))
        assert status.startswith("200")
        assert headers["Content-Type"] == "image/jpeg"
        assert isinstance(body, bytes) and len(body) > 0
        status, headers, body = call_app(
            app, "GET", "/api/meme-assets/{}/image".format(asset_id))
        assert status.startswith("200")
        assert isinstance(body, bytes) and len(body) > 0
        status, _, _ = call_app(app, "GET", "/api/meme-assets/does-not-exist")
        assert status.startswith("404")
        status, _, _ = call_app(app, "GET", "/api/meme-assets/does-not-exist/image")
        assert status.startswith("404")
    finally:
        store.close()


def test_favorite_tags_and_stats(tmp_path):
    store, service, app = wired(tmp_path)
    try:
        asset_id = service.query_meme_assets(search="study")["items"][0]["id"]
        status, _, payload = call_app(
            app, "POST", "/api/meme-assets/{}/favorite".format(asset_id),
            {"favorite": True})
        assert status.startswith("200")
        assert payload["asset"]["favorite"] is True
        status, _, payload = call_app(
            app, "POST", "/api/meme-assets/{}/tags".format(asset_id),
            {"tags": ["study", "exams"]})
        assert status.startswith("200")
        assert payload["asset"]["tags"] == ["study", "exams"]
        status, _, payload = call_app(app, "GET", "/api/meme-assets/stats")
        assert status.startswith("200")
        assert payload["assets"] == 3
        assert payload["favorites"] == 1
        assert payload["by_sentiment"]["positive"] == 1
        status, _, _ = call_app(
            app, "POST", "/api/meme-assets/missing/favorite", {"favorite": True})
        assert status.startswith("400")
    finally:
        store.close()


def test_response_crud_from_asset_and_test_delivery(tmp_path):
    store, service, app = wired(tmp_path)
    try:
        asset_id = service.query_meme_assets(search="doom")["items"][0]["id"]
        status, _, payload = call_app(app, "POST", "/api/responses", {
            "asset_id": asset_id, "kind": "MEME", "tone": "sarcastic",
            "title": "Bro", "body_template": "Back to {goal}.",
            "severity_min": 2, "severity_max": 4, "cooldown_seconds": 60.0})
        assert status.startswith("201")
        response_id = payload["response"]["id"]
        assert payload["response"]["asset_id"] == asset_id
        status, _, payload = call_app(
            app, "GET", "/api/meme-assets/{}/responses".format(asset_id))
        assert status.startswith("200")
        assert [row["id"] for row in payload["responses"]] == [response_id]
        status, _, payload = call_app(
            app, "POST", "/api/responses/{}".format(response_id),
            {"tone": "firm", "enabled": False})
        assert status.startswith("200")
        assert payload["response"]["tone"] == "firm"
        assert payload["response"]["enabled"] is False
        status, _, payload = call_app(
            app, "POST", "/api/responses/{}".format(response_id),
            {"tone": "spicy"})
        assert status.startswith("400")
        # Test delivery exercises the path with zero persistence.
        status, _, payload = call_app(
            app, "POST", "/api/responses/{}/test".format(response_id),
            {"context": {"goal": "Exams"}})
        assert status.startswith("200")
        assert payload["test"] is True
        assert payload["persisted"] is False
        assert payload["payload"]["image_url"].endswith("/image")
        assert store.query_interventions(limit=100000) == []
        assert store.get_response(response_id).times_shown == 0
        status, _, _ = call_app(app, "POST", "/api/responses", {"kind": "MEME"})
        assert status.startswith("400")
        status, _, _ = call_app(
            app, "POST", "/api/responses/missing/test", {})
        assert status.startswith("400")
    finally:
        store.close()


def test_ingest_endpoint_reports_stats(tmp_path):
    store, service, app = wired(tmp_path)
    try:
        images = service.meme_dataset_dir()
        csv_path = tmp_path / "labels.csv"
        status, _, payload = call_app(app, "POST", "/api/meme-assets/ingest", {
            "csv_path": str(csv_path), "images_dir": images,
            "make_thumbs": False})
        assert status.startswith("200")
        assert payload["csv_rows"] == 3
        assert payload["imported"] == 0
        assert payload["refreshed"] == 3
        status, _, _ = call_app(app, "POST", "/api/meme-assets/ingest", {})
        assert status.startswith("400")
    finally:
        store.close()
