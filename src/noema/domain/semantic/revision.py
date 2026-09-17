"""How a verdict changes when the episode it describes grows.

Stage A (``change.py``) decides whether to look. This module decides what to
do with what was found, and it is the piece that makes the original failure
structurally impossible.

The governing rule is that revising a verdict does **not** rewrite history.
The 0.78 PRODUCTIVE verdict formed at 21:16 was a true statement about
21:16-21:20. It stays true of that interval forever. What was wrong was
letting it also speak for 21:20-21:36, which it never saw. So a revision
keeps the prior verdict bound to its own window and adds a new segment for
the new evidence:

    before:  [======== PRODUCTIVE 0.78 ========]   (claimed 21:16-21:36)
    after:   [ PROD 0.78 ][ UNCERTAIN ][ DISTR 0.91 ]

Resolution is then purely temporal: a row at 21:34 asks "whose window
contains me?" and gets segment C or nothing at all. There is no code path by
which it can receive segment A's verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from noema.domain.activity import coerce_timestamp

from .change import ChangeAssessment
from .evidence_window import EvidenceWindow, observation_digest
from .provenance import (
    Freshness,
    Provenance,
    SemanticState,
    assess_freshness,
    is_presentable_as_current,
)
from .segment import SemanticSegment


class RevisionOutcome(str, Enum):
    """What the pipeline decided to do with an existing verdict."""

    #: Stage A found nothing material; the verdict stands untouched.
    RETAINED = "retained"
    #: Reconsidered and confirmed; the window extends over the new evidence.
    REAFFIRMED = "reaffirmed"
    #: Reconsidered and re-decided; prior verdict keeps its own window.
    REVISED = "revised"
    #: Evidence no longer supports any verdict for the new interval.
    UNCERTAIN = "uncertain"
    #: Nothing existed before; this is a first classification.
    FIRST = "first"


@dataclass(frozen=True)
class RevisionDecision:
    """The full, explainable result of reconsidering one episode."""

    outcome: RevisionOutcome
    reason: str
    semantic_state: str = SemanticState.UNKNOWN.value
    provenance: str = Provenance.DIRECT.value
    revision: int = 1
    model_called: bool = False
    assessment: Optional[ChangeAssessment] = None
    segments: Tuple[SemanticSegment, ...] = ()
    #: Set when Stage A verified the new evidence carries no material
    #: semantic change. The verdict's window may then be extended over that
    #: evidence WITHOUT a model call — see ``extend_window``.
    extend_to: Optional[EvidenceWindow] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "semantic_state": self.semantic_state,
            "provenance": self.provenance,
            "revision": self.revision,
            "model_called": self.model_called,
            "window_extended": self.extend_to is not None,
            "assessment": self.assessment.to_dict() if self.assessment else None,
            "segments": [segment.to_dict() for segment in self.segments],
        }


@dataclass(frozen=True)
class ResolvedVerdict:
    """A verdict resolved for one specific interval, with its lineage.

    This is what presentation layers render. ``verdict`` may be ``None``,
    which means exactly what it says: Noema has no verdict covering this
    interval. That is a legitimate, displayable answer and is never
    substituted with a neighbouring verdict.
    """

    verdict: Optional[Any]
    provenance: str
    freshness: str
    source_id: Optional[str] = None
    segment_id: Optional[str] = None
    reason: str = ""

    @property
    def has_verdict(self) -> bool:
        return self.verdict is not None

    @property
    def presentable_as_current(self) -> bool:
        """Whether this may be shown as a current fact about the interval."""

        return self.has_verdict and is_presentable_as_current(self.freshness)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provenance": self.provenance,
            "freshness": self.freshness,
            "source_id": self.source_id,
            "segment_id": self.segment_id,
            "reason": self.reason,
            "presentable_as_current": self.presentable_as_current,
        }


#: Returned when nothing covers the interval. Deliberately a verdict-free
#: value rather than a neutral verdict: "I have no claim about this" and
#: "I claim this was neutral" are different statements.
NO_VERDICT = ResolvedVerdict(
    verdict=None,
    provenance=Provenance.DIRECT.value,
    freshness=Freshness.STALE.value,
    reason="no verdict's evidence window covers this interval",
)


def resolve_for_interval(
    start: Any,
    end: Any,
    episode_verdict: Optional[Any] = None,
    segments: Optional[Sequence[SemanticSegment]] = None,
    episode_id: Optional[str] = None,
    unit_start: Any = None,
    unit_end: Any = None,
    unit_active_seconds: Optional[float] = None,
    episode_start: Any = None,
    episode_end: Any = None,
    direct_verdict: Optional[Any] = None,
    now: Optional[Any] = None,
    tolerance_seconds: float = 2.0,
) -> ResolvedVerdict:
    """Resolve which verdict, if any, legitimately describes an interval.

    Precedence:

    1. A verdict classified directly for this unit.
    2. The segment whose evidence window covers the interval.
    3. The episode verdict, but **only if its evidence window covers the
       interval**. This single condition is the structural fix: an episode
       verdict can no longer reach evidence it never saw.
    4. Nothing. Reported honestly as "no verdict", never as neutral.

    ``unit_start``/``unit_end`` scope the freshness judgement and default to
    the interval itself: a verdict that genuinely covers 21:16-21:20 is a
    fresh claim about 21:18 even while the *episode-level* claim has gone
    stale. Those are different statements and are reported separately.
    ``episode_start``/``episode_end`` bound the legacy fallback only.
    """

    interval_start = coerce_timestamp(start)
    interval_end = coerce_timestamp(end) if end is not None else interval_start

    # 1. A direct verdict about exactly this unit always wins.
    if direct_verdict is not None and getattr(direct_verdict, "is_verdict", False):
        freshness = direct_verdict.freshness_for(
            unit_start if unit_start is not None else interval_start,
            unit_end if unit_end is not None else interval_end,
            unit_active_seconds=unit_active_seconds, now=now)
        return ResolvedVerdict(
            verdict=direct_verdict,
            provenance=Provenance.DIRECT.value,
            freshness=freshness,
            source_id=getattr(direct_verdict, "session_id", None),
            reason="classified directly for this unit",
        )

    # 2. The segment that actually covers this interval.
    covering: List[SemanticSegment] = [
        segment for segment in (segments or ())
        if segment.is_verdict and segment.covers(
            interval_start, interval_end, tolerance_seconds=tolerance_seconds)
    ]
    if covering:
        # Most specific wins: the narrowest window that still contains the
        # interval is the closest observation of it.
        segment = min(covering, key=lambda item: item.duration)
        return ResolvedVerdict(
            verdict=segment,
            provenance=Provenance.INHERITED.value,
            freshness=Freshness.FRESH.value,
            source_id=segment.episode_id,
            segment_id=segment.id,
            reason="covered by semantic segment {} of episode {}".format(
                segment.sequence, segment.episode_id),
        )

    # 3. The episode verdict, only within its own evidence window.
    if episode_verdict is not None and getattr(episode_verdict, "is_verdict", False):
        if episode_verdict.covers(interval_start, interval_end,
                                  tolerance_seconds=tolerance_seconds):
            freshness = episode_verdict.freshness_for(
                unit_start if unit_start is not None else interval_start,
                unit_end if unit_end is not None else interval_end,
                unit_active_seconds=unit_active_seconds, now=now)
            return ResolvedVerdict(
                verdict=episode_verdict,
                provenance=Provenance.INHERITED.value,
                freshness=freshness,
                source_id=episode_id or getattr(episode_verdict, "session_id", None),
                reason="inside the evidence window of episode verdict {}".format(
                    episode_id or getattr(episode_verdict, "session_id", "")),
            )
        window = episode_verdict.evidence_window
        if window is not None:
            # A recorded window that does not contain this interval is a
            # definite negative: the model was never shown this evidence, so
            # there is no verdict about it. This is the rule that makes the
            # original fan-out impossible.
            return ResolvedVerdict(
                verdict=None,
                provenance=Provenance.INHERITED.value,
                freshness=Freshness.STALE.value,
                source_id=episode_id or getattr(episode_verdict, "session_id", None),
                reason="episode verdict's evidence covers {}-{} and not this interval".format(
                    window.start.strftime("%H:%M:%S"), window.end.strftime("%H:%M:%S")),
            )

        # No recorded window at all: a verdict stored before windows existed.
        # Blanking it would erase a user's history over a bookkeeping gap, so
        # it still resolves — but bounded to the episode's own extent, and
        # never as a current fact. The UI shows it with its scope explicitly
        # unrecorded and stale, which is the honest reading.
        bound_start = episode_start if episode_start is not None else unit_start
        bound_end = episode_end if episode_end is not None else unit_end
        if bound_start is not None and bound_end is not None:
            inside = (coerce_timestamp(bound_start) <= interval_start
                      and interval_end <= coerce_timestamp(bound_end))
        else:
            inside = True
        if inside:
            return ResolvedVerdict(
                verdict=episode_verdict,
                provenance=Provenance.INHERITED.value,
                freshness=Freshness.STALE.value,
                source_id=episode_id or getattr(episode_verdict, "session_id", None),
                reason="episode verdict predates evidence windows; scope unrecorded, "
                       "shown bounded to the episode and never as current",
            )
        return ResolvedVerdict(
            verdict=None,
            provenance=Provenance.INHERITED.value,
            freshness=Freshness.STALE.value,
            source_id=episode_id or getattr(episode_verdict, "session_id", None),
            reason="interval lies outside the episode this verdict belongs to",
        )

    return NO_VERDICT


def build_segments(
    episode_id: str,
    prior_verdict: Optional[Any],
    prior_window: Optional[EvidenceWindow],
    new_verdict: Optional[Any],
    new_window: Optional[EvidenceWindow],
    existing_segments: Optional[Sequence[SemanticSegment]] = None,
    observations: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Tuple[SemanticSegment, ...]:
    """Fold a revision into an episode's segment list without rewriting it.

    The prior verdict keeps its own window; the new verdict claims only the
    interval the prior one never saw. Segments are contiguous, ordered, and
    non-overlapping.
    """

    segments: List[SemanticSegment] = list(existing_segments or ())

    def _members(window: Optional[EvidenceWindow]) -> Tuple[str, ...]:
        if window is None:
            return ()
        found = []
        for item in observations or ():
            if not isinstance(item, Mapping):
                continue
            stamp = item.get("end") or item.get("timestamp") or item.get("start")
            identifier = item.get("session_id") or item.get("id")
            if identifier and stamp is not None:
                try:
                    if window.covers(stamp, tolerance_seconds=1.0):
                        found.append(str(identifier))
                except (TypeError, ValueError):
                    continue
        return tuple(dict.fromkeys(found))

    def _segment(sequence: int, verdict: Any, window: EvidenceWindow,
                 provenance: str, state: str) -> SemanticSegment:
        return SemanticSegment(
            episode_id=episode_id,
            sequence=sequence,
            start_time=window.start,
            end_time=window.end,
            category=getattr(verdict, "category", "uncertain"),
            productivity=getattr(verdict, "productivity", "neutral"),
            confidence=getattr(verdict, "confidence", 0.0),
            activity=getattr(verdict, "activity", None),
            signal=getattr(verdict, "signal", None),
            topic=getattr(verdict, "topic", None),
            activity_type=getattr(verdict, "activity_type", None),
            evidence_quality=getattr(verdict, "evidence_quality", None),
            evidence_start=window.start,
            evidence_end=window.end,
            evidence_digest=window.digest,
            evidence_observation_count=window.observation_count,
            evidence_active_seconds=window.active_seconds,
            provenance=provenance,
            semantic_state=state,
            classification_status=getattr(verdict, "classification_status", "pending"),
            activity_session_ids=_members(window),
            provider=getattr(verdict, "provider", None),
            model=getattr(verdict, "model", None),
            prompt_version=getattr(verdict, "prompt_version", None),
            classifier_version=getattr(verdict, "classifier_version", None),
        )

    # Seed the list with the prior verdict bound to its own window, so the
    # earlier claim survives a revision instead of being overwritten.
    if not segments and prior_verdict is not None and prior_window is not None:
        segments.append(_segment(
            0, prior_verdict, prior_window,
            Provenance.DIRECT.value, SemanticState.STABLE.value))

    if new_verdict is None or new_window is None:
        return tuple(segments)

    # The new verdict claims only what the prior one did not cover.
    claim_start = new_window.start
    if segments:
        latest_end = max(segment.end_time for segment in segments)
        if latest_end > claim_start:
            claim_start = latest_end
    if new_window.end <= claim_start:
        # Nothing genuinely new to claim; the existing segments stand.
        return tuple(segments)

    bounded = EvidenceWindow(
        start=claim_start,
        end=new_window.end,
        observation_count=new_window.observation_count,
        active_seconds=new_window.active_seconds,
        digest=new_window.digest,
    )
    segments.append(_segment(
        len(segments), new_verdict, bounded,
        Provenance.RECLASSIFIED.value if prior_verdict is not None else Provenance.DIRECT.value,
        SemanticState.RECLASSIFYING.value if prior_verdict is not None else SemanticState.EMERGING.value,
    ))
    return tuple(segments)
