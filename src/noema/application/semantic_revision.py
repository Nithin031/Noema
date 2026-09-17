"""Orchestration of evidence-bounded classification and revision.

This is the seam between the domain rules (``noema.domain.semantic``) and the
pipeline. It owns four jobs:

1. Stamp every verdict with the evidence window it actually describes.
2. Run Stage A on grown episodes and decide whether to spend a model call.
3. Fold a revision into segments without rewriting the earlier claim.
4. Emit the observability record that answers "why did Noema call the model
   here?" and, just as importantly, "why did it not?".

It deliberately contains no model client and no SQL. The pipeline supplies
both, so this stays testable without a database or a provider.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from noema.domain.activity import coerce_timestamp
from noema.domain.semantic import (
    ChangeAssessment,
    EvidenceChangeDetector,
    EvidenceWindow,
    Provenance,
    RevisionDecision,
    RevisionOutcome,
    SemanticSegment,
    SemanticState,
    build_segments,
    next_semantic_state,
)


logger = logging.getLogger("noema.semantic.revision")


#: Categories that represent a positive semantic claim. Only these can be
#: "contradicted" or "reaffirmed"; uncertainty is neither.
_CLAIMS = frozenset({"productive", "distractive"})


@dataclass(frozen=True)
class EvidenceBundle:
    """Everything known about one episode's current evidence."""

    episode_id: str
    start: datetime
    end: datetime
    observations: Tuple[Mapping[str, Any], ...] = ()
    active_seconds: float = 0.0

    @property
    def window(self) -> EvidenceWindow:
        return EvidenceWindow.from_observations(
            self.start, self.end, list(self.observations), self.active_seconds)


def bundle_for_episode(
    episode: Any,
    observations: Optional[Sequence[Mapping[str, Any]]] = None,
) -> EvidenceBundle:
    """Assemble the evidence bundle describing an episode's current extent."""

    start = getattr(episode, "start_time", None) or getattr(episode, "start")
    end = getattr(episode, "end_time", None) or getattr(episode, "end")
    try:
        active = float(getattr(episode, "active_duration_seconds", 0) or 0)
    except (TypeError, ValueError):
        active = 0.0
    if not active:
        try:
            active = float(getattr(episode, "duration", 0) or 0)
        except (TypeError, ValueError):
            active = 0.0
    return EvidenceBundle(
        episode_id=str(getattr(episode, "id", "")),
        start=coerce_timestamp(start),
        end=coerce_timestamp(end),
        observations=tuple(item for item in (observations or ()) if isinstance(item, Mapping)),
        active_seconds=active,
    )


def stamp_evidence_window(
    verdict: Any,
    window: EvidenceWindow,
    provenance: str = Provenance.DIRECT.value,
    revision: int = 1,
    semantic_state: str = SemanticState.EMERGING.value,
    parent_classification_id: Optional[str] = None,
    revision_reason: Optional[str] = None,
) -> Any:
    """Bind a verdict to the evidence it was formed from.

    Applied to every classification result before persistence. A verdict that
    reaches storage without a window would be a verdict of unbounded scope,
    which is the defect this release exists to remove — so failures keep a
    window too, describing what was attempted.
    """

    return replace(
        verdict,
        evidence_start=window.start,
        evidence_end=window.end,
        evidence_digest=window.digest,
        evidence_observation_count=window.observation_count,
        evidence_active_seconds=window.active_seconds,
        provenance=provenance,
        revision=max(1, int(revision)),
        semantic_state=semantic_state,
        parent_classification_id=parent_classification_id,
        revision_reason=revision_reason,
    )


