"""Deterministic verdict-extension licensing (semantic V3).

A model verdict describes the evidence the model actually saw. Reusing it
for evidence it never saw — a new interval, a new member session — is only
licensed here when a deterministic local assessor establishes POSITIVE
SEMANTIC CONTINUITY. Absence of contradiction is never sufficient on its
own; that would recreate the original fan-out problem where one verdict
silently covered unrelated activity.

This package is pure: mappings in, assessment out. No database, no model
calls, no quota spend. Quota efficiency is structural — the gate only ever
permits *fewer* model calls (by safely covering coherent intervals) and
never requires new ones.
"""

from noema.domain.semantic.extension import (
    ExtensionAssessment,
    ExtensionProvenance,
    assess_extension,
)

__all__ = [
    "ExtensionAssessment",
    "ExtensionProvenance",
    "assess_extension",
]
