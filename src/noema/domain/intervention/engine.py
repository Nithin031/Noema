"""Policy-gated interventions driven by behavior observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from noema.domain.activity import coerce_timestamp
from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.application.classification import Classification
from noema.domain.intent import Intent
from noema.domain.session_context import browser_target, session_label
from noema.domain.sessions import ActivitySession


class InterventionMode(str, Enum):
    HOLDOUT = "HOLDOUT"
    NOTIFICATION = "NOTIFICATION"
    MEME = "MEME"


class InterventionStatus(str, Enum):
    PLANNED = "PLANNED"
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class InterventionActionPayload:
    """Explicit, transport-safe action contract for browser/desktop clients."""

    intervention_id: str
    type: str
    severity: int = 1
    target: Mapping[str, Any] = field(default_factory=dict)
    message: Mapping[str, Any] = field(default_factory=dict)
    meme: Optional[Mapping[str, Any]] = None
    svg: Optional[str] = None
    actions: Tuple[str, ...] = ()
    expires_in: int = 5000

    def __post_init__(self) -> None:
        object.__setattr__(self, "intervention_id", str(self.intervention_id))
        object.__setattr__(self, "type", str(self.type or "NONE").upper())
        object.__setattr__(self, "severity", min(5, max(1, int(self.severity))))
        object.__setattr__(self, "target", dict(self.target or {}))
        object.__setattr__(self, "message", dict(self.message or {}))
        object.__setattr__(self, "meme", dict(self.meme) if self.meme else None)
        object.__setattr__(self, "svg", str(self.svg) if self.svg else None)
        object.__setattr__(
            self,
            "actions",
            tuple(
                dict.fromkeys(
                    str(action).strip().upper()
                    for action in (self.actions or ())
                    if str(action).strip()
                )
            ),
        )
        object.__setattr__(self, "expires_in", max(0, int(self.expires_in)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intervention_id": self.intervention_id,
            "type": self.type,
            "severity": self.severity,
            "target": dict(self.target),
            "message": dict(self.message),
            "meme": dict(self.meme) if self.meme else None,
            "svg": self.svg,
            "actions": list(self.actions),
            "expires_in": self.expires_in,
        }


@dataclass(frozen=True)
class InterventionPolicy:
    enabled: bool = True
    mode: InterventionMode = InterventionMode.NOTIFICATION
    cooldown_seconds: float = 900.0
    require_actionable: bool = True
    holdout_seconds: float = 60.0
    dry_run: bool = True
    # Per-mode cooldowns bound repeats of the SAME mode. MEME is
    # deliberately slower than NOTIFICATION: identical memes in a row
    # ("meme meme meme") are noise, not help.
    mode_cooldowns: Mapping[str, float] = field(default_factory=lambda: {
        "MEME": 3600.0,
        "NOTIFICATION": 900.0,
        "HOLDOUT": 300.0,
    })
    # After this many consecutive NOT_RECOVERED outcomes for the current
    # mode (within ineffective_lookback_seconds), MEME de-escalates to
    # NOTIFICATION and other modes back off entirely instead of escalating
    # forever.
    max_ineffective_streak: int = 3
    ineffective_lookback_seconds: float = 86400.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", InterventionMode(self.mode))
        if self.cooldown_seconds < 0 or self.holdout_seconds < 0:
            raise ValueError("intervention durations cannot be negative")
        if int(self.max_ineffective_streak) < 1:
            raise ValueError("max_ineffective_streak must be at least 1")
        if float(self.ineffective_lookback_seconds) < 0:
            raise ValueError("ineffective_lookback_seconds cannot be negative")
        normalized = {
            str(key).upper(): float(value)
            for key, value in dict(self.mode_cooldowns or {}).items()
        }
        if any(value < 0 for value in normalized.values()):
            raise ValueError("mode cooldowns cannot be negative")
        object.__setattr__(self, "mode_cooldowns", normalized)


@dataclass(frozen=True)
class Intervention:
    session_id: str
    mode: Optional[InterventionMode]
    status: InterventionStatus
    reason: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    executed_at: Optional[datetime] = None
    intervention_id: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", str(self.session_id))
        if self.mode is not None:
            object.__setattr__(self, "mode", InterventionMode(self.mode))
        object.__setattr__(self, "status", InterventionStatus(self.status))
        object.__setattr__(self, "payload", dict(self.payload or {}))
        object.__setattr__(self, "created_at", coerce_timestamp(self.created_at))
        if self.executed_at is not None:
            object.__setattr__(self, "executed_at", coerce_timestamp(self.executed_at))

    @property
    def id(self) -> str:
        if self.intervention_id:
            return self.intervention_id
        identity = "{}|{}|{}".format(self.session_id, self.mode or "SKIP", self.created_at.isoformat())
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @property
    def action(self) -> InterventionActionPayload:
        """Return the stable action contract derived from the stored payload."""

        payload = dict(self.payload)
        message = payload.get("message", {})
        if isinstance(message, str):
            message = {"title": "Noema", "body": message}
        meme = payload.get("meme")
        svg = payload.get("svg")
        if meme and not svg:
            # Use the existing bounded renderer; model output never becomes
            # executable markup in the browser.
            from noema.domain.meme import MemePayload, MemeRenderer

            meme_payload = MemePayload(
                session_id=self.id,
                template=meme.get("template", "drake"),
                severity=meme.get("severity", payload.get("severity", 2)),
                top=meme.get("top", "Your active goal"),
                bottom=meme.get("bottom", "A distraction"),
            )
            svg = MemeRenderer().render_svg(meme_payload)
        return InterventionActionPayload(
            intervention_id=self.id,
            type=self.mode.value if self.mode else "NONE",
            severity=payload.get("severity", 1),
            target=payload.get("target", {}),
            message=message,
            meme=meme,
            svg=svg,
            actions=tuple(payload.get("actions", ())),
            expires_in=payload.get("expires_in", 5000),
        )

    def with_target(self, target: Mapping[str, Any]) -> "Intervention":
        """Return a copy with missing target fields filled from live browser state."""

        payload = dict(self.payload)
        merged = dict(payload.get("target", {}) or {})
        merged.update({key: value for key, value in dict(target or {}).items() if value is not None})
        payload["target"] = merged
        return replace(self, payload=payload)

    def to_dict(self) -> Dict[str, Any]:
        action = self.action.to_dict()
        return {
            "id": self.id,
            "session_id": self.session_id,
            "mode": self.mode.value if self.mode else None,
            "status": self.status.value,
            "reason": self.reason,
            "payload": dict(self.payload),
            "created_at": self.created_at.isoformat().replace("+00:00", "Z"),
            "executed_at": self.executed_at.isoformat().replace("+00:00", "Z") if self.executed_at else None,
            "action": action,
            # Keep the explicit transport fields at the top level as well as
            # under ``action`` so browser clients can consume either shape.
            **action,
        }


class InterventionEngine:
    """Plan and execute interventions without bypassing behavior policy."""

    def __init__(self, policy: Optional[InterventionPolicy] = None):
        self.policy = policy or InterventionPolicy()

    def consider(
        self,
        observation: BehaviorObservation,
        session: ActivitySession,
        intent: Optional[Intent] = None,
        classification: Optional[Classification] = None,
        recent: Iterable[Intervention] = (),
        now: Optional[datetime] = None,
        presence_state: str = "active",
        recent_outcomes: Iterable[Any] = (),
    ) -> Intervention:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        if not self.policy.enabled:
            return self._skipped(observation.session_id, "interventions disabled", now)
        if str(presence_state or "active").strip().lower() == "afk":
            # AFK veto: presence is device truth. Never intervene at an
            # empty chair, no matter what the classifier said.
            return self._skipped(observation.session_id, "user is AFK; intervention vetoed", now)
        if observation.state != BehaviorState.DISTRACTED:
            return self._skipped(observation.session_id, "behavior state is not DISTRACTED", now)
        if self.policy.require_actionable and not observation.actionable:
            return self._skipped(observation.session_id, "distraction is below actionability threshold", now)
        cooldown = timedelta(seconds=self.policy.cooldown_seconds)
        for item in recent:
            if item.status in {InterventionStatus.PLANNED, InterventionStatus.EXECUTED} and item.created_at >= now - cooldown:
                return self._skipped(observation.session_id, "intervention cooldown is active", now)
        mode = self.policy.mode
        mode_window = timedelta(seconds=float(
            self.policy.mode_cooldowns.get(mode.value, self.policy.cooldown_seconds)))
        for item in recent:
            if (item.mode == mode
                    and item.status in {InterventionStatus.PLANNED, InterventionStatus.EXECUTED}
                    and item.created_at >= now - mode_window):
                return self._skipped(
                    observation.session_id,
                    "{} per-mode cooldown is active".format(mode.value), now)
        degraded = False
        streak = self._ineffective_streak(mode, recent_outcomes, now)
        if streak >= int(self.policy.max_ineffective_streak):
            if mode == InterventionMode.MEME:
                # De-escalate noise instead of repeating what failed.
                mode = InterventionMode.NOTIFICATION
                degraded = True
            else:
                return self._skipped(
                    observation.session_id,
                    "backing off after {} ineffective interventions".format(streak), now)

        goal = intent.goal if intent else "your active goal"
        distraction = (
            classification.topic or classification.category
            if classification
            else session_label(session, "this distraction")
        )
        severity = max(1, min(5, round(observation.distraction_score * 5)))
        target = browser_target(session)
        message = {
            "title": "BRO 💀" if mode == InterventionMode.MEME else "Noema",
            "body": "You've been drifting from: {}".format(goal),
        }
        actions = ("LOCK_IN", "DISMISS_WORKING", "DISMISS")
        if mode == InterventionMode.HOLDOUT:
            payload = {
                "duration_seconds": self.policy.holdout_seconds,
                "goal": goal,
                "severity": severity,
                "target": target,
                "message": message,
                # HOLDOUT is recorded but never delivered to a browser.
                "actions": (),
                "expires_in": max(0, round(self.policy.holdout_seconds * 1000)),
            }
        elif mode == InterventionMode.MEME:
            meme = {
                "template": "drake",
                "top": goal,
                "bottom": "Become a {} historian instead".format(distraction),
            }
            payload = {
                "template": "drake",
                "severity": severity,
                "top": goal,
                "bottom": meme["bottom"],
                "meme": meme,
                "target": target,
                "message": message,
                "actions": actions,
                "expires_in": 5000,
            }
        else:
            payload = {
                "title": "Noema",
                "message": message,
                "distraction": distraction,
                "severity": severity,
                "target": target,
                "actions": actions,
                "expires_in": 5000,
            }
        return Intervention(
            session_id=observation.session_id,
            mode=mode,
            status=InterventionStatus.PLANNED,
            reason=(
                "actionable distraction detected (de-escalated from MEME "
                "after repeated ineffective memes)"
                if degraded else "actionable distraction detected"
            ),
            payload=payload,
            created_at=now,
        )

    def _ineffective_streak(self, mode: InterventionMode,
                              recent_outcomes: Iterable[Any], now: datetime) -> int:
        """Count trailing NOT_RECOVERED outcomes for ``mode``.

        Newest first; a RECOVERED or PENDING outcome (or anything older
        than the lookback) ends the streak — unknown is not failure.
        """
        from noema.domain.outcomes import RecoveryStatus

        try:
            ordered = sorted(
                (item for item in recent_outcomes
                 if getattr(item, "intervention_time", None) is not None),
                key=lambda item: item.intervention_time,
                reverse=True,
            )
        except TypeError:
            return 0
        cutoff = now - timedelta(seconds=float(self.policy.ineffective_lookback_seconds))
        streak = 0
        for outcome in ordered:
            try:
                moment = outcome.intervention_time
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
            except (AttributeError, TypeError, ValueError):
                continue
            if moment < cutoff:
                break
            if str(getattr(outcome, "intervention_type", "") or "").upper() != mode.value:
                continue
            try:
                status = RecoveryStatus(getattr(outcome, "recovery_status"))
            except (AttributeError, TypeError, ValueError):
                break
            if status == RecoveryStatus.NOT_RECOVERED:
                streak += 1
            else:
                break
        return streak

    @staticmethod
    def _skipped(session_id: str, reason: str, now: datetime) -> Intervention:
        return Intervention(
            session_id=session_id,
            mode=None,
            status=InterventionStatus.SKIPPED,
            reason=reason,
            created_at=now,
        )

    def execute(
        self,
        intervention: Intervention,
        handler: Optional[Callable[[Intervention], Any]] = None,
        now: Optional[datetime] = None,
    ) -> Intervention:
        if intervention.status != InterventionStatus.PLANNED:
            raise ValueError("only planned interventions can be executed")
        if not self.policy.dry_run:
            if handler is None:
                raise ValueError("an intervention handler is required when dry_run is disabled")
            handler(intervention)
        executed_at = now or datetime.now(timezone.utc)
        if executed_at.tzinfo is None:
            executed_at = executed_at.replace(tzinfo=timezone.utc)
        payload = dict(intervention.payload)
        if self.policy.dry_run:
            payload["dry_run"] = True
        return replace(
            intervention,
            status=InterventionStatus.EXECUTED,
            payload=payload,
            executed_at=executed_at.astimezone(timezone.utc),
        )
