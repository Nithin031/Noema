"""V3 Phase 10 Meme Center backend tests.

Fixture dataset (5 CSV rows + 5 generated images) lives in tmp_path —
tests never touch the real 6,992-image corpus.
"""

import csv
import os

import pytest

from noema.application.meme_assets import (
    ingest_dataset,
    parse_dataset_csv,
    resolve_image_path,
    validate_row,
)
from noema.domain.meme import MemeAsset, asset_id_for
from noema.domain.response import Response
from noema.infrastructure.database import SQLiteStore


def make_image(path, color=(200, 30, 30), size=(64, 48)):
    from PIL import Image

    image = Image.new("RGB", size, color)
    image.save(path)


@pytest.fixture()
def corpus(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    rows = [
        # number, image_name, ocr, corrected, sentiment
        ("0", "image_1.jpg", "STUDY HARD EXAMS", "Study hard, exams soon", "positive"),
        ("1", "image_2.png", "", "", "neutral"),
        ("2", "image_3.jpg", "TIRED <b>broken", "Tired today", "negative"),
        ("3", "image_4.jpg", "DEADLINE http://example.com/x", "Deadline week", "very_negative"),
        ("4", "image_5.jpg", "KEEP GOING", "Keep going", "bogus-label"),
    ]
    for number, name, _ocr, _corrected, _sentiment in rows:
        make_image(str(images / name))
    csv_path = tmp_path / "labels.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["number", "image_name", "text_ocr", "text_corrected",
                         "overall_sentiment"])
        writer.writerows(rows)
    # malformed extra line: missing image file + unknown sentiment covered
    # by row 4 (bogus-label) and the absent file check below.
    return csv_path, images


def test_csv_parsing_and_row_validation(corpus):
    csv_path, images = corpus
    rows, problems = parse_dataset_csv(str(csv_path))
    assert len(rows) == 5  # 1. dataset row parsing
    assert problems == []
    fields, problem = validate_row(rows[0], str(images))
    assert problem is None  # 2. image resolution
    assert fields["filename"] == "image_1.jpg"
    assert fields["sentiment"] == "positive"  # 6. sentiment parsing
    assert fields["ocr_text"] == "STUDY HARD EXAMS"  # 7. OCR parsing
    assert fields["width"] == 64
    bogus, problem = validate_row(rows[4], str(images))
    assert bogus is None and "unknown sentiment" in problem
    missing, problem = validate_row(
        {"number": "9", "image_name": "nope.jpg", "text_ocr": "",
         "text_corrected": "", "overall_sentiment": "neutral"},
        str(images))
    assert missing is None and "missing image file" in problem  # 3.
    assert resolve_image_path(str(images), "../evil.jpg") is None
    assert resolve_image_path(str(images), "image_1.jpg") is not None


def test_ingest_is_idempotent_and_reports(corpus, tmp_path):
    csv_path, images = corpus
    store = SQLiteStore()
    try:
        thumbs = str(tmp_path / "thumbs")
        first = ingest_dataset(str(csv_path), str(images), store,
                               thumbs_dir=thumbs)
        assert first["csv_rows"] == 5
        assert first["matched"] == 4  # bogus-sentiment row is invalid
        assert first["invalid"] == 1
        assert first["missing"] == 0
        assert first["imported"] == 4
        assert first["refreshed"] == 0
        second = ingest_dataset(str(csv_path), str(images), store,
                                thumbs_dir=thumbs)
        assert second["imported"] == 0  # 5. idempotent: still 4 assets
        assert second["refreshed"] == 4
        assert store.meme_asset_stats()["assets"] == 4
        assert len([name for name in os.listdir(thumbs) if name.endswith(".jpg")]) == 4
    finally:
        store.close()


def test_asset_queries_search_filter_favorite(corpus):
    csv_path, images = corpus
    store = SQLiteStore()
    try:
        ingest_dataset(str(csv_path), str(images), store, thumbs_dir=None,
                       make_thumbs=False)
        found = store.query_meme_assets(search="study")  # 9. asset search
        assert found["total"] == 1
        assert found["items"][0]["filename"] == "image_1.jpg"
        assert found["items"][0]["display_text"] == "Study hard, exams soon"
        assert found["items"][0]["curated"] is False
        assert store.query_meme_assets(sentiment="neutral")["total"] == 1  # 10.
        assert store.query_meme_assets(sentiment="positive")["total"] == 1
        assert store.query_meme_assets(status="favorite")["total"] == 0
        asset_id = found["items"][0]["id"]
        assert store.set_asset_favorite(asset_id, True)  # 11. favorite
        assert store.query_meme_assets(status="favorite")["total"] == 1
        assert store.get_meme_asset(asset_id).favorite is True
        assert store.set_asset_tags(asset_id, ["Study", "EXAMS"])  # 12. curation tags
        assert store.get_meme_asset(asset_id).tags == ("study", "exams")
        assert store.query_meme_assets(search="exams")["total"] == 1
        assert store.query_meme_assets(status="curated")["total"] == 0
        assert store.query_meme_assets(status="not_curated")["total"] == 4
    finally:
        store.close()


