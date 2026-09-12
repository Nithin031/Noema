"""Compatibility name for the canonical meaningful-session merger."""

from .engine import MeaningfulSessionEngine


class MeaningfulSessionMerger(MeaningfulSessionEngine):
    """Named facade for callers that describe grouping as a merge operation."""


__all__ = ["MeaningfulSessionMerger"]
