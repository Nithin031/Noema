"""Deterministic local distraction candidate detector + hysteresis.

Two pieces, deliberately split:

- :class:`DistractionCandidateDetector` is stateless: a
  :class:`BehaviorWindow` in, a bounded ``0.0–1.0`` explainable score out.
  No single threshold decides alone; weak signals combine into strong
  ones through configured weights.
- :class:`DetectionTracker` is the stateful hysteresis machine
  (``NORMAL → CANDIDATE → CONFIRMED → INTERVENTION_COOLDOWN → RECOVERING
  → NORMAL``) so a score oscillating around one threshold cannot
  repeatedly invoke the fast model.

Both are pure/deterministic: identical inputs always give identical
outputs, which is what makes the mandatory boundary tests possible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .features import BehaviorWindow

DETECTOR_VERSION = "1"

_DEFAULT_WEIGHTS = {
    "sustained_run": 0.25,
    "distraction_minutes": 0.18,
    "distraction_ratio": 0.14,
    "distraction_entries": 0.06,
    "short_bursts": 0.05,
    "distraction_streak": 0.04,
    "repeated_context": 0.05,
    "context_switching": 0.03,
    "productive_to_distraction": 0.03,
    "time_since_productive": 0.08,
    "goal_misalignment": 0.02,
    "current_session": 0.07,
}


def _clamp01(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


@dataclass(frozen=True)
class DetectorConfig:
    """Detector knobs. All changes are code-free config."""

    weights: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    enter_threshold: float = 0.70
    exit_threshold: float = 0.50
    min_active_seconds: float = 120.0
    cooldown_seconds: float = 1800.0
    recovery_seconds: float = 600.0

    def __post_init__(self) -> None:
        weights = dict(self.weights)
        missing = set(_DEFAULT_WEIGHTS) - set(weights)
        if missing:
            raise ValueError("detector weights missing signals: {}".format(sorted(missing)))
        total = sum(float(weights[key]) for key in _DEFAULT_WEIGHTS)
        if abs(total - 1.0) > 0.001:
            raise ValueError("detector weights must sum to 1.0, got {}".format(total))
        if not 0.0 < float(self.exit_threshold) < float(self.enter_threshold) < 1.0:
            raise ValueError("need 0 < exit < enter < 1")
        if float(self.min_active_seconds) < 0:
            raise ValueError("min_active_seconds cannot be negative")
        if float(self.cooldown_seconds) < 0 or float(self.recovery_seconds) < 0:
            raise ValueError("cooldown/recovery seconds cannot be negative")


@dataclass(frozen=True)
class CandidateDecision:
    """One scoring verdict for one window."""

    score: float
    is_candidate: bool
    signals: Dict[str, float]
    reasons: List[str]
    window_minutes: int
    evaluated_at: str
    detector_version: str = DETECTOR_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "is_candidate": self.is_candidate,
            "signals": {key: round(value, 3) for key, value in self.signals.items()},
            "reasons": list(self.reasons),
            "window_minutes": self.window_minutes,
            "evaluated_at": self.evaluated_at,
            "detector_version": self.detector_version,
        }


class DistractionCandidateDetector:
    """Score rolling windows without any model call."""

    def __init__(self, config: Optional[DetectorConfig] = None):
        self.config = config or DetectorConfig()

    @staticmethod
    def _signals(window: BehaviorWindow) -> Dict[str, float]:
        distraction_minutes = window.distractive_seconds / 60.0
        active_minutes = window.active_seconds / 60.0
        unclassified_ratio = (
            window.unclassified_seconds / window.active_seconds
            if window.active_seconds > 0
            else 0.0
        )
        repeated_max = max(window.repeated_distraction_contexts.values(), default=0)
        since_minutes = (
            window.time_since_last_productive_session / 60.0
            if window.time_since_last_productive_session is not None
            else None
        )
        return {
            # One sustained 15-minute run saturates: a long unbroken drift
            # is a pattern by itself, even as a single session.
            "sustained_run": _clamp01(
                (window.longest_distraction_run_seconds / 60.0) / 15.0
            ),
            # 20 distraction-minutes in the hour saturates.
            "distraction_minutes": _clamp01(distraction_minutes / 20.0),
            # Rises from 10% to 50% distraction share.
            "distraction_ratio": _clamp01((window.distraction_ratio - 0.10) / 0.40),
            # A single entry is not a pattern; five is.
            "distraction_entries": _clamp01((window.distraction_entries - 1) / 4.0),
            "short_bursts": _clamp01(window.short_distraction_bursts / 3.0),
            "distraction_streak": _clamp01((window.longest_distraction_streak - 1) / 3.0),
            "repeated_context": _clamp01((repeated_max - 1) / 2.0),
            "context_switching": _clamp01(window.context_switch_count / 10.0),
            "productive_to_distraction": _clamp01(
                window.productive_to_distraction_switches / 3.0
            ),
            "time_since_productive": _clamp01(
                since_minutes / 30.0 if since_minutes is not None
                else active_minutes / 20.0
            ),
            "goal_misalignment": _clamp01(
                1.0 - window.goal_alignment if window.goal_alignment is not None else 0.0
            ),
            "current_session": _clamp01(
                window.current_session_confidence
                if window.current_session_category == "distractive"
                else 0.0
            ),
            "_unclassified_ratio": _clamp01(unclassified_ratio),
        }

    def score(self, window: BehaviorWindow) -> CandidateDecision:
        """Score one window. Never raises on odd input; degrades to 0."""
        evaluated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        signals = self._signals(window)
        unclassified_ratio = signals.pop("_unclassified_ratio")
        if window.active_seconds < float(self.config.min_active_seconds):
            return CandidateDecision(
                score=0.0,
                is_candidate=False,
                signals=signals,
                reasons=["window has too little active time to judge"],
                window_minutes=window.window_minutes,
                evaluated_at=evaluated_at,
            )
        if window.current_session_category == "afk":
            return CandidateDecision(
                score=0.0,
                is_candidate=False,
                signals=signals,
                reasons=["user is AFK; presence vetoes candidacy"],
                window_minutes=window.window_minutes,
                evaluated_at=evaluated_at,
            )
        weighted = sum(
            float(self.config.weights[name]) * value for name, value in signals.items()
        )
        # Heavy unclassification tempers the score: verdicts we do not have
        # must not manufacture urgency. At most halves the score.
        score = _clamp01(weighted * (1.0 - 0.5 * unclassified_ratio))
        ranked = sorted(signals.items(), key=lambda item: item[1], reverse=True)
        reasons = [
            "{}={:.2f}".format(name, value) for name, value in ranked[:3] if value > 0
        ] or ["no distraction signals"]
        return CandidateDecision(
            score=score,
            is_candidate=score >= float(self.config.enter_threshold),
            signals=signals,
            reasons=reasons,
            window_minutes=window.window_minutes,
            evaluated_at=evaluated_at,
        )


class DetectionState(str, Enum):
    NORMAL = "NORMAL"
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    INTERVENTION_COOLDOWN = "INTERVENTION_COOLDOWN"
    RECOVERING = "RECOVERING"


class DetectionTracker:
    """Hysteresis machine around the detector score.

    Enter at ``enter_threshold``, exit at the strictly lower
    ``exit_threshold``. External events drive confirmation
    (``notify_confirmed`` after fast-model verification) and
    ``notify_intervention`` after an action is taken. The tracker itself is
    deterministic given the same (score, now, event) sequence and can be
    serialized for restart safety.
    """

    def __init__(self, config: Optional[DetectorConfig] = None,
                 state: str = DetectionState.NORMAL.value,
                 state_since: Optional[str] = None,
                 last_score: float = 0.0):
        self.config = config or DetectorConfig()
        self.state = str(state)
        self.state_since = state_since
        self.last_score = float(last_score)

    def _stamp(self, now: datetime) -> str:
        return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _enter(self, state: str, score: float, now: datetime) -> str:
        if state != self.state:
            self.state = state
            self.state_since = self._stamp(now)
        self.last_score = float(score)
        return self.state

    def update(self, score: float, now: Optional[datetime] = None) -> str:
        """Feed a fresh score; returns the (possibly new) state."""
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        score = _clamp01(score)
        enter = float(self.config.enter_threshold)
        exit_at = float(self.config.exit_threshold)
        if self.state == DetectionState.NORMAL.value:
            if score >= enter:
                return self._enter(DetectionState.CANDIDATE.value, score, moment)
        elif self.state == DetectionState.CANDIDATE.value:
            if score <= exit_at:
                return self._enter(DetectionState.NORMAL.value, score, moment)
        elif self.state == DetectionState.CONFIRMED.value:
            if score <= exit_at:
                return self._enter(DetectionState.RECOVERING.value, score, moment)
        elif self.state == DetectionState.INTERVENTION_COOLDOWN.value:
            cooling = True
            if self.state_since:
                try:
                    since = datetime.fromisoformat(self.state_since.replace("Z", "+00:00"))
                    cooling = (moment - since).total_seconds() < float(self.config.cooldown_seconds)
                except ValueError:
                    cooling = True
            if not cooling:
                if score >= enter:
                    return self._enter(DetectionState.CANDIDATE.value, score, moment)
                return self._enter(DetectionState.RECOVERING.value, score, moment)
        elif self.state == DetectionState.RECOVERING.value:
            if score >= enter:
                return self._enter(DetectionState.CANDIDATE.value, score, moment)
            recovering = True
            if self.state_since:
                try:
                    since = datetime.fromisoformat(self.state_since.replace("Z", "+00:00"))
                    recovering = (moment - since).total_seconds() < float(self.config.recovery_seconds)
                except ValueError:
                    recovering = True
            if not recovering:
                return self._enter(DetectionState.NORMAL.value, score, moment)
        else:  # Unknown persisted state: fail safe to NORMAL.
            return self._enter(DetectionState.NORMAL.value, score, moment)
        self.last_score = float(score)
        return self.state

    def notify_confirmed(self, score: float, now: Optional[datetime] = None) -> str:
        """Fast model verified the candidate: CANDIDATE → CONFIRMED."""
        moment = now or datetime.now(timezone.utc)
        if self.state == DetectionState.CANDIDATE.value:
            return self._enter(DetectionState.CONFIRMED.value, score, moment)
        self.last_score = float(score)
        return self.state

    def notify_intervention(self, now: Optional[datetime] = None) -> str:
        """An intervention action was taken: → INTERVENTION_COOLDOWN."""
        moment = now or datetime.now(timezone.utc)
        return self._enter(DetectionState.INTERVENTION_COOLDOWN.value, self.last_score, moment)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "state_since": self.state_since,
            "last_score": round(self.last_score, 3),
            "enter_threshold": float(self.config.enter_threshold),
            "exit_threshold": float(self.config.exit_threshold),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any],
                  config: Optional[DetectorConfig] = None) -> "DetectionTracker":
        return cls(
            config=config,
            state=str(payload.get("state", DetectionState.NORMAL.value)),
            state_since=payload.get("state_since"),
            last_score=float(payload.get("last_score", 0.0) or 0.0),
        )
