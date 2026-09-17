"""Stage A: deterministic detection of semantically material evidence change.

This is the cheap local half of the two-stage classification strategy. It
never calls a model. Its only job is to answer one question:

    Does the new evidence justify reconsidering the current semantic verdict?

Answering "no" is the common case and costs nothing. Answering "yes" is what
buys a Gemini call, so the signals are explicit, named, individually testable,
and each carries the observation that produced it. "Why did Noema call the
model here?" and "why did it not?" are both answerable from the returned
assessment alone, without reading this file.

Signals are *evidence*, not verdicts. A single application change is a signal;
it is not by itself a reason to destroy an episode or overturn a verdict. The
detector reports what changed and how strongly; the revision policy decides.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .evidence_window import EvidenceWindow, observation_digest


# --- signal names -------------------------------------------------------
# Each is a distinct, observable reason to reconsider. They are deliberately
# separate so telemetry can report *which* one fired.

SIGNAL_APPLICATION_TRANSITION = "application_transition"
SIGNAL_DOMAIN_TRANSITION = "domain_transition"
SIGNAL_SEMANTIC_NOVELTY = "semantic_novelty"
SIGNAL_EVIDENCE_COMPOSITION_SHIFT = "evidence_composition_shift"
SIGNAL_ACTIVITY_ENTROPY_CHANGE = "activity_entropy_change"
SIGNAL_NEW_ACTIVE_DURATION = "new_active_duration"
SIGNAL_LOW_CONFIDENCE_VERDICT = "low_confidence_verdict"
SIGNAL_LOW_EVIDENCE_QUALITY = "low_evidence_quality"
SIGNAL_VERDICT_CONTRADICTION = "verdict_contradiction"
SIGNAL_PROLONGED_OUTSIDE_CLUSTER = "prolonged_outside_cluster"
SIGNAL_NO_PRIOR_VERDICT = "no_prior_verdict"
SIGNAL_EVIDENCE_UNCOVERED = "evidence_uncovered"

#: Signals that on their own justify reconsideration. Everything else needs
#: corroboration: one weak hint is never semantic proof (section 8).
_SUFFICIENT_ALONE = frozenset({
    SIGNAL_NO_PRIOR_VERDICT,
    SIGNAL_VERDICT_CONTRADICTION,
    SIGNAL_PROLONGED_OUTSIDE_CLUSTER,
})

#: Corroborating signal *families* needed when no decisive signal fired.
#: Counting families rather than signals stops one change from corroborating
#: itself: a new application necessarily shifts evidence composition and
#: attention spread too, and those three observations are one event, not
#: three independent reasons.
CORROBORATION_REQUIRED = 2

#: Which kind of change each signal observes.
SIGNAL_FAMILIES = {
    SIGNAL_APPLICATION_TRANSITION: "surface",
    SIGNAL_DOMAIN_TRANSITION: "surface",
    SIGNAL_SEMANTIC_NOVELTY: "topical",
    SIGNAL_EVIDENCE_COMPOSITION_SHIFT: "structural",
    SIGNAL_ACTIVITY_ENTROPY_CHANGE: "structural",
    SIGNAL_LOW_CONFIDENCE_VERDICT: "verdict_strength",
    SIGNAL_LOW_EVIDENCE_QUALITY: "verdict_strength",
}

# --- thresholds ---------------------------------------------------------
# Named, documented, and individually testable. None is a bare magic number
# at a call site.

#: New active seconds below which growth is ordinary heartbeat drift rather
#: than new evidence worth a model call.
MIN_NEW_ACTIVE_SECONDS = 90.0

#: Share of current observations that must be new before evidence
#: composition counts as shifted.
COMPOSITION_SHIFT_FRACTION = 0.34

#: Share of the prior verdict's topical anchors that must have *vanished*
#: from the new evidence before the topic counts as genuinely changed.
#:
#: This is deliberately asymmetric. Symmetric set distance punishes
#: vocabulary growth ("Noema" -> "Noema pull request review") exactly like
#: vocabulary replacement ("Noema" -> "Netflix Friends"), which would make
#: every deepening of the same task look like a new one. What actually
#: distinguishes them is whether the prior anchors survived.
SEMANTIC_NOVELTY_ANCHOR_LOSS = 0.80

#: Minimum number of distinct prior anchors required before their absence
#: means anything. One token disappearing is noise, not a topic change.
MIN_ANCHORS_FOR_NOVELTY = 2

#: Change in normalized application entropy that counts as a change in how
#: scattered the activity is.
ENTROPY_DELTA = 0.28

#: A verdict at or below this confidence is weak enough that new evidence
#: should be allowed to revisit it cheaply.
LOW_CONFIDENCE_CEILING = 0.55

#: Evidence-quality grades that make a verdict cheap to revisit.
_WEAK_EVIDENCE = frozenset({"weak", "absent", "low"})

#: Continuous seconds of activity outside the verdict's semantic cluster
#: that, alone, justify reconsideration.
PROLONGED_OUTSIDE_CLUSTER_SECONDS = 240.0

#: Words that carry no topical meaning and must not create false novelty.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "new", "tab", "page", "home", "com", "www", "http",
    "https", "org", "net", "io", "html", "index", "untitled", "google",
    "search", "mozilla", "firefox", "chrome", "edge", "brave", "safari",
})


def _tokens(*values: Any) -> Set[str]:
    """Topical tokens from arbitrary text, stopwords and noise removed."""

    found: Set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        for token in re.findall(r"[a-z0-9]{3,}", value.casefold()):
            if token not in _STOPWORDS and not token.isdigit():
                found.add(token)
    return found


def _observation_tokens(observations: Iterable[Mapping[str, Any]]) -> Set[str]:
    collected: Set[str] = set()
    for item in observations or ():
        if not isinstance(item, Mapping):
            continue
        collected |= _tokens(
            item.get("title"), item.get("window_title"),
            item.get("domain"), item.get("url"),
        )
    return collected


def _applications(observations: Iterable[Mapping[str, Any]]) -> List[str]:
    names = []
    for item in observations or ():
        if not isinstance(item, Mapping):
            continue
        value = item.get("application") or item.get("app")
        if isinstance(value, str) and value.strip():
            names.append(value.strip().casefold())
    return names


def _domains(observations: Iterable[Mapping[str, Any]]) -> List[str]:
    names = []
    for item in observations or ():
        if not isinstance(item, Mapping):
            continue
        value = item.get("domain")
        if isinstance(value, str) and value.strip():
            names.append(value.strip().casefold().lstrip("www."))
    return names


def _duration(item: Mapping[str, Any]) -> float:
    try:
        return max(0.0, float(item.get("duration_seconds") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _normalized_entropy(values: Sequence[str]) -> float:
    """Shannon entropy of a label distribution, normalized to [0, 1].

    0.0 means one label dominates completely; 1.0 means attention is spread
    evenly. Used to detect "the user went from one app to scattered across
    six", which is a change in *how* they are working, not just in what.
    """

    if not values:
        return 0.0
    counts: Dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    total = float(len(values))
    if len(counts) <= 1:
        return 0.0
    entropy = -sum((c / total) * math.log(c / total, 2) for c in counts.values())
    return min(1.0, entropy / math.log(len(counts), 2)) if len(counts) > 1 else 0.0


def _anchor_loss(prior: Set[str], current: Set[str]) -> float:
    """Fraction of the prior topical anchors absent from the new evidence.

    1.0 means the earlier subject matter left no trace in what followed;
    0.0 means every anchor is still present. Growth of vocabulary does not
    raise this value, which is the point: continuing the same task adds
    words without erasing the ones that identified it.
    """

    if not prior:
        return 0.0
    surviving = len(prior & current)
    return 1.0 - (surviving / len(prior))


@dataclass(frozen=True)
class ChangeSignal:
    """One named, observed reason to reconsider (or not)."""

    name: str
    fired: bool
    detail: str
    magnitude: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "fired": self.fired,
            "detail": self.detail,
            "magnitude": round(float(self.magnitude), 4),
        }


@dataclass(frozen=True)
class ChangeAssessment:
    """Stage A's answer, with the full reasoning attached.

    ``should_reconsider`` is the decision. ``signals`` is every signal that
    was evaluated, fired or not, so the negative case is as explainable as
    the positive one.
    """

    should_reconsider: bool
    reason: str
    signals: Tuple[ChangeSignal, ...] = ()
    new_active_seconds: float = 0.0
    uncovered_seconds: float = 0.0
    new_observation_count: int = 0
    #: True only when the full signal battery ran over the new evidence and
    #: found it consistent. False when the detector declined to look at all
    #: (below the new-evidence floor). "I checked and it is consistent" and
    #: "there was too little to bother checking" are different conclusions,
    #: and only the first one justifies letting a verdict cover that
    #: evidence without a fresh model call.
    examined: bool = False

    @property
    def fired(self) -> Tuple[str, ...]:
        return tuple(signal.name for signal in self.signals if signal.fired)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "should_reconsider": self.should_reconsider,
            "reason": self.reason,
            "fired_signals": list(self.fired),
            "signals": [signal.to_dict() for signal in self.signals],
            "new_active_seconds": round(self.new_active_seconds, 3),
            "uncovered_seconds": round(self.uncovered_seconds, 3),
            "new_observation_count": self.new_observation_count,
            "examined": self.examined,
        }


class EvidenceChangeDetector:
    """Decide locally whether new evidence warrants a model call.

    Pure and side-effect free: observations in, assessment out. It reads only
    what was measured and never asks a model anything.
    """

    def __init__(
        self,
        min_new_active_seconds: float = MIN_NEW_ACTIVE_SECONDS,
        composition_shift_fraction: float = COMPOSITION_SHIFT_FRACTION,
        semantic_novelty_anchor_loss: float = SEMANTIC_NOVELTY_ANCHOR_LOSS,
        entropy_delta: float = ENTROPY_DELTA,
        low_confidence_ceiling: float = LOW_CONFIDENCE_CEILING,
        prolonged_outside_cluster_seconds: float = PROLONGED_OUTSIDE_CLUSTER_SECONDS,
    ):
        self.min_new_active_seconds = float(min_new_active_seconds)
        self.composition_shift_fraction = float(composition_shift_fraction)
        self.semantic_novelty_anchor_loss = float(semantic_novelty_anchor_loss)
        self.entropy_delta = float(entropy_delta)
        self.low_confidence_ceiling = float(low_confidence_ceiling)
        self.prolonged_outside_cluster_seconds = float(prolonged_outside_cluster_seconds)

    def assess(
        self,
        prior_window: Optional[EvidenceWindow],
        prior_verdict: Optional[Any],
        current_observations: Sequence[Mapping[str, Any]],
        current_start: Any = None,
        current_end: Any = None,
        current_active_seconds: Optional[float] = None,
    ) -> ChangeAssessment:
        """Compare a stored verdict's evidence against the current evidence."""

        observations = [item for item in (current_observations or ()) if isinstance(item, Mapping)]

        # No prior verdict: there is nothing to retain, so this is not a
        # "change" at all — it is a first classification.
        if prior_verdict is None or prior_window is None:
            signal = ChangeSignal(
                SIGNAL_NO_PRIOR_VERDICT, True,
                "no prior verdict with an evidence window exists", 1.0)
            return ChangeAssessment(
                should_reconsider=True,
                reason="first classification: no prior verdict to retain",
                signals=(signal,),
                new_observation_count=len(observations),
            )

        # Partition the current evidence into what the prior verdict saw and
        # what appeared after it. Time is the partition key: an observation
        # ending after the window closed is evidence the verdict never had.
        covered: List[Mapping[str, Any]] = []
        uncovered: List[Mapping[str, Any]] = []
        for item in observations:
            stamp = item.get("end") or item.get("timestamp") or item.get("start")
            if stamp is None:
                covered.append(item)
                continue
            try:
                covered.append(item) if prior_window.covers(stamp, tolerance_seconds=1.0) else uncovered.append(item)
            except (TypeError, ValueError):
                covered.append(item)

        new_active = sum(_duration(item) for item in uncovered)
        signals: List[ChangeSignal] = []

        # --- 1. did meaningful new time even happen? --------------------
        enough_new_time = new_active >= self.min_new_active_seconds
        signals.append(ChangeSignal(
            SIGNAL_NEW_ACTIVE_DURATION, enough_new_time,
            "{:.0f}s of new active evidence (threshold {:.0f}s)".format(
                new_active, self.min_new_active_seconds),
            new_active))

        uncovered_seconds = new_active
        if not uncovered and not enough_new_time:
            # Nothing new at all. Cheapest possible answer.
            signals.append(ChangeSignal(
                SIGNAL_EVIDENCE_UNCOVERED, False,
                "all current evidence lies inside the verdict's window", 0.0))
            return ChangeAssessment(
                should_reconsider=False,
                reason="no evidence outside the current verdict's window",
                signals=tuple(signals),
                new_active_seconds=new_active,
                uncovered_seconds=uncovered_seconds,
                new_observation_count=0,
            )

        signals.append(ChangeSignal(
            SIGNAL_EVIDENCE_UNCOVERED, bool(uncovered),
            "{} observation(s) outside the verdict's window".format(len(uncovered)),
            float(len(uncovered))))

        # --- 2. application / domain transitions ------------------------
        old_apps = set(_applications(covered))
        new_apps = set(_applications(uncovered))
        introduced_apps = new_apps - old_apps
        signals.append(ChangeSignal(
            SIGNAL_APPLICATION_TRANSITION, bool(introduced_apps),
            "new application(s): {}".format(", ".join(sorted(introduced_apps)) or "none"),
            float(len(introduced_apps))))

        old_domains = set(_domains(covered))
        new_domains = set(_domains(uncovered))
        introduced_domains = new_domains - old_domains
        signals.append(ChangeSignal(
            SIGNAL_DOMAIN_TRANSITION, bool(introduced_domains),
            "new domain(s): {}".format(", ".join(sorted(introduced_domains)) or "none"),
            float(len(introduced_domains))))

        # --- 3. semantic novelty ----------------------------------------
        old_tokens = _observation_tokens(covered)
        new_tokens = _observation_tokens(uncovered)
        anchors = old_tokens | _tokens(
            getattr(prior_verdict, "topic", None),
            getattr(prior_verdict, "project", None),
        )
        loss = _anchor_loss(anchors, new_tokens)
        novel = (
            bool(new_tokens)
            and len(anchors) >= MIN_ANCHORS_FOR_NOVELTY
            and loss >= self.semantic_novelty_anchor_loss
        )
        if len(anchors) < MIN_ANCHORS_FOR_NOVELTY:
            novelty_detail = "too few prior anchors ({}) to judge topical change".format(len(anchors))
        else:
            novelty_detail = "{:.0%} of prior topical anchors absent from new evidence (threshold {:.0%})".format(
                loss, self.semantic_novelty_anchor_loss)
        signals.append(ChangeSignal(SIGNAL_SEMANTIC_NOVELTY, novel, novelty_detail, loss))

        # --- 4. evidence composition ------------------------------------
        total = len(covered) + len(uncovered)
        share = (len(uncovered) / total) if total else 0.0
        shifted = share >= self.composition_shift_fraction
        signals.append(ChangeSignal(
            SIGNAL_EVIDENCE_COMPOSITION_SHIFT, shifted,
            "{:.0%} of evidence is new (threshold {:.0%})".format(
                share, self.composition_shift_fraction),
            share))

        # --- 5. activity entropy ----------------------------------------
        old_entropy = _normalized_entropy(_applications(covered))
        combined_entropy = _normalized_entropy(_applications(observations))
        entropy_change = abs(combined_entropy - old_entropy)
        entropy_moved = entropy_change >= self.entropy_delta
        signals.append(ChangeSignal(
            SIGNAL_ACTIVITY_ENTROPY_CHANGE, entropy_moved,
            "attention spread moved {:.2f} (threshold {:.2f})".format(
                entropy_change, self.entropy_delta),
            entropy_change))

        # --- 6. how confident was the verdict we would be retaining? ----
        try:
            confidence = float(getattr(prior_verdict, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        weak_verdict = confidence <= self.low_confidence_ceiling
        signals.append(ChangeSignal(
            SIGNAL_LOW_CONFIDENCE_VERDICT, weak_verdict,
            "prior confidence {:.2f} (ceiling {:.2f})".format(
                confidence, self.low_confidence_ceiling),
            confidence))

        quality = str(getattr(prior_verdict, "evidence_quality", "") or "").strip().casefold()
        weak_quality = quality in _WEAK_EVIDENCE
        signals.append(ChangeSignal(
            SIGNAL_LOW_EVIDENCE_QUALITY, weak_quality,
            "prior evidence quality {!r}".format(quality or "unknown"),
            1.0 if weak_quality else 0.0))

        # --- 7. does the new evidence contradict the verdict? -----------
        contradiction, contradiction_detail = self._contradiction(prior_verdict, uncovered)
        signals.append(ChangeSignal(
            SIGNAL_VERDICT_CONTRADICTION, contradiction, contradiction_detail,
            1.0 if contradiction else 0.0))

        # --- 8. prolonged activity outside the verdict's cluster --------
        cluster_tokens = old_tokens | _tokens(
            getattr(prior_verdict, "topic", None),
            getattr(prior_verdict, "project", None),
            getattr(prior_verdict, "activity", None),
        )
        outside_seconds = 0.0
        for item in uncovered:
            item_tokens = _tokens(
                item.get("title"), item.get("window_title"),
                item.get("domain"), item.get("url"))
            if item_tokens and not (item_tokens & cluster_tokens):
                outside_seconds += _duration(item)
        prolonged = outside_seconds >= self.prolonged_outside_cluster_seconds
        signals.append(ChangeSignal(
            SIGNAL_PROLONGED_OUTSIDE_CLUSTER, prolonged,
            "{:.0f}s outside the verdict's semantic cluster (threshold {:.0f}s)".format(
                outside_seconds, self.prolonged_outside_cluster_seconds),
            outside_seconds))

        return self._decide(signals, new_active, uncovered_seconds, len(uncovered),
                            enough_new_time)

    @staticmethod
    def _contradiction(prior_verdict: Any, uncovered: Sequence[Mapping[str, Any]]) -> Tuple[bool, str]:
        """Does the new evidence positively contradict the stored verdict?

        Contradiction requires *positive* evidence of a different kind of
        activity, never the mere absence of supporting evidence. A verdict of
        PRODUCTIVE is not contradicted by silence; it is contradicted by
        sustained, specific evidence of something else.
        """

        category = str(getattr(prior_verdict, "category", "") or "").strip().casefold()
        if category not in {"productive", "distractive"}:
            return False, "prior verdict is not a strong claim; nothing to contradict"

        opposite_tokens = _ENTERTAINMENT_TOKENS if category == "productive" else _WORK_TOKENS
        seconds = 0.0
        matched: Set[str] = set()
        for item in uncovered:
            text_tokens = _tokens(
                item.get("title"), item.get("window_title"),
                item.get("domain"), item.get("url"),
                item.get("application"), item.get("app"))
            hit = text_tokens & opposite_tokens
            if hit:
                seconds += _duration(item)
                matched |= hit
        # A single brief glance is not a contradiction; sustained opposite
        # evidence is. The threshold is the same "prolonged" bar used above.
        if seconds >= PROLONGED_OUTSIDE_CLUSTER_SECONDS and matched:
            return True, "{:.0f}s of evidence contradicting {!r}: {}".format(
                seconds, category, ", ".join(sorted(matched)[:5]))
        return False, "no sustained evidence contradicting {!r}".format(category or "unknown")

    @staticmethod
    def _decide(
        signals: Sequence[ChangeSignal],
        new_active: float,
        uncovered_seconds: float,
        new_count: int,
        enough_new_time: bool,
    ) -> ChangeAssessment:
        fired = {signal.name for signal in signals if signal.fired}
        sufficient = fired & _SUFFICIENT_ALONE
        if sufficient:
            return ChangeAssessment(
                should_reconsider=True,
                reason="decisive signal: {}".format(", ".join(sorted(sufficient))),
                signals=tuple(signals),
                new_active_seconds=new_active,
                uncovered_seconds=uncovered_seconds,
                new_observation_count=new_count,
            )

        # Everything else needs both enough new time AND corroboration. One
        # application switch on 20 seconds of evidence is not a reason to
        # spend a model call.
        corroborating = fired - _SUFFICIENT_ALONE - {SIGNAL_EVIDENCE_UNCOVERED,
                                                     SIGNAL_NEW_ACTIVE_DURATION}
        families = {SIGNAL_FAMILIES.get(name, name) for name in corroborating}
        if enough_new_time and len(families) >= CORROBORATION_REQUIRED:
            return ChangeAssessment(
                should_reconsider=True,
                reason="{:.0f}s of new evidence corroborated across {} change families ({})".format(
                    new_active, len(families), ", ".join(sorted(families))),
                signals=tuple(signals),
                new_active_seconds=new_active,
                uncovered_seconds=uncovered_seconds,
                new_observation_count=new_count,
            )

        if not enough_new_time:
            # Declined to look. The new evidence was never assessed for
            # consistency, so nothing here licenses covering it.
            reason = "only {:.0f}s of new evidence; below the {:.0f}s reconsideration floor".format(
                new_active, MIN_NEW_ACTIVE_SECONDS)
            examined = False
        else:
            # Looked at every signal and found continuity.
            reason = "new evidence is consistent with the current verdict ({} change famil(y/ies) observed, {} required)".format(
                len(families), CORROBORATION_REQUIRED)
            examined = True
        return ChangeAssessment(
            should_reconsider=False,
            reason=reason,
            signals=tuple(signals),
            new_active_seconds=new_active,
            uncovered_seconds=uncovered_seconds,
            new_observation_count=new_count,
            examined=examined,
        )


#: Token vocabularies used only for *contradiction* detection — that is, for
#: deciding whether to spend a model call, never for deciding a verdict. A
#: match here buys reconsideration; the model still decides what the evidence
#: means. This is deliberately not an application allowlist: it is topical
#: vocabulary observed in titles, domains and URLs.
_ENTERTAINMENT_TOKENS = frozenset({
    "netflix", "hulu", "disney", "primevideo", "hotstar", "cinepolis",
    "instagram", "tiktok", "reddit", "twitch", "9gag", "memes", "meme",
    "trailer", "episode", "season", "movie", "movies", "series", "anime",
    "gameplay", "playthrough", "speedrun", "vlog", "funny", "prank",
    "highlights", "shopping", "cart", "checkout", "amazon", "flipkart",
    "myntra", "ebay", "deals", "sale",
})

_WORK_TOKENS = frozenset({
    "pull", "request", "commit", "repository", "repo", "github", "gitlab",
    "jira", "stacktrace", "traceback", "compile", "build", "deploy",
    "documentation", "docs", "api", "reference", "tutorial", "lecture",
    "paper", "arxiv", "stackoverflow", "debug", "refactor", "migration",
    "schema", "query", "terminal", "console", "pytest", "npm",
})
