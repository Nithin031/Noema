"""Semantic classification subsystem.

This is the single obvious place to inspect Noema's semantic
classification pipeline: evidence construction, prompt contracts,
batched per-member judging, parsing, validation, queue eligibility,
coverage/provenance resolution, and verdict persistence.

Import direction (enforced by tests): this package consumes domain
models, infrastructure interfaces (providers, database, observability),
and configuration. It must never import application orchestration
(``application.pipeline``), the daemon lifecycle (``runtime.daemon``),
API/dashboard code, or web code.
"""
