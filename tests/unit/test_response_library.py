"""V3 response library tests: registry, selection, persistence, seeding."""

from datetime import datetime, timedelta, timezone

from noema.domain.response import (
    Response,
    seed_responses,
    select_response,
)
from noema.infrastructure.database import SQLiteStore


def _rebuild(item, **overrides):
    data = item.to_dict()
    data["context_tags"] = tuple(data["context_tags"])
    del data["recovery_rate"]
    data.update(overrides)
    return Response(**data)


def _registry():
    return [Response(
        id=row["id"], kind=row["kind"], tone=row.get("tone", "gentle"),
        title=row.get("title", "Noema"), body_template=row.get("body_template", ""),
        asset_id=row.get("asset_id"), severity_min=row.get("severity_min", 1),
        severity_max=row.get("severity_max", 5),
        context_tags=tuple(row.get("context_tags", ())),
        cooldown_seconds=row.get("cooldown_seconds", 3600.0),
    ) for row in seed_responses()]


def test_seed_rows_are_valid_and_stable():
    rows = seed_responses()
    assert len(rows) >= 8
    ids = [row["id"] for row in rows]
    assert len(set(ids)) == len(ids)
    kinds = {row["kind"] for row in rows}
    assert {"MEME", "CHARACTER", "TEXT", "NUDGE", "CHALLENGE", "ENCOURAGEMENT"} <= kinds


def test_response_validation_rejects_bad_rows():
    import pytest
    with pytest.raises(ValueError):
        Response(id="", kind="MEME")
    with pytest.raises(ValueError):
        Response(id="x", kind="VIDEO")
    with pytest.raises(ValueError):
        Response(id="x", kind="MEME", tone="spicy")
    with pytest.raises(ValueError):
        Response(id="x", kind="MEME", severity_min=4, severity_max=2)


def test_render_fills_only_known_slots_and_never_raises():
    response = Response(id="r", kind="TEXT", body_template="Back to {goal} ({minutes_away}m).")
    rendered = response.render(goal="Power Electronics", minutes_away=28.0)
    assert rendered["body"] == "Back to Power Electronics (28m)."
    weird = Response(id="w", kind="TEXT", body_template="Brace {oops} alone.")
    assert weird.render()["body"] == "Brace {oops} alone."


def test_selection_prefers_exact_class_and_least_shown():
    registry = _registry()
    selected = select_response(response_class="MEME", mode="MEME", severity=3,
                               registry=registry)
    assert selected.fallback is False
    assert selected.response.kind == "MEME"
    # Least-shown exploration: bump one candidate, the other wins.
    shown = [item for item in registry if item.kind == "MEME"]
    assert len(shown) >= 1
    bumped = [_rebuild(item, times_shown=50) if item.id == shown[0].id else item
              for item in registry]
    if len(shown) > 1:
        reselected = select_response(response_class="MEME", mode="MEME",
                                     severity=3, registry=bumped)
        assert reselected.response.id != shown[0].id


def test_selection_respects_severity_mode_and_cooldown():
    registry = _registry()
    now = datetime.now(timezone.utc)
    # Gentle class under NOTIFICATION mode resolves to TEXT/NUDGE/ENCOURAGEMENT.
    gentle = select_response(response_class="GENTLE", mode="NOTIFICATION",
                             severity=2, registry=registry)
    assert gentle.fallback is False
    assert gentle.response.kind in {"TEXT", "NUDGE", "ENCOURAGEMENT"}
    # MEME class under NOTIFICATION mode has no servable kind → fallback.
    mismatch = select_response(response_class="MEME", mode="NOTIFICATION",
                               severity=3, registry=registry)
    assert mismatch.fallback is True
    assert mismatch.response is None
    # NONE class never selects.
    none = select_response(response_class="NONE", mode="MEME", severity=3,
                           registry=registry)
    assert none.fallback is True
    # Cooldown: mark every MEME-kind row shown now → fallback.
    cooled = [_rebuild(item, last_shown_at=now.isoformat()) if item.kind == "MEME" else item
              for item in registry]
    assert select_response(response_class="MEME", mode="MEME", severity=3,
                           registry=cooled, now=now).fallback is True
    # Disabled rows never serve.
    disabled = [_rebuild(item, enabled=False) for item in registry]
    assert select_response(response_class="GENTLE", mode="NOTIFICATION",
                           severity=2, registry=disabled).fallback is True


def test_store_seeds_idempotently_and_tracks_counters():
    store = SQLiteStore()
    try:
        first = store.seed_responses()
        assert first >= 8
        assert store.seed_responses() == 0
        rows = store.query_responses()
        assert len(rows) == first
        assert all(row.enabled for row in rows)
        target = rows[0].id
        store.record_response_shown(target)
        store.record_response_clicked(target)
        store.record_response_recovered(target)
        updated = store.get_response(target)
        assert updated.times_shown == 1
        assert updated.times_clicked == 1
        assert updated.recovery_count == 1
        assert updated.last_shown_at is not None
        table = store.response_effectiveness()
        assert table[0]["recovery_rate"] == 1.0
        assert table[0]["id"] == target
    finally:
        store.close()


def test_intervention_feedback_validates_and_separates_types():
    import pytest
    store = SQLiteStore()
    try:
        row = store.insert_intervention_feedback("int-1", "INTERPRETATION", "WRONG")
        assert row["value"] == "WRONG"
        row2 = store.insert_intervention_feedback("int-1", "USEFULNESS", "HELPFUL")
        assert row2["value"] == "HELPFUL"
        # Re-record overwrites the same (intervention, type, source).
        row3 = store.insert_intervention_feedback("int-1", "INTERPRETATION", "CORRECT")
        assert row3["value"] == "CORRECT"
        rows = store.query_intervention_feedback("int-1")
        assert len(rows) == 2
        with pytest.raises(ValueError):
            store.insert_intervention_feedback("int-1", "MOOD", "HAPPY")
        with pytest.raises(ValueError):
            store.insert_intervention_feedback("int-1", "INTERPRETATION", "MAYBE")
    finally:
        store.close()
