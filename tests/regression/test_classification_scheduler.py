"""Scheduler-driver equivalence tests (Phase 2 extraction, Step 11).

The helpers in :mod:`noema.classification.scheduler` are a verbatim
move of the daemon's job-boundary math. These tests pin the preserved
semantics: 20-minute window + future-cursor clamp, completed 5-minute
bucket, deterministic batch ids, chronological dedup merge, and
newest-first max-per-run bounding.
"""

from datetime import datetime, timedelta, timezone

from noema.classification.scheduler import (
    build_batch_id,
    classification_window,
    completed_window,
    deduplicate_candidates,
    select_chunk,
)


class _Item:
    def __init__(self, item_id, start):
        self.id = item_id
        self.start_time = start


def _at(hour, minute):
    return datetime(2026, 9, 17, hour, minute, tzinfo=timezone.utc)


def test_classification_window_defaults_to_one_interval():
    now = _at(10, 20)
    start, end = classification_window(None, now, 1200.0)
    assert (end, start) == (now, now - timedelta(seconds=1200))


def test_classification_window_clamps_future_cursor():
    now = _at(10, 20)
    start, end = classification_window(now + timedelta(hours=1), now, 1200.0)
    assert (end, start) == (now, now - timedelta(seconds=1200))


def test_completed_window_is_prior_five_minute_bucket():
    now = _at(10, 23, )
    start, end = completed_window(now)
    assert end == _at(10, 20)
    assert start == _at(10, 15)


def test_build_batch_id_deterministic_and_prefixed():
    first = build_batch_id(_at(10, 15), _at(10, 20))
    assert first == build_batch_id(_at(10, 15), _at(10, 20))
    assert first.startswith("batch_")
    assert build_batch_id(_at(10, 20), _at(10, 25)) != first


def test_deduplicate_candidates_merges_chronologically_without_dupes():
    primary = [_Item("b", _at(10, 5)), _Item("a", _at(10, 0))]
    overflow = [_Item("b", _at(10, 5)), _Item("c", _at(9, 0))]
    merged = deduplicate_candidates(primary, overflow)
    assert [item.id for item in merged] == ["a", "b", "c"]


def test_select_chunk_newest_first_bounded():
    items = [_Item("a", _at(9, 0)), _Item("b", _at(10, 0)), _Item("c", _at(10, 10))]
    assert [item.id for item in select_chunk(items, 2)] == ["c", "b"]
    assert [item.id for item in select_chunk(items, 60)] == ["c", "b", "a"]
