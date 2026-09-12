"""ActivityWatch telemetry adapter — the only module that speaks AW."""

from .adapter import ActivityWatchAdapter, ActivityWatchClient, source_event_kind

__all__ = ["ActivityWatchAdapter", "ActivityWatchClient", "source_event_kind"]
