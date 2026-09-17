"""Structured classification trace representation (Phase 2 scaffold).

Assembly-only: this module defines the shape of a full decision trace —
what went into the model, what came back, how it was parsed/validated/
stored — so a later forensic phase can capture and persist it. Phase 2
wires NOTHING: no persistence, no telemetry change, no secret capture.

Secrets (API keys, credentials, environment) must never be placed in a
trace. Prompts/evidence stored here are the already-assembled request
bodies; transport-level auth stays in infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


@dataclass
class ClassificationTrace:
    """One classified unit's end-to-end decision record (in memory only)."""

    # What was judged.
    session_id: str = ""
    input: Mapping[str, Any] = field(default_factory=dict)
    # What the model saw.
    evidence: Mapping[str, Any] = field(default_factory=dict)
    prompt: Optional[str] = None
    token_measurement: Optional[Mapping[str, Any]] = None
    batch_metadata: Optional[Mapping[str, Any]] = None
    # Who served it.
    provider: Optional[str] = None
    model: Optional[str] = None
    # What came back.
    raw_response: Optional[Any] = None
    parsed_response: Optional[Mapping[str, Any]] = None
    validated_result: Optional[Any] = None
    # Where it landed.
    persistence_metadata: Optional[Mapping[str, Any]] = None
    coverage_metadata: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Plain mapping for logging/debugging (never persist secrets)."""
        return {
            "session_id": self.session_id,
            "input": dict(self.input or {}),
            "evidence": dict(self.evidence or {}),
            "prompt": self.prompt,
            "token_measurement": (
                dict(self.token_measurement) if self.token_measurement else None
            ),
            "batch_metadata": (
                dict(self.batch_metadata) if self.batch_metadata else None
            ),
            "provider": self.provider,
            "model": self.model,
            "raw_response": self.raw_response,
            "parsed_response": (
                dict(self.parsed_response) if self.parsed_response else None
            ),
            "validated_result": self.validated_result,
            "persistence_metadata": (
                dict(self.persistence_metadata)
                if self.persistence_metadata
                else None
            ),
            "coverage_metadata": (
                dict(self.coverage_metadata) if self.coverage_metadata else None
            ),
        }