class SemanticRevisionPlanner:
    """Decide, locally and for free, what each episode needs next.

    ``plan`` never calls a model. It returns a decision that tells the caller
    whether a model call is warranted, and carries the full reasoning either
    way so the choice is auditable from telemetry alone.
    """

    def __init__(self, detector: Optional[EvidenceChangeDetector] = None):
        self.detector = detector or EvidenceChangeDetector()

    def plan(
        self,
        bundle: EvidenceBundle,
        stored_verdict: Optional[Any],
    ) -> RevisionDecision:
        """Stage A: is reconsideration of this episode's verdict warranted?"""

        prior_window = getattr(stored_verdict, "evidence_window", None) if stored_verdict else None
        has_verdict = bool(stored_verdict is not None
                           and getattr(stored_verdict, "is_verdict", False))

        # A verdict stored before evidence windows existed has unrecorded
        # scope. Eligibility and presentation answer that differently, and
        # deliberately so:
        #
        #   eligibility   "spend quota re-examining this?"  -> no: nothing
        #                 observable says it changed, and re-queueing every
        #                 historical row on upgrade is a stampede, not an
        #                 improvement.
        #   presentation  "show this as a current fact?"    -> only inside a
        #                 proven window, and it has none (``covers`` returns
        #                 False), so it renders as unscoped rather than as a
        #                 judgment about rows it may never have seen.
        #
        # Operators who do want historical rows re-read have the existing,
        # explicit lever: a classifier-version bump.
        if has_verdict and prior_window is None:
            return RevisionDecision(
                outcome=RevisionOutcome.RETAINED,
                reason="verdict predates evidence windows; no observable "
                       "change to act on (scope stays unrecorded)",
                semantic_state=getattr(stored_verdict, "semantic_state",
                                       SemanticState.UNKNOWN.value),
                provenance=Provenance.REUSED.value,
                revision=int(getattr(stored_verdict, "revision", 1) or 1),
                model_called=False,
            )

        assessment = self.detector.assess(
            prior_window if has_verdict else None,
            stored_verdict if has_verdict else None,
            list(bundle.observations),
            current_start=bundle.start,
            current_end=bundle.end,
            current_active_seconds=bundle.active_seconds,
        )

        if not has_verdict:
            return RevisionDecision(
                outcome=RevisionOutcome.FIRST,
                reason=assessment.reason,
                semantic_state=SemanticState.EMERGING.value,
                provenance=Provenance.DIRECT.value,
                revision=1,
                model_called=True,
                assessment=assessment,
            )

        if not assessment.should_reconsider:
            state = next_semantic_state(
                getattr(stored_verdict, "semantic_state", SemanticState.UNKNOWN.value),
                material_change=False, verdict_changed=False, has_verdict=True)
            # Stage A did not merely fail to find a change — it checked each
            # new observation for application transitions, domain
            # transitions, topical novelty, entropy shift and contradiction,
            # and found none. That is a positive finding of continuity, and
            # it is a defensible basis for the verdict to cover the new
            # evidence without another model call.
            #
            # This is emphatically NOT the original defect. That one extended
            # coverage with zero examination. This extends it only after a
            # per-observation consistency check, records provenance as
            # ``reused`` so the surface can say the model did not re-examine
            # it, and still ages on wall-clock time so a long-unexamined
            # verdict stops reading as current.
            extend_to = None
            if (prior_window is not None
                    and assessment.new_observation_count
                    and assessment.examined):
                extend_to = EvidenceWindow(
                    start=min(prior_window.start, bundle.start),
                    end=max(prior_window.end, bundle.end),
                    observation_count=len(bundle.observations),
                    active_seconds=bundle.active_seconds,
                    digest=bundle.window.digest,
                )
            return RevisionDecision(
                outcome=RevisionOutcome.RETAINED,
                reason=assessment.reason,
                semantic_state=state,
                provenance=Provenance.REUSED.value,
                revision=int(getattr(stored_verdict, "revision", 1) or 1),
                model_called=False,
                assessment=assessment,
                extend_to=extend_to,
            )

        return RevisionDecision(
            outcome=RevisionOutcome.REVISED,
            reason=assessment.reason,
            semantic_state=SemanticState.CHANGING.value,
            provenance=Provenance.RECLASSIFIED.value,
            revision=int(getattr(stored_verdict, "revision", 1) or 1) + 1,
            model_called=True,
            assessment=assessment,
        )

    def apply(
        self,
        bundle: EvidenceBundle,
        stored_verdict: Optional[Any],
        decision: RevisionDecision,
        new_verdict: Optional[Any],
        existing_segments: Optional[Sequence[SemanticSegment]] = None,
    ) -> Tuple[Any, RevisionDecision]:
        """Stage B follow-through: fold a model answer into the lineage.

        Returns the verdict to persist and the completed decision. A model
        answer that failed validation never becomes a verdict: the prior
        verdict is preserved with its own window intact.
        """

        window = bundle.window
        prior_window = getattr(stored_verdict, "evidence_window", None) if stored_verdict else None

        # --- the model produced nothing usable ---------------------------
        if new_verdict is None or not getattr(new_verdict, "is_verdict", False):
            if stored_verdict is not None and getattr(stored_verdict, "is_verdict", False):
                # Preserve the prior verdict *and its original window*. It
                # remains a true claim about what it saw; it simply does not
                # extend over the evidence the failed call was meant to read.
                kept = replace(
                    stored_verdict,
                    semantic_state=SemanticState.CHANGING.value,
                    revision_reason="reconsideration failed; prior verdict retained within its own window",
                )
                return kept, replace(
                    decision,
                    outcome=RevisionOutcome.RETAINED,
                    reason="model reconsideration produced no valid verdict; "
                           "prior verdict kept, scope unchanged",
                    semantic_state=SemanticState.CHANGING.value,
                    provenance=Provenance.REUSED.value,
                    revision=int(getattr(stored_verdict, "revision", 1) or 1),
                )
            return new_verdict, replace(
                decision,
                outcome=RevisionOutcome.UNCERTAIN,
                reason="no valid verdict produced and none existed before",
                semantic_state=SemanticState.UNKNOWN.value,
            )

        # --- first classification ---------------------------------------
        if stored_verdict is None or not getattr(stored_verdict, "is_verdict", False):
            stamped = stamp_evidence_window(
                new_verdict, window,
                provenance=Provenance.DIRECT.value,
                revision=1,
                semantic_state=SemanticState.EMERGING.value,
                revision_reason=decision.reason,
            )
            return stamped, replace(
                decision, outcome=RevisionOutcome.FIRST,
                semantic_state=SemanticState.EMERGING.value,
                provenance=Provenance.DIRECT.value, revision=1)

        # --- reconsidered: did the verdict actually change? --------------
        changed = (
            str(getattr(new_verdict, "category", "")).casefold()
            != str(getattr(stored_verdict, "category", "")).casefold()
        )
        revision = int(getattr(stored_verdict, "revision", 1) or 1) + 1

        if not changed:
            # Reaffirmed. The verdict now legitimately covers the larger
            # window, because a model was actually shown that evidence.
            stamped = stamp_evidence_window(
                new_verdict, window,
                provenance=Provenance.RECLASSIFIED.value,
                revision=revision,
                semantic_state=SemanticState.STABLE.value,
                parent_classification_id=getattr(stored_verdict, "session_id", None),
                revision_reason="reaffirmed against new evidence: " + decision.reason,
            )
            return stamped, replace(
                decision, outcome=RevisionOutcome.REAFFIRMED,
                semantic_state=SemanticState.STABLE.value,
                provenance=Provenance.RECLASSIFIED.value, revision=revision,
                segments=tuple(existing_segments or ()))

        # --- genuinely revised: segment rather than rewrite --------------
        new_window = EvidenceWindow.from_observations(
            bundle.start, bundle.end, list(bundle.observations), bundle.active_seconds)
        segments = build_segments(
            bundle.episode_id, stored_verdict, prior_window,
            new_verdict, new_window,
            existing_segments=existing_segments,
            observations=list(bundle.observations),
        )
        stamped = stamp_evidence_window(
            new_verdict, window,
            provenance=Provenance.RECLASSIFIED.value,
            revision=revision,
            semantic_state=SemanticState.RECLASSIFYING.value,
            parent_classification_id=getattr(stored_verdict, "session_id", None),
            revision_reason="revised from {!r} to {!r}: {}".format(
                getattr(stored_verdict, "category", "?"),
                getattr(new_verdict, "category", "?"),
                decision.reason),
        )
        return stamped, replace(
            decision, outcome=RevisionOutcome.REVISED,
            semantic_state=SemanticState.RECLASSIFYING.value,
            provenance=Provenance.RECLASSIFIED.value, revision=revision,
            segments=segments)


