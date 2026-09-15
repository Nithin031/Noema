"""Meme candidate retrieval: deterministic, bounded, sentiment-as-feature."""

from noema.application.meme_decision import (
    MEME_CANDIDATE_LIMIT,
    MIN_DIRECT_SAMPLES,
    retrieve_meme_candidates,
    score_meme_candidate,
)
from noema.domain.meme import MemeAsset, asset_id_for
from noema.domain.response import Response
from noema.infrastructure.database import SQLiteStore


def _asset(filename, ocr="", sentiment="neutral", tags=(), favorite=False,
           enabled=True, response_count=0):
    return {
        "id": asset_id_for("test-corpus", filename),
        "filename": filename,
        "ocr_text": ocr,
        "corrected_text": "",
        "sentiment": sentiment,
        "tags": list(tags),
        "favorite": favorite,
        "enabled": enabled,
        "response_count": response_count,
    }


def _store_with(*assets):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    store = SQLiteStore()
    for index, asset in enumerate(assets):
        store.upsert_meme_asset(MemeAsset(
            id=asset["id"], source="test-corpus", filename=asset["filename"],
            source_ref=asset["filename"],
            ocr_text=asset["ocr_text"] or None,
            corrected_text=asset.get("corrected_text") or None,
            sentiment=asset["sentiment"], tags=tuple(asset["tags"]),
            favorite=asset["favorite"], enabled=asset["enabled"],
            created_at=now))
    return store


def test_retrieval_scores_keyword_overlap_first():
    store = _store_with(
        _asset("procrastination_dog.jpg", ocr="stop procrastinating now",
               sentiment="negative", tags=["procrastination"]),
        _asset("sunset.jpg", ocr="beautiful evening sky", sentiment="positive"),
    )
    try:
        results = retrieve_meme_candidates(
            store, meme_intent="procrastination callout", tone="sarcastic")
        assert [item["id"] for item in results] == [
            asset_id_for("test-corpus", "procrastination_dog.jpg"),
            asset_id_for("test-corpus", "sunset.jpg"),
        ]
        assert all(set(item) >= {"id", "filename", "ocr_snippet", "sentiment",
                                 "tags", "favorite", "score", "effectiveness"}
                   for item in results)
        # Metadata only: no image bytes in candidate dicts.
        assert all("bytes" not in item and "image" not in item for item in results)
    finally:
        store.close()


def test_retrieval_bounded_and_deterministic():
    assets = [_asset("m{:04d}.jpg".format(index), ocr="lorem ipsum filler")
              for index in range(40)]
    store = _store_with(*assets)
    try:
        first = retrieve_meme_candidates(store, meme_intent="study", limit=10)
        second = retrieve_meme_candidates(store, meme_intent="study", limit=10)
        assert len(first) == 10
        assert [item["id"] for item in first] == [item["id"] for item in second]
        assert len(retrieve_meme_candidates(store, limit=5000)) == 40
    finally:
        store.close()


def test_disabled_assets_never_retrieved():
    store = _store_with(
        _asset("off.jpg", ocr="study now", enabled=False),
        _asset("on.jpg", ocr="unrelated clouds"),
    )
    try:
        results = retrieve_meme_candidates(store, meme_intent="study now")
        assert [item["filename"] for item in results] == ["on.jpg"]
    finally:
        store.close()


def test_sentiment_is_feature_not_verdict():
    # A negative-sentiment asset with strong keyword overlap still wins
    # over a positive one with none: sentiment never excludes.
    store = _store_with(
        _asset("neg.jpg", ocr="stop procrastinating", sentiment="negative"),
        _asset("pos.jpg", ocr="kittens playing", sentiment="positive"),
    )
    try:
        results = retrieve_meme_candidates(
            store, meme_intent="procrastinating", tone="playful")
        assert results[0]["filename"] == "neg.jpg"
    finally:
        store.close()


def test_favorite_and_curated_boost_but_do_not_decide():
    store = _store_with(
        _asset("plain.jpg", ocr="stop procrastinating"),
        _asset("fav.jpg", ocr="weather today", favorite=True),
    )
    try:
        results = retrieve_meme_candidates(store, meme_intent="procrastinating")
        assert results[0]["filename"] == "plain.jpg"
    finally:
        store.close()


def test_insufficient_effectiveness_stays_neutral():
    asset = _asset("new.jpg", ocr="stop procrastinating")
    assert score_meme_candidate(asset, ["procrastinating"], "playful",
                                {"times_shown": 1, "recovery_rate": 1.0}) == \
        score_meme_candidate(asset, ["procrastinating"], "playful",
                             {"times_shown": 4, "recovery_rate": 0.0})
    assert score_meme_candidate(asset, ["procrastinating"], "playful", None) == \
        score_meme_candidate(asset, ["procrastinating"], "playful",
                             {"times_shown": 4, "recovery_rate": 0.0})


def test_sufficient_effectiveness_influences_score():
    asset = _asset("proven.jpg", ocr="stop procrastinating")
    low = score_meme_candidate(asset, ["procrastinating"], "playful",
                               {"times_shown": 12, "recovery_rate": 0.1})
    high = score_meme_candidate(asset, ["procrastinating"], "playful",
                                {"times_shown": 12, "recovery_rate": 0.9})
    assert high > low
    # Malformed rates degrade to neutral, never crash.
    assert score_meme_candidate(asset, ["procrastinating"], "playful",
                                {"times_shown": 12, "recovery_rate": "bogus"}) == \
        score_meme_candidate(asset, ["procrastinating"], "playful", None)


def test_retrieval_without_store_returns_empty():
    assert retrieve_meme_candidates(object(), meme_intent="study") == []


def test_effectiveness_joins_responses_by_asset():
    store = _store_with(_asset("a.jpg", ocr="stop procrastinating"))
    try:
        asset_id = asset_id_for("test-corpus", "a.jpg")
        store.insert_response(Response(
            id="resp-1", kind="MEME", tone="playful", title="T",
            body_template="B", asset_id=asset_id))
        for _ in range(MIN_DIRECT_SAMPLES):
            store.record_response_shown("resp-1")
        store.record_response_recovered("resp-1")
        results = retrieve_meme_candidates(store, meme_intent="procrastinating")
        assert len(results) == 1
        assert results[0]["effectiveness"]["shown"] == MIN_DIRECT_SAMPLES
        assert results[0]["effectiveness"]["direct"] == 1
    finally:
        store.close()
