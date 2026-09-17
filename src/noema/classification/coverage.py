"""Verdict coverage resolution for timeline slices (moved verbatim logic).

Decides, for one slice of observed activity, which stored verdict (if any)
covers it and with what epistemic provenance. Dashboard assembly (totals,
tables, buckets) stays in the application layer; this module owns only the
coverage decision: direct verdict, verified extension via the licensing
gate, or no coverage.

Pure function of mappings: no database, no model calls.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from noema.classification.extension import ExtensionProvenance, assess_extension


def resolve_slice_verdict(
    classifications: Mapping[str, Any],
    lineage: Optional[Mapping[str, Any]],
    session_id: Any,
    evidence: Mapping[str, Any],
) -> Tuple[Any, str, Optional[str]]:
    """Resolve ``(verdict_or_None, provenance, inherited_from)`` for a slice.

    ``classifications`` maps session ids to verdict objects (already fanned
    so member sessions point at their episode's verdict where one exists).
    ``lineage`` carries parents/inherited/episodes/apps/domains as assembled
    by the caller. ``evidence`` describes this slice only (app/title/domain/
    url/seconds/start/end/window_id/tab_id).
    """
    verdicts = classifications or {}
    classification = verdicts.get(session_id)
    info = lineage or {}
    parents = info.get("parents", {})
    inherited = info.get("inherited", set())
    episodes = info.get("episodes", {})
    apps = info.get("apps", {})
    domains = info.get("domains", {})
    parent_id = parents.get(session_id)
    provenance = ExtensionProvenance.NONE
    inherited_from = None
    if classification is not None and session_id not in inherited:
        # The model evaluated this unit's own evidence directly.
        provenance = ExtensionProvenance.DIRECT
    elif classification is not None and parent_id is not None:
        # Candidate extension: the verdict may cover this slice ONLY if
        # the deterministic gate positively verifies continuity. Absence
        # of contradiction is never enough; doubt strips the verdict.
        episode = episodes.get(parent_id)
        assessment = assess_extension(
            evidence,
            {
                "category": classification.category,
                "productivity": classification.productivity,
                "topic": classification.topic,
                "activity": classification.activity,
                "prompt_version": classification.prompt_version,
                "classifier_version": classification.classifier_version,
            },
            {
                "primary_task": getattr(episode, "primary_task", None),
                "primary_topic": getattr(episode, "primary_topic", None),
                "start": getattr(episode, "start_time", None),
                "end": getattr(episode, "end_time", None),
                "browser_window_id": getattr(episode, "browser_window_id", None),
                "browser_tab_id": getattr(episode, "browser_tab_id", None),
            } if episode is not None else {},
            set(apps.get(parent_id, set())),
            set(domains.get(parent_id, set())),
        ) if episode is not None else None
        if assessment is not None and assessment.licensed:
            provenance = assessment.provenance
            inherited_from = parent_id
        else:
            # Not licensed: the slice keeps no verdict. It aggregates as
            # unclassified time and displays as pending/unknown.
            classification = None
    return classification, provenance, inherited_from
