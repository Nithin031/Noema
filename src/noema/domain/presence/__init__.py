"""User-presence detection: ACTIVE vs AFK vs UNKNOWN.

Presence answers "was the user interacting?" from input-timing signals
(ActivityWatch afk-watcher rows, explicit browser interactions). It never
reads window content, keystrokes, or coordinates, and it never classifies:
AFK is an operational state, not productive/distractive/neutral.
"""

from .detector import PresenceDetector, afk_status, is_presence_event
from .models import PresenceInterval, PresenceState, PresenceTimeline

__all__ = [
    "PresenceDetector",
    "PresenceInterval",
    "PresenceState",
    "PresenceTimeline",
    "afk_status",
    "is_presence_event",
]
