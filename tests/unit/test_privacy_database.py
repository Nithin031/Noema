from noema.domain.activity import ActivityEvent
from noema.infrastructure.database import SQLiteStore
from noema.domain.privacy import PrivacyFilter, PrivacyPolicy


def event(**kwargs):
    values = {
        "timestamp": "2026-09-04T10:00:00Z",
        "duration": 60,
        "device": "laptop",
        "app": "Chrome",
        "title": "Research",
        "domain": "example.com",
        "url": "https://example.com/page?token=secret#fragment",
        "bucket_id": "web_laptop",
        "source_event_id": "1",
    }
    values.update(kwargs)
    return ActivityEvent(**values)


def test_privacy_filter_discards_sensitive_events_and_sanitizes_allowed_urls():
    policy = PrivacyPolicy(blocked_domains={"bank.example"})
    privacy = PrivacyFilter(policy)

    assert privacy.evaluate(event(app="Bitwarden")).reason == "blocked_app"
    assert privacy.evaluate(event(domain="login.bank.example")).reason == "blocked_domain"
    assert privacy.evaluate(event(metadata={"incognito": True})).reason == "private_window"

    allowed = privacy.evaluate(event())
    assert allowed.allowed is True
    assert allowed.event.url == "https://example.com/page"


def test_sqlite_store_is_idempotent_and_queryable():
    store = SQLiteStore()
    first = event()
    second = event(
        timestamp="2026-09-04T10:05:00Z",
        source_event_id="2",
        domain="docs.example.com",
    )

    assert store.insert_event(first) is True
    assert store.insert_event(first) is False
    assert store.insert_many([first, second]) == 1
    assert store.count() == 2
    found = store.query(start="2026-09-04T10:01:00Z", domain="docs.example.com")
    assert len(found) == 1
    assert found[0].event.source_event_id == "2"
    store.close()
