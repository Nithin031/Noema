"""Classification repository contract (moved interface, not implementation).

This module defines the narrow persistence surface the classification
subsystem requires. The SQLite implementation stays in
:mod:`noema.infrastructure.database.store`, which owns connections,
locking, transactions, and schema. This file owns the *contract*: the
exact operations classification code may rely on, with SQLite
compatibility verified by tests rather than by moving SQL.

Direction: classification defines what it needs; infrastructure
implements it. No SQL moves in Phase 2.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, runtime_checkable


@runtime_checkable
class ClassificationRepository(Protocol):
    """Persistence operations specific to semantic classification."""

    def insert_classification(self, classification: Any) -> bool:
        """Persist or refresh one verdict (archiving superseded rows)."""
        ...

    def insert_classifications(self, classifications: Iterable[Any]) -> int:
        """Persist many verdicts atomically; returns rows created."""
        ...

    def query_classifications(
        self,
        session_id: Optional[str] = None,
        category: Optional[str] = None,
        productivity: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Any]:
        """Stored verdicts, newest first."""
        ...

    def query_classification_map(self, session_ids: Iterable[str]) -> Dict[str, Any]:
        """Stored verdicts keyed by session id, one query."""
        ...

    def query_classification_revisions(
        self, session_id: str, limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Superseded verdicts for one session, newest first."""
        ...

    def classification_status_counts(self) -> Dict[str, int]:
        """Row counts per classification status."""
        ...

    def query_classification_batches(
        self, status: Optional[str] = None, limit: int = 100000,
    ) -> List[Dict[str, Any]]:
        """Scheduler batch rows for idempotent window claims."""
        ...

    def claim_classification_batches(self, limit: int = 100000) -> List[Dict[str, Any]]:
        """Atomically claim due scheduler batches."""
        ...

    def complete_classification_batch(self, batch_id: str) -> None:
        """Mark a scheduler batch complete."""
        ...

    def retry_classification_batch(self, batch_id: str, reason: str) -> None:
        """Return a scheduler batch to the queue with a reason."""
        ...

    def update_classification_batch_payload(
        self, batch_id: str, payload: Mapping[str, Any],
    ) -> None:
        """Attach window/session metadata to a scheduler batch row."""
        ...
