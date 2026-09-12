"""Named grouping entry point for integrations that prefer a grouper API."""

from __future__ import annotations

from .engine import MeaningfulSessionEngine


class MeaningfulSessionGrouper(MeaningfulSessionEngine):
    """Compatibility name for the semantic grouping engine."""

    pass
