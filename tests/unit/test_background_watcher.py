"""Background watcher: accumulates per-app open/audio seconds, ignores noise, stores no titles."""

import json
from datetime import datetime, timezone

from noema.infrastructure.activity_sources.native_background import BackgroundWatcher
from noema.infrastructure.database import SQLiteStore

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def _watcher(open_apps, audio, foreground="brave", interval=15):
    store = SQLiteStore()
    return store, BackgroundWatcher(
        store, interval_seconds=interval, open_provider=lambda: open_apps,
        audio_provider=lambda: audio, foreground_provider=lambda: foreground)


def test_accumulates_open_and_audio_seconds_and_excludes_the_foreground_app():
    store, watcher = _watcher([("brave", "Secret tab"), ("Spotify", "Song - Artist"), ("Slack", "chat")], {"Spotify"})
    assert watcher.tick(NOW, monotonic=0.0) is True
    assert watcher.tick(NOW, monotonic=5.0) is False      # rate limited
    assert watcher.tick(NOW, monotonic=20.0) is True
    rows = {r["app"]: r for r in watcher.today(NOW)}
    assert "brave" not in rows                              # foreground app is the foreground timeline's job
    assert rows["Spotify"]["audio_minutes"] > 0 and rows["Spotify"]["open_minutes"] > 0
    assert rows["Slack"]["audio_minutes"] == 0 and rows["Slack"]["open_minutes"] > 0
    assert watcher.today(NOW)[0]["app"] == "Spotify"         # audio ranks first
    store.close()


def test_titles_are_never_stored():
    store, watcher = _watcher([("Spotify", "Very Private Song Title")], set())
    watcher.tick(NOW, monotonic=0.0)
    stored = store.get_state(watcher._day_key(NOW))
    assert "Very Private" not in stored and json.loads(stored).keys() == {"Spotify"}
    store.close()


def test_unsupported_audio_is_reported_not_faked():
    store, watcher = _watcher([("Slack", "chat")], None)
    watcher.tick(NOW, monotonic=0.0)
    assert watcher.status(NOW)["now"]["audio_supported"] is False
    store.close()


def test_provider_failure_never_raises():
    store = SQLiteStore()
    watcher = BackgroundWatcher(store, open_provider=lambda: 1 / 0, audio_provider=lambda: set(),
                                foreground_provider=lambda: None)
    assert watcher.tick(NOW, monotonic=0.0) is True
    assert "ZeroDivisionError" in watcher.last_error
    store.close()
