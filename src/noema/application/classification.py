"""Temporary compatibility shim (Phase 2 extraction).

The classifier implementation now lives in :mod:`noema.classification.core`.
This module contains NO logic; it only re-exports the previous public names
so external importers keep working during staged migration. Production code
must import from ``noema.classification`` instead. The shim is removed once
no production code depends on this path.
"""

from noema.classification.core import (
    CLASSIFIER_VERSION,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Classification,
    ClassificationJobTelemetry,
    Classifier,
)

__all__ = [
    "CLASSIFIER_VERSION",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "Classification",
    "ClassificationJobTelemetry",
    "Classifier",
]
