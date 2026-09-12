"""Persistent detection records for the real-time path.

Only meaningful detection decisions are stored — candidate entries,
verification outcomes, intervention triggers, recoveries — never every
detector tick. IDs are deterministic (window + session + score +
decision) so a retried evaluation cannot create duplicates.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def detection_id(timestamp: Any, session_id: Optional[str], candidate_score: float,
                 decision: str) -> str:
    try:
        stamp = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
    except (AttributeError, TypeError, ValueError):
        stamp = str(timestamp)
    raw = "{}|{}|{:.3f}|{}".format(stamp, session_id or "", float(candidate_score), decision)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _coerce_moment(value: Any) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        from noema.domain.activity import coerce_timestamp

        moment = coerce_timestamp(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class DetectionRecord:
    detection_id: str
    timestamp: datetime
    session_id: Optional[str]
    window_start: datetime
    window_end: datetime
    window_minutes: int
    feature_version: str
    candidate_score: float
    signals_json: str
    tracker_state: str
    model_verdict: Optional[str]
    model_confidence: Optional[float]
    severity: Optional[int]
    recommended_intervention: Optional[str]
    provider: Optional[str]
    model: Optional[str]
    latency_ms: Optional[float]
    decision: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        def _iso(value: Optional[datetime]) -> Optional[str]:
            return value.isoformat().replace("+00:00", "Z") if value else None

        import json as _json

        try:
            signals = _json.loads(self.signals_json or "{}")
        except ValueError:
            signals = {}
        return {
            "detection_id": self.detection_id,
            "timestamp": _iso(self.timestamp),
            "session_id": self.session_id,
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "window_minutes": self.window_minutes,
            "feature_version": self.feature_version,
            "candidate_score": round(float(self.candidate_score), 3),
            "signals": signals,
            "tracker_state": self.tracker_state,
            "model_verdict": self.model_verdict,
            "model_confidence": self.model_confidence,
            "severity": self.severity,
            "recommended_intervention": self.recommended_intervention,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "decision": self.decision,
            "reason": self.reason,
        }

    @classmethod
    def from_row(cls, row: Any) -> "DetectionRecord":
        def _opt_float(value: Any) -> Optional[float]:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def _opt_int(value: Any) -> Optional[int]:
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return cls(
            detection_id=str(row["detection_id"]),
            timestamp=_coerce_moment(row["timestamp"]),
            session_id=row["session_id"],
            window_start=_coerce_moment(row["window_start"]),
            window_end=_coerce_moment(row["window_end"]),
            window_minutes=int(row["window_minutes"]),
            feature_version=str(row["feature_version"]),
            candidate_score=float(row["candidate_score"]),
            signals_json=str(row["signals_json"] or "{}"),
            tracker_state=str(row["tracker_state"]),
            model_verdict=row["model_verdict"],
            model_confidence=_opt_float(row["model_confidence"]),
            severity=_opt_int(row["severity"]),
            recommended_intervention=row["recommended_intervention"],
            provider=row["provider"],
            model=row["model"],
            latency_ms=_opt_float(row["latency_ms"]),
            decision=str(row["decision"]),
            reason=str(row["reason"]),
        )
