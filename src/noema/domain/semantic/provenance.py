"""Provenance, freshness and semantic state for classification verdicts.

Three orthogonal questions, three separate answers:

* **Provenance** — how did this verdict come to be attached to this unit?
  Was a model asked about *this* evidence (DIRECT), or is it an episode
  verdict being shown on a member row (INHERITED), or a prior verdict reused
  because nothing material changed (REUSED), or the result of reconsidering
  a prior verdict against new evidence (RECLASSIFIED)?

* **Freshness** — how well does the verdict's evidence window still describe
  the unit's current extent? FRESH, AGING, STALE, or INVALIDATED.

* **Semantic state** — where is this episode in its interpretation lifecycle?
  UNKNOWN -> EMERGING -> STABLE -> CHANGING -> RECLASSIFYING -> STABLE.

None of these is confidence, and none of them is evidence quality.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

from noema.domain.activity import coerce_timestamp


class Provenance(str, Enum):
    """How a verdict came to be attached to the unit displaying it."""

    #: A model was asked about exactly this evidence.
    DIRECT = "direct"
    #: An episode verdict shown on a member row it genuinely covers.
    INHERITED = "inherited"
    #: A prior verdict kept because Stage A found no material change.
    REUSED = "reused"
    #: A prior verdict reconsidered against new evidence and re-decided.
    RECLASSIFIED = "reclassified"


class Freshness(str, Enum):
    """How well a verdict's evidence still describes the current unit."""

    #: The verdict's window covers essentially all current evidence.
    FRESH = "fresh"
    #: Meaningful new evidence exists but has not yet contradicted anything.
    AGING = "aging"
    #: Substantial evidence now lies outside the verdict's window.
    STALE = "stale"
    #: The verdict was explicitly superseded or contradicted.
    INVALIDATED = "invalidated"


class SemanticState(str, Enum):
    """Where an episode sits in its interpretation lifecycle."""

    UNKNOWN = "unknown"
    EMERGING = "emerging"
    STABLE = "stable"
    CHANGING = "changing"
    RECLASSIFYING = "reclassifying"


#: Fraction of the unit's current active time that must lie *outside* a
#: verdict's evidence window before the verdict stops being FRESH. Below
#: this, growth is ordinary heartbeat drift on the same activity.
AGING_UNCOVERED_FRACTION = 0.20

#: ...and the fraction at which it becomes STALE: a verdict that has seen
#: less than half of what the unit now contains is no longer a description
#: of that unit.
STALE_UNCOVERED_FRACTION = 0.50

#: Absolute floor. New evidence shorter than this never ages a verdict on
#: its own, regardless of fractions, so a 10-second tail cannot mark a
#: 30-minute verdict stale.
MIN_UNCOVERED_SECONDS = 45.0

#: Wall-clock age at which a verdict about a *closed* unit is reported as
#: aging even when its window still covers the unit. Knowledge has a date.
WALL_CLOCK_AGING_SECONDS = 30 * 60.0
WALL_CLOCK_STALE_SECONDS = 4 * 60 * 60.0


def assess_freshness(
    window: Optional[Any],
    unit_start: Any,
    unit_end: Any,
    unit_active_seconds: Optional[float] = None,
    classified_at: Optional[Any] = None,
    now: Optional[Any] = None,
    invalidated: bool = False,
) -> Freshness:
    """Grade how well ``window`` still describes ``[unit_start, unit_end]``.

    Deterministic and observation-only. A missing window (legacy rows) is
    reported STALE rather than FRESH: unknown scope is never assumed to be
    total scope.
    """

    if invalidated:
        return Freshness.INVALIDATED
    if window is None:
        return Freshness.STALE

    start = coerce_timestamp(unit_start)
    end = coerce_timestamp(unit_end)
    span = max(0.0, (end - start).total_seconds())
    covered = window.overlap_seconds(start, end)
    uncovered = max(0.0, span - covered)

    reference = float(unit_active_seconds) if unit_active_seconds else span
    reference = max(reference, 1.0)
    fraction = uncovered / reference

    freshness = Freshness.FRESH
    if uncovered >= MIN_UNCOVERED_SECONDS:
        if fraction >= STALE_UNCOVERED_FRACTION:
            freshness = Freshness.STALE
        elif fraction >= AGING_UNCOVERED_FRACTION:
            freshness = Freshness.AGING

    # Wall-clock age only ever *weakens* a verdict, never strengthens it.
    if classified_at is not None and freshness is Freshness.FRESH:
        current = coerce_timestamp(now) if now is not None else datetime.now(
            coerce_timestamp(classified_at).tzinfo)
        try:
            age = max(0.0, (current - coerce_timestamp(classified_at)).total_seconds())
        except (TypeError, ValueError):
            age = 0.0
        if age >= WALL_CLOCK_STALE_SECONDS:
            freshness = Freshness.STALE
        elif age >= WALL_CLOCK_AGING_SECONDS:
            freshness = Freshness.AGING
    return freshness


def is_presentable_as_current(freshness: Any) -> bool:
    """Whether a verdict of this freshness may be shown as a current fact."""

    try:
        value = Freshness(freshness)
    except (TypeError, ValueError):
        return False
    return value in {Freshness.FRESH, Freshness.AGING}


def next_semantic_state(
    current: Any,
    material_change: bool,
    verdict_changed: bool,
    has_verdict: bool,
) -> SemanticState:
    """Advance the episode lifecycle from an observed transition.

    The machine never forces a transition: an episode with no verdict stays
    UNKNOWN, and a stable episode with no material change stays STABLE.
    """

    try:
        state = SemanticState(current)
    except (TypeError, ValueError):
        state = SemanticState.UNKNOWN

    if not has_verdict:
        return SemanticState.UNKNOWN
    if verdict_changed:
        return SemanticState.RECLASSIFYING
    if material_change:
        return SemanticState.CHANGING
    if state in {SemanticState.UNKNOWN, SemanticState.EMERGING}:
        return SemanticState.EMERGING if state is SemanticState.UNKNOWN else SemanticState.STABLE
    if state in {SemanticState.CHANGING, SemanticState.RECLASSIFYING}:
        return SemanticState.STABLE
    return SemanticState.STABLE
