"""Shared, dependency-light views over raw and meaningful sessions.

The downstream generations accept both session contracts while Gen 1.5 is
being rolled out.  Keeping this adapter duck-typed avoids an import cycle
between the meaningful-session package and the older raw-session modules.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional


def is_meaningful_session(session: Any) -> bool:
    return hasattr(session, "activity_session_ids") and hasattr(session, "primary_topic")


def search_values(session: Any) -> List[str]:
    """Return observable text fields suitable for matching or model context."""

    if is_meaningful_session(session):
        values: Iterable[Any] = (
            session.primary_project,
            session.primary_task,
            session.primary_topic,
            session.dominant_category,
            session.dominant_activity_type,
            *session.activities,
            session.summary if isinstance(session.summary, str) else None,
        )
    else:
        values = (session.app, session.domain, session.url, session.title)
    return [str(value) for value in values if value]


def session_label(session: Any, default: str = "this activity") -> str:
    """Choose a safe human-readable label without inventing semantics."""

    if is_meaningful_session(session):
        for value in (
            session.primary_task,
            session.primary_topic,
            session.primary_project,
            session.dominant_category,
        ):
            if value:
                return str(value)
    else:
        for value in (session.domain, session.app, session.title):
            if value:
                return str(value)
    return default


def has_observable_activity(session: Any) -> bool:
    return bool(search_values(session))


def browser_name(session: Any) -> Optional[str]:
    """Return a normalized browser name when the session identifies one."""

    explicit = getattr(session, "browser", None)
    if explicit:
        return str(explicit).strip().casefold()
    app = str(getattr(session, "app", None) or "").strip().casefold()
    known = {
        "firefox": "firefox", "mozilla firefox": "firefox",
        "chrome": "chrome", "google chrome": "chrome",
        "edge": "edge", "microsoft edge": "edge",
        "safari": "safari", "brave": "brave", "opera": "opera",
    }
    return known.get(app)


def browser_target(session: Any) -> dict:
    """Build an exact browser target, omitting unavailable identifiers."""

    target = {}
    device_set = getattr(session, "device_set", ())
    device = getattr(session, "device", None) or (next(iter(device_set), None) if device_set else None)
    browser = browser_name(session)
    window_id = getattr(session, "browser_window_id", None)
    tab_id = getattr(session, "browser_tab_id", None)
    if device:
        target["device"] = str(device)
    if browser:
        target["browser"] = browser
    if window_id:
        target["window_id"] = str(window_id)
    if tab_id:
        target["tab_id"] = str(tab_id)
    return target
