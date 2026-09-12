from datetime import timezone

from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter


def test_adapter_normalizes_activitywatch_browser_event():
    bucket = {
        "id": "aw-watcher-web_hostname",
        "type": "web.tab.current",
        "hostname": "laptop",
        "client": "aw-watcher-web",
    }
    event = {
        "id": 42,
        "timestamp": "2026-09-04T10:20:00+05:30",
        "duration": 12.5,
        "data": {
            "title": "PPO Algorithms",
            "url": "https://www.arxiv.org/abs/1234",
            "audible": False,
        },
    }

    normalized = ActivityWatchAdapter().normalize_event(event, bucket)

    assert normalized.timestamp.tzinfo == timezone.utc
    assert normalized.timestamp.isoformat() == "2026-09-04T04:50:00+00:00"
    assert normalized.device == "laptop"
    assert normalized.title == "PPO Algorithms"
    assert normalized.domain == "arxiv.org"
    assert normalized.bucket_id == "aw-watcher-web_hostname"
    assert normalized.source_event_id == "42"
    assert normalized.metadata["audible"] is False


def test_adapter_accepts_activitywatch_bucket_map():
    buckets = {
        "window_laptop": {
            "type": "currentwindow",
            "hostname": "laptop",
            "events": [
                {
                    "timestamp": 1788497400,
                    "duration": 1,
                    "data": {"app": "Code", "title": "reward.py"},
                }
            ],
        }
    }

    events = list(ActivityWatchAdapter().iter_buckets(buckets))

    assert len(events) == 1
    assert events[0].app == "Code"
    assert events[0].bucket_id == "window_laptop"


def test_adapter_preserves_optional_browser_identity():
    normalized = ActivityWatchAdapter().normalize_event({
        "id": "browser-1",
        "timestamp": "2026-09-04T10:20:00Z",
        "duration": 2,
        "data": {
            "app": "Firefox",
            "title": "Reddit",
            "url": "https://reddit.com/r/test",
            "browser": "firefox",
            "browser_window_id": 17,
            "browser_tab_id": 42,
        },
    }, {"id": "web", "hostname": "laptop"})

    assert normalized.browser == "firefox"
    assert normalized.browser_window_id == "17"
    assert normalized.browser_tab_id == "42"
