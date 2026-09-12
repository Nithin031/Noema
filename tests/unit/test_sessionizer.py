from noema.domain.activity import ActivityEvent
from noema.infrastructure.database import SQLiteStore
from noema.domain.sessions import Sessionizer


def make_event(timestamp, duration, app="Chrome", domain=None, title=None, event_id=None):
    return ActivityEvent(
        timestamp=timestamp,
        duration=duration,
        device="laptop",
        app=app,
        domain=domain,
        title=title,
        bucket_id="window_laptop",
        source_event_id=event_id,
    )


def test_sessionizer_groups_heartbeats_and_splits_contexts_and_gaps():
    events = [
        make_event("2026-09-04T10:00:20Z", 10, title="reward.py", event_id="3"),
        make_event("2026-09-04T10:00:00Z", 10, title="main.py", event_id="1"),
        make_event("2026-09-04T10:00:15Z", 2, title="main.py", event_id="2"),
        make_event("2026-09-04T10:00:31Z", 3, domain="reddit.com", event_id="4"),
        make_event("2026-09-04T10:02:32Z", 3, domain="reddit.com", event_id="5"),
    ]

    sessions = Sessionizer(max_gap_seconds=60).sessionize(events)

    assert len(sessions) == 3
    assert sessions[0].app == "Chrome"
    assert sessions[0].event_count == 3
    assert sessions[0].title == "main.py"
    assert sessions[1].domain == "reddit.com"
    assert sessions[1].event_count == 1
    assert sessions[2].event_count == 1
    assert sessions[2].start.isoformat() == "2026-09-04T10:02:32+00:00"


def test_sessions_are_persisted_and_recomputed_idempotently():
    store = SQLiteStore()
    events = [
        make_event("2026-09-04T10:00:00Z", 10, app="Code", event_id="1"),
        make_event("2026-09-04T10:00:20Z", 10, app="Code", event_id="2"),
    ]
    sessions = Sessionizer().sessionize(events)

    assert store.insert_sessions(sessions) == 1
    assert store.insert_sessions(sessions) == 0
    found = store.query_sessions(start="2026-09-04T09:59:00Z")
    assert len(found) == 1
    assert found[0].id == sessions[0].id
    assert found[0].duration == 30
    assert store.count_sessions() == 1
    store.close()


def test_browser_titles_split_sessions_when_domain_is_unavailable():
    events = [
        make_event("2026-09-06T10:00:00Z", 20, title="Inbox - NITK - Mozilla Firefox", event_id="1"),
        make_event("2026-09-06T10:00:25Z", 20, title="Instagram - Mozilla Firefox", event_id="2"),
    ]

    sessions = Sessionizer().sessionize(events)

    assert len(sessions) == 2
    assert sessions[0].title.startswith("Inbox")
    assert sessions[1].title.startswith("Instagram")
