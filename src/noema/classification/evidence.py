"""Per-session evidence construction for semantic classification.

Pure mapping from sessions (+ recent context) to evidence payloads and
assembled prompts. No database, no model calls, no quota spend.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional

from noema.classification.contracts import SYSTEM_PROMPT
from noema.classification.policy import assess_policy
from noema.domain.session_context import is_meaningful_session

MAX_TEXT_FIELD_CHARS = 500


def _truncate_text(value: Any, limit: int = MAX_TEXT_FIELD_CHARS) -> Any:
    return value

def _compact_context_item(item: Mapping[str, Any]) -> Dict[str, Any]:
    compact: Dict[str, Any] = {}
    for key in (
        "application", "app", "window_title", "title", "domain",
        "url", "duration_seconds", "device", "browser",
        "browser_window_id", "browser_tab_id", "event_count",
    ):
        if item.get(key) is not None:
            compact[key] = item.get(key)
    return compact

def _observed_values(recent_context: Iterable[Mapping[str, Any]], keys: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for item in recent_context or []:
        if not isinstance(item, Mapping):
            continue
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in seen:
                seen.append(value.strip())
    return seen

def _fit_prompt(context: Dict[str, Any]) -> Dict[str, Any]:
    return dict(context)

def build_evidence(session: Any, recent_context: Optional[Iterable[Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """Build the evidence payload without the system prompt.

    Batch classification sends one shared instruction header plus one
    evidence object per session, so the system text is not repeated per
    item and the token budget holds far more sessions.
    """
    if is_meaningful_session(session):
        raw_recent = [_compact_context_item(item) for item in (recent_context or []) if isinstance(item, Mapping)]
        observed_apps = _observed_values(raw_recent, ("application", "app"))
        observed_titles = _observed_values(raw_recent, ("window_title", "title"))
        observed_domains = _observed_values(raw_recent, ("domain",))
        observed_urls = _observed_values(raw_recent, ("url",))
        try:
            active_seconds = float(
                getattr(session, "active_duration_seconds", 0)
                or session.duration)
        except (TypeError, ValueError):
            active_seconds = 0.0
        policy_prior = assess_policy(
            observed_apps[0] if observed_apps else None,
            observed_titles[0] if observed_titles else None,
            observed_domains[0] if observed_domains else None,
            observed_urls[0] if observed_urls else None,
            active_seconds,
            observed_apps,
        ).to_dict()
        context = {
            "session_type": "meaningful",
            "start_time": session.start_time.isoformat(),
            "end_time": session.end_time.isoformat(),
            "duration_seconds": session.duration,
            # Active (interacted) seconds the verdict must describe.
            # AFK time inside the span is excluded from classification.
            "active_duration_seconds": round(float(
                getattr(session, "active_duration_seconds", 0) or 0), 1),
            "presence": "active",
            "device_set": list(session.device_set),
            "browser": session.browser,
            "browser_window_id": session.browser_window_id,
            "browser_tab_id": session.browser_tab_id,
            "raw_activity_session_ids": list(session.activity_session_ids),
            "primary_project": _truncate_text(session.primary_project),
            "primary_task": _truncate_text(session.primary_task),
            "primary_topic": _truncate_text(session.primary_topic),
            "activities": list(session.activities),
            "primary_app": getattr(session, "app", None) or (observed_apps[0] if observed_apps else None),
            "primary_domain": getattr(session, "domain", None) or (observed_domains[0] if observed_domains else None),
            "primary_title": _truncate_text(getattr(session, "title", None) or (observed_titles[0] if observed_titles else None)),
            "observed_applications": observed_apps,
            "observed_titles": observed_titles,
            "observed_domains": observed_domains,
            "observed_urls": observed_urls,
            "event_count": getattr(session, "event_count", 0),
            "device": getattr(session, "device", None),
            "source": getattr(session, "source", "meaningful_session"),
            "evidence": [_truncate_text(e) for e in list(session.evidence)],
            "evidence_quality": getattr(session, "evidence_quality", None).value if hasattr(session, 'evidence_quality') and session.evidence_quality else None,
            # Deterministic user policy prior (Class A/B/C + games).
            # Observed input only: the model still decides from the
            # full evidence, but policy priors are explicit and auditable.
            "policy_prior": policy_prior,
        }
        context["recent_context"] = raw_recent
        context["raw_activity_evidence"] = raw_recent
    else:
        context = {
            "session_type": "activity",
            "app": _truncate_text(session.app),
            "domain": session.domain,
            "title": _truncate_text(session.title),
            "url": _truncate_text(getattr(session, "url", None)),
            "device": session.device,
            "duration_seconds": session.duration,
            "event_count": session.event_count,
            "source": session.source,
        }
        recent_items = [item for item in (recent_context or []) if isinstance(item, Mapping)]
            # Member-of-episode envelope (see classification.queue.member_context):
        # the group this item was tokenized from, stated explicitly so
        # the model never has to guess group boundaries. Episode keys
        # are reserved here and never treated as neighboring activity.
        episode = next((item for item in recent_items if "episode_task" in item), None)
        if episode is not None:
            context["episode"] = {
                "task": _truncate_text(episode.get("episode_task")),
                "topic": _truncate_text(episode.get("episode_topic")),
                "project": _truncate_text(episode.get("episode_project")),
            }
        context["recent_context"] = [
            _compact_context_item(item)
            for item in recent_items if "episode_task" not in item
        ]
        
    context = _fit_prompt(context)
    return context

def build_prompt(session: Any, recent_context: Optional[Iterable[Mapping[str, Any]]] = None) -> str:
    context = build_evidence(session, recent_context)
    return SYSTEM_PROMPT + "\nCONTEXT PAYLOAD:\n" + json.dumps(context, ensure_ascii=False, sort_keys=True)
