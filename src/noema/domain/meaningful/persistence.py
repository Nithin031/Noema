"""Persistence boundary for semantic sessions.

The concrete SQLite implementation remains in ``noema.infrastructure.database`` so
all application-layer tables share one connection and transaction policy.
"""

from __future__ import annotations

from typing import Any, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from noema.infrastructure.database.store import SQLiteStore

from .models import MeaningfulSession


class MeaningfulSessionPersistence:
    """Small repository facade that keeps semantic persistence swappable."""

    def __init__(self, store: Any):
        self.store = store

    def save(self, sessions: Iterable[MeaningfulSession]) -> int:
        return self.store.insert_meaningful_sessions(sessions)

    def query(self, *args: Any, **kwargs: Any):
        return self.store.query_meaningful_sessions(*args, **kwargs)
