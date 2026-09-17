"""Verdict-extension licensing predicate and epistemic provenance.

Core invariant
--------------
``extend(verdict, new_interval)`` is licensed ONLY when ALL hold:

* sufficient_evidence — the new slice is identifiable and substantive.
* positive_continuity — at least REQUIRED_POSITIVE_SIGNALS distinct
  positive signals corroborate continuation (never one signal alone,
  never mere absence of contradiction).
* temporal_continuity — the slice lies within the episode span (bounded).
* no_material_contradiction — no deterministic-policy veto fires.
* same_semantic_context — implied by the positive signals referencing the
  verdict's own evidence text; the gate never consults grown episode
  membership as corroboration.

It must NOT be ``sufficient_evidence AND NOT contradiction`` — that is
precisely the original fan-out bug restated.

Epistemic labels
----------------
* DIRECT — the model directly evaluated this unit's evidence.
* VERIFIED_EXTENSION — the model did not see this slice, but deterministic
  local analysis positively verified semantic continuity. The inherited
  category may be shown; the confidence stays the verdict's own and must
  be labeled as episode-evidence confidence, never fresh certainty.
* INHERITED — structural membership display (a row belongs to a classified
  episode) without a coverage claim. Home: session listings whose rows
  carry explicit lineage.
* REUSED — an event re-displays its own session's verdict (same evidence,
  no new judgment implied). Home: raw-event listings.
* STALE — RESERVED. No read-path producer exists today: candidate
  producers either rewrite history (contract-rot relabeling) or duplicate
  UNKNOWN's meaning (lapsed coverage already surfaces as pending). A
  future scheduler-driven verdict-aging feature owns this label.
* examined — True only when the assessor had enough evidence to evaluate
  at all. examined=False (insufficient evidence) can never license.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from noema.classification import continuity as _continuity

#: Distinct positive signals required to license extension. Two, so no
#: single signal (one app, one keyword, one tab) can ever license alone.
REQUIRED_POSITIVE_SIGNALS = 2

#: Temporal bound (seconds) around the episode span a slice may lie in.
#: Absorbs boundary slicing; membership itself is established structurally.
EPISODE_SPAN_TOLERANCE_SECONDS = 300.0


class ExtensionProvenance:
    """Epistemic provenance values for verdict display."""

    DIRECT = "direct"
    VERIFIED_EXTENSION = "verified_extension"
    INHERITED = "inherited"
    REUSED = "reused"
    STALE = "stale"
    NONE = "none"

    ALL = frozenset({DIRECT, VERIFIED_EXTENSION, INHERITED, REUSED, STALE, NONE})


@dataclass(frozen=True)
class ExtensionAssessment:
    """Outcome of licensing one slice against one verdict."""

    licensed: bool
    examined: bool
    provenance: str
    positive_hits: Tuple[str, ...] = ()
    category: str = "insufficient_evidence"
    reasons: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "licensed": self.licensed,
            "examined": bool(self.examined),
            "provenance": self.provenance,
            "positive_hits": list(self.positive_hits),
            "category": self.category,
            "reasons": list(self.reasons),
        }


def _as_moment(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            from noema.domain.activity import coerce_timestamp

            return coerce_timestamp(value)
        except (AttributeError, TypeError, ValueError):
            return None
    return None


def assess_extension(
    evidence: Mapping[str, Any],
    verdict: Mapping[str, Any],
    episode: Mapping[str, Any],
    member_apps: Set[str],
    member_domains: Set[str],
) -> ExtensionAssessment:
    """Decide whether ``verdict`` may cover ``evidence``. Pure function.

    ``evidence``: app/title/domain/url/seconds/start/end (+window_id/tab_id).
    ``verdict``: category/productivity/topic/activity.
    ``episode``: primary_task/primary_topic/start/end/browser ids.
    Never raises on malformed input: doubt fails closed to unlicensed.
    """
    reasons: List[str] = []

    # -- sufficient_evidence -------------------------------------------
    identity = _continuity.evidence_identity(evidence)
    try:
        seconds = float(evidence.get("seconds") or 0.0)
    except (TypeError, ValueError):
        seconds = 0.0
    if not identity or seconds < _continuity.MIN_SLICE_SECONDS:
        return ExtensionAssessment(
            licensed=False, examined=False,
            provenance=ExtensionProvenance.NONE,
            category="insufficient_evidence",
            reasons=tuple(["missing identifiable evidence: {}".format(identity or "none")]),
        )

    # -- temporal_continuity -------------------------------------------
    seg_start = _as_moment(evidence.get("start") or evidence.get("timestamp"))
    seg_end = _as_moment(evidence.get("end"))
    ep_start = _as_moment(episode.get("start") or episode.get("start_time"))
    ep_end = _as_moment(episode.get("end") or episode.get("end_time"))
    if seg_start is None or seg_end is None or ep_start is None or ep_end is None:
        reasons.append("unparseable timestamps; cannot establish continuity")
        return ExtensionAssessment(
            licensed=False, examined=True,
            provenance=ExtensionProvenance.NONE,
            category="insufficient_evidence", reasons=tuple(reasons),
        )
    tolerance = EPISODE_SPAN_TOLERANCE_SECONDS
    try:
        in_span = (
            (seg_start - ep_start).total_seconds() >= -tolerance
            and (seg_end - ep_end).total_seconds() <= tolerance
        )
    except (TypeError, OverflowError):
        in_span = False
    if not in_span:
        return ExtensionAssessment(
            licensed=False, examined=True,
            provenance=ExtensionProvenance.NONE,
            category="insufficient_evidence",
            reasons=tuple(["slice outside episode span tolerance"]),
        )

    # -- no_material_contradiction (veto BEFORE counting positives) -----
    # Note: there is deliberately no wall-clock or contract-version expiry
    # here. A verdict describes the evidence it saw forever; historical
    # timelines must keep working, and live staleness is caught by the
    # continuity and contradiction conjuncts (a prototype version gate was
    # rejected after it turned every pre-bump timeline unclassified).
    veto = _continuity.material_contradiction(evidence, verdict.get("productivity"))
    if veto is not None:
        return ExtensionAssessment(
            licensed=False, examined=True,
            provenance=ExtensionProvenance.NONE,
            category="strong_contradiction",
            reasons=tuple(["material contradiction: {}".format(veto)]),
        )

    # -- positive_continuity (at least two DISTINCT signals) ------------
    hits = _continuity.positive_signals(evidence, verdict, episode, member_apps, member_domains)
    matched = sorted(name for name, hit in hits.items() if hit)
    if len(matched) < REQUIRED_POSITIVE_SIGNALS:
        # Absence of contradiction is explicitly NOT enough: a slice with
        # full identity but no positive corroboration fails closed.
        return ExtensionAssessment(
            licensed=False, examined=True,
            provenance=ExtensionProvenance.NONE,
            positive_hits=tuple(matched),
            category="absence_of_contradiction",
            reasons=tuple([
                "only {} of {} required positive signals matched{}".format(
                    len(matched), REQUIRED_POSITIVE_SIGNALS,
                    " ({})".format(", ".join(matched)) if matched else "",
                ),
                "absence of contradiction does not license extension",
            ]),
        )

    return ExtensionAssessment(
        licensed=True, examined=True,
        provenance=ExtensionProvenance.VERIFIED_EXTENSION,
        positive_hits=tuple(matched),
        category="positive_continuity",
        reasons=tuple(["positive continuity: {}".format(", ".join(matched))]),
    )