def log_decision(
    log: Any,
    episode_id: str,
    decision: RevisionDecision,
    bundle: Optional[EvidenceBundle] = None,
) -> Dict[str, Any]:
    """Emit the record that makes model spending auditable.

    Answers both directions of the question: when a call happened, which
    signal bought it; when it did not, which floor blocked it.
    """

    assessment = decision.assessment
    record = {
        "episode_id": episode_id,
        "outcome": decision.outcome.value,
        "model_called": decision.model_called,
        "model_call_reason": decision.reason if decision.model_called else None,
        "model_skipped_reason": None if decision.model_called else decision.reason,
        "semantic_state": decision.semantic_state,
        "provenance": decision.provenance,
        "revision": decision.revision,
        "segment_count": len(decision.segments),
        "fired_signals": list(assessment.fired) if assessment else [],
        "new_active_seconds": round(assessment.new_active_seconds, 1) if assessment else 0.0,
        "new_observation_count": assessment.new_observation_count if assessment else 0,
    }
    if bundle is not None:
        record["evidence_start"] = bundle.start.isoformat().replace("+00:00", "Z")
        record["evidence_end"] = bundle.end.isoformat().replace("+00:00", "Z")
        record["evidence_active_seconds"] = round(bundle.active_seconds, 1)
    if log is not None:
        try:
            log(record)
        except Exception:  # observability must never break the pipeline
            logger.debug("semantic revision logging failed", exc_info=True)
    return record


def extend_window(verdict: Any, window: EvidenceWindow, reason: str) -> Any:
    """Widen a verdict's evidence window after a verified continuity check.

    Only the scope, provenance and reason change: the category, confidence
    and revision number are untouched, because nothing re-decided them. The
    verdict now says "this claim covers this larger window, and the model
    was not re-asked", which is exactly what happened.
    """

    return replace(
        verdict,
        evidence_start=window.start,
        evidence_end=window.end,
        evidence_digest=window.digest,
        evidence_observation_count=window.observation_count,
        evidence_active_seconds=window.active_seconds,
        provenance=Provenance.REUSED.value,
        revision_reason="window extended without re-asking the model: " + reason,
    )
