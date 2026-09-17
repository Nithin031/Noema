"""Semantic classification subsystem.

This is the single obvious place to inspect Noema's semantic
classification pipeline: evidence construction, prompt contracts,
batched per-member judging, parsing, validation, queue eligibility,
coverage/provenance resolution, persistence contract, scheduling
policy, and trace representation.

Import direction (enforced by tests): this package consumes domain
models, infrastructure interfaces (providers, database, observability),
and configuration. It must never import application orchestration
(``application.pipeline``), the daemon lifecycle (``runtime.daemon``),
API/dashboard code, or web code.
"""

from noema.classification.batching import (
    BATCH_MAX_OUTPUT_TOKENS,
    BATCH_OUTPUT_RESERVE_PER_ITEM,
    BatchTransport,
    pack_batches,
    valid_item,
)
from noema.classification.contracts import (
    BATCH_INSTRUCTIONS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
)
from noema.classification.core import (
    CLASSIFIER_VERSION,
    Classification,
    ClassificationJobTelemetry,
    Classifier,
)
from noema.classification.coverage import resolve_slice_verdict
from noema.classification.evidence import build_evidence, build_prompt
from noema.classification.extension import (
    ExtensionAssessment,
    ExtensionProvenance,
    assess_extension,
)
from noema.classification.queue import (
    CLASSIFICATION_RETRIES_MAX,
    CLASSIFIER_VERSION_DEFAULT,
    MAX_MEMBER_ITEMS_PER_EPISODE,
    MAX_MEMBER_SIBLINGS,
    apply_retry_accounting,
    episode_members,
    expand_classification_items,
    is_afk_only,
    is_eligible,
    member_context,
    pending_classification_sessions,
    retry_delay_minutes,
)
from noema.classification.repository import ClassificationRepository
from noema.classification.scheduler import (
    build_batch_id,
    classification_window,
    completed_window,
    deduplicate_candidates,
    select_chunk,
)
from noema.classification.trace import ClassificationTrace

__all__ = [
    "BATCH_INSTRUCTIONS",
    "BATCH_MAX_OUTPUT_TOKENS",
    "BATCH_OUTPUT_RESERVE_PER_ITEM",
    "CLASSIFIER_VERSION",
    "CLASSIFIER_VERSION_DEFAULT",
    "CLASSIFICATION_RETRIES_MAX",
    "MAX_MEMBER_ITEMS_PER_EPISODE",
    "MAX_MEMBER_SIBLINGS",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "BatchTransport",
    "Classification",
    "ClassificationJobTelemetry",
    "ClassificationRepository",
    "ClassificationTrace",
    "Classifier",
    "ExtensionAssessment",
    "ExtensionProvenance",
    "apply_retry_accounting",
    "assess_extension",
    "build_batch_id",
    "build_evidence",
    "build_prompt",
    "classification_window",
    "completed_window",
    "deduplicate_candidates",
    "episode_members",
    "expand_classification_items",
    "is_afk_only",
    "is_eligible",
    "member_context",
    "pack_batches",
    "pending_classification_sessions",
    "resolve_slice_verdict",
    "retry_delay_minutes",
    "select_chunk",
    "valid_item",
]