def test_response_association_and_creation_from_asset(corpus):
    csv_path, images = corpus
    store = SQLiteStore()
    try:
        from noema.api import NoemaService
        from noema.infrastructure.activity_sources.activitywatch import (
            ActivityWatchAdapter,
        )

        ingest_dataset(str(csv_path), str(images), store, thumbs_dir=None,
                       make_thumbs=False)
        service = NoemaService(ActivityWatchAdapter(), store)
        asset_id = store.query_meme_assets(search="study")["items"][0]["id"]
        created = service.create_response_from_asset(asset_id, {  # 14.
            "kind": "MEME", "tone": "sarcastic", "title": "Bro",
            "body_template": "Back to {goal}.", "severity_min": 2,
            "severity_max": 4, "cooldown_seconds": 60.0})
        assert created["asset_id"] == asset_id  # 13. response association
        assert created["kind"] == "MEME"
        assert store.query_meme_assets(status="curated")["total"] == 1
        linked = store.asset_responses(asset_id)
        assert [item.id for item in linked] == [created["id"]]
        # Same asset backs a second response (13: one asset, many responses).
        second = service.create_response_from_asset(asset_id, {
            "kind": "MEME", "tone": "gentle", "title": "Noema",
            "body_template": "Easy does it.", "cooldown_seconds": 60.0})
        assert second["id"] != created["id"]
        assert len(store.asset_responses(asset_id)) == 2
        # 15. response editing via validated update path.
        updated = service.update_response(created["id"], {
            "tone": "firm", "severity_min": 3, "enabled": False})
        assert updated["tone"] == "firm"
        assert updated["enabled"] is False
        # 16. disabled responses are excluded from normal selection.
        assert all(item.id != created["id"]
                   for item in store.query_responses())
        assert any(item.id == created["id"]
                   for item in store.query_responses(enabled_only=False))
    finally:
        store.close()


def test_test_delivery_writes_nothing(corpus):
    csv_path, images = corpus
    store = SQLiteStore()
    try:
        from noema.api import NoemaService
        from noema.infrastructure.activity_sources.activitywatch import (
            ActivityWatchAdapter,
        )

        ingest_dataset(str(csv_path), str(images), store, thumbs_dir=None,
                       make_thumbs=False)
        service = NoemaService(ActivityWatchAdapter(), store)
        asset_id = store.query_meme_assets(search="study")["items"][0]["id"]
        created = service.create_response_from_asset(asset_id, {
            "kind": "MEME", "tone": "sarcastic", "title": "Bro",
            "body_template": "Back to {goal}.", "cooldown_seconds": 60.0})
        before = {
            "interventions": len(store.query_interventions(limit=100000)),
            "outcomes": len(store.query_outcomes(limit=100000)),
            "detections": len(store.query_detections(limit=100000)),
            "invocations": len(store.telemetry_repository().query_invocations(limit=100000))
            if service.telemetry is not None else 0,
        }
        receipt = service.test_response_delivery(  # 18. test isolation
            created["id"], {"goal": "Exams", "minutes_away": 28})
        assert receipt["test"] is True
        assert receipt["persisted"] is False
        assert receipt["payload"]["response_id"] == created["id"]
        assert receipt["payload"]["image_url"].endswith("/image")
        after = {
            "interventions": len(store.query_interventions(limit=100000)),
            "outcomes": len(store.query_outcomes(limit=100000)),
            "detections": len(store.query_detections(limit=100000)),
        }
        assert after == {key: before[key] for key in after}  # 19. effectiveness isolation
        # Counters untouched: no shown/clicked/recovery from tests.
        row = store.get_response(created["id"])
        assert (row.times_shown, row.times_clicked, row.recovery_count) == (0, 0, 0)
    finally:
        store.close()


def test_asset_id_stability_and_provenance(corpus):
    csv_path, images = corpus
    first = asset_id_for("meme-dataset-v1", "image_1.jpg")
    assert first == asset_id_for("meme-dataset-v1", "image_1.jpg")
    assert first != asset_id_for("meme-dataset-v1", "image_2.jpg")
    assert first != asset_id_for("meme-dataset-v2", "image_1.jpg")
    store = SQLiteStore()
    try:
        ingest_dataset(str(csv_path), str(images), store, thumbs_dir=None,
                       make_thumbs=False)
        asset = store.get_meme_asset(first)
        assert asset is not None  # 21. provenance preservation
        assert asset.source == "meme-dataset-v1"
        assert asset.source_ref == "image_1.jpg"
        assert asset.filename == "image_1.jpg"
        # Re-ingestion preserves user curation (favorite/tags/enabled).
        store.set_asset_favorite(first, True)
        store.set_asset_tags(first, ["keep"])
        second = __import__("noema.application.meme_assets", fromlist=["ingest_dataset"]).ingest_dataset(
            str(csv_path), str(images), store, thumbs_dir=None, make_thumbs=False)
        assert second["imported"] == 0
        kept = store.get_meme_asset(first)
        assert kept is not None and kept.favorite is True
        assert kept.tags == ("keep",)
    finally:
        store.close()


def test_migration_creates_asset_table_on_existing_db(tmp_path):
    from noema.infrastructure.database import SQLiteStore

    db = str(tmp_path / "existing.sqlite3")  # 20. migration
    store = SQLiteStore(db)
    store.seed_responses()
    store._connection.execute("DROP TABLE meme_assets")
    store._connection.execute(
        "INSERT INTO responses (id, kind) VALUES ('legacy-resp', 'TEXT')")
    store.close()
    reopened = SQLiteStore(db)
    try:
        tables = {row["name"] for row in reopened._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "meme_assets" in tables
        # Pre-existing rows survive the migration.
        assert reopened.get_response("legacy-resp") is not None
        assert reopened.meme_asset_stats()["assets"] == 0
    finally:
        reopened.close()
