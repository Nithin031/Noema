"""First-class observability for Noema: metrics, events, benchmarks.

The layer observes the product; it never decides for it. Classifier,
detector, intervention, and provider code emit standardized invocation
events through :class:`TelemetryRecorder`; this package records them and
answers which model ran, why, how long it took, what it cost in tokens,
and whether it worked. No prompts, responses, keys, or private activity
text are ever stored.
"""

from noema.observability.benchmark import (
    BENCHMARK_TEST_SET,
    FIXTURES,
    BenchmarkRunner,
    build_prompt,
    compare_runs,
    format_delta,
    format_report,
    validate_schema,
)
from noema.observability.models import (
    BenchmarkResult,
    BenchmarkRun,
    CostBasis,
    InvocationRecord,
    OperationRecord,
    Pipeline,
    Purpose,
    TokenBasis,
    invocation_id,
    utcnow_iso,
)
from noema.observability.percentile import (
    deltas,
    percentile,
    rate,
    summarize_latencies,
)
from noema.observability.provider_telemetry import (
    ProviderResponseMetadata,
    assemble_invocation,
    classify_error,
    cost_for_model,
    quota_scope_for,
    take_usage,
)
from noema.observability.recorder import (
    TelemetryRecorder,
    new_request_id,
)
from noema.observability.repository import (
    OBSERVABILITY_TABLES,
    TelemetryRepository,
)

__all__ = [
    "BENCHMARK_TEST_SET",
    "BenchmarkResult",
    "BenchmarkRun",
    "BenchmarkRunner",
    "CostBasis",
    "FIXTURES",
    "InvocationRecord",
    "OperationRecord",
    "OBSERVABILITY_TABLES",
    "Pipeline",
    "ProviderResponseMetadata",
    "Purpose",
    "TelemetryRecorder",
    "TelemetryRepository",
    "assemble_invocation",
    "build_prompt",
    "compare_runs",
    "quota_scope_for",
    "TokenBasis",
    "classify_error",
    "cost_for_model",
    "deltas",
    "format_delta",
    "format_report",
    "invocation_id",
    "new_request_id",
    "percentile",
    "rate",
    "summarize_latencies",
    "take_usage",
    "utcnow_iso",
    "validate_schema",
]
