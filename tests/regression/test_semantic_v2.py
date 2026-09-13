"""Semantic V2 regression coverage.

These tests pin the core correctness contracts of the goal-relevance /
alignment layer:

* Goal relevance is separate from the raw semantic category.
* ``productive`` never automatically means ``aligned``; ``distractive`` never
  automatically means ``misaligned``.
* Insufficient evidence -> ``unknown`` (never ``misaligned``, never drift).
* No active goal -> ``unknown`` alignment (and no drift).
* Failed classifications cannot drive alignment or behavior.
* Cache identity is safe across prompt/classifier version changes.
* Raw-session fanout preserves the raw -> episode -> classification lineage
  and never masquerades as many independent AI judgments.
"""

from datetime import datetime, timezone

import noema.application.classification as classification_mod
from noema.application.classification import Classification, Classifier
from noema.domain.behavior import BehaviorEngine, BehaviorState
from noema.domain.intent import (
    AlignmentResult,
    GoalAligner,
    Intent,
    RELATION_ALIGNED,
    RELATION_MISALIGNED,
    RELATION_UNKNOWN,
    RELEVANCE_NONE,
    RELEVANCE_UNKNOWN,
)
from noema.domain.sessions import ActivitySession
from noema.domain.meaningful import MeaningfulSession
from noema.domain.meaningful.models import EvidenceQuality
from noema.infrastructure.database import SQLiteStore
from noema.api import NoemaService
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter


ROBOTICS_GOAL = Intent(
    text="Implement YOLO crater detection for the rover",
    goal="Implement YOLO crater detection",
    topic="computer vision",
    project="Rover",
    keywords=("yolo", "crater", "detection"),
    created_at=datetime(2026, 9, 4, 9, tzinfo=timezone.utc),
)


def _session(app="Chrome", domain=None, title=None, duration=600):
    start = "2026-09-04T10:00:00Z"
    end = "2026-09-04T10:10:00Z"
    return ActivitySession(
        start=start, end=end, device="laptop",
        app=app, domain=domain, title=title,
        event_keys=(app + (domain or "") + (title or ""),),
    )


def _classified(session, **kw):
    base = dict(
        session_id=session.id,
        category="productive",
        productivity="productive",
        confidence=0.9,
        classification_status="classified",
        provider="gemini",
        model="gemini-test",
        source="gemini",
        evidence_quality="strong",
    )
    base.update(kw)
    return Classification(**base)


# --- goal relevance vs category ------------------------------------------


def test_goal_related_coding_is_aligned_high_relevance():
    session = _session(app="VS Code", title="train.py YOLO crater detector")
    classification = _classified(
        session, category="development", activity_type="coding",
        topic="yolo crater detection", project="Rover")
    result = GoalAligner().align(session, ROBOTICS_GOAL, classification)
    assert result.relation == RELATION_ALIGNED
    assert result.aligned is True
    assert result.goal_relevance in {"high", "medium"}


def test_productive_but_unrelated_is_misaligned_not_aligned():
    # Productive in general (reading an ML article) but nothing to do with the
    # active goal, with a confident well-evidenced verdict -> MISALIGNED.
    session = _session(app="Chrome", domain="arxiv.org",
                       title="A survey of reinforcement learning for finance")
    classification = _classified(
        session, category="productive", activity_type="reading",
        topic="reinforcement learning finance", project=None,
        confidence=0.9, evidence_quality="strong")
    result = GoalAligner().align(session, ROBOTICS_GOAL, classification)
    assert result.relation == RELATION_MISALIGNED
    assert result.goal_relevance == RELEVANCE_NONE
    assert result.aligned is False


def test_distractive_without_evidence_is_unknown_not_misaligned():
    # A distractive-looking activity with THIN evidence must not become a
    # misalignment: distractive != misaligned without evidence.
    session = _session(app="Firefox", title="Google", duration=4)
    thin = _classified(
        session, category="neutral", productivity="neutral",
        activity_type="browsing", confidence=0.2, evidence_quality="weak")
    result = GoalAligner().align(session, ROBOTICS_GOAL, thin)
    assert result.relation == RELATION_UNKNOWN
    assert result.goal_relevance == RELEVANCE_UNKNOWN


def test_insufficient_evidence_is_unknown():
    session = _session(app="Firefox", title="Google", duration=3)
    # No classification at all, and no goal-token overlap -> unknown.
    result = GoalAligner().align(session, ROBOTICS_GOAL, None)
    assert result.relation == RELATION_UNKNOWN


def test_clearly_unrelated_with_strong_evidence_is_misaligned():
    session = _session(app="Chrome", domain="espncricinfo.com",
                       title="India vs Australia — live cricket score")
    classification = _classified(
        session, category="distractive", productivity="distracting",
        activity_type="sports_browsing", topic="cricket",
        confidence=0.85, evidence_quality="strong")
    result = GoalAligner().align(session, ROBOTICS_GOAL, classification)
    assert result.relation == RELATION_MISALIGNED


# --- behavior downstream --------------------------------------------------


def test_no_goal_produces_unknown_alignment_and_no_drift():
    # No alignment passed at all (no active goal). A productive session must
    # not drift and must not falsely read FOCUSED.
    session = _session(app="VS Code", title="main.py")
    classification = _classified(session, category="development",
                                 activity_type="coding")
    obs = BehaviorEngine().evaluate([session], {session.id: classification})[0]
    assert obs.state != BehaviorState.DRIFTING
    assert obs.state == BehaviorState.NORMAL


def test_unknown_alignment_does_not_drift():
    session = _session(app="VS Code", title="notes.md")
    classification = _classified(session, category="neutral",
                                 productivity="neutral", activity_type="writing")
    unknown = AlignmentResult.unknown(session.id, ROBOTICS_GOAL.id)
    assert unknown.relation == RELATION_UNKNOWN
    obs = BehaviorEngine().evaluate(
        [session], {session.id: classification}, {session.id: unknown})[0]
    assert obs.state != BehaviorState.DRIFTING
    assert obs.distraction_score == 0.0


def test_explicit_misaligned_drifts():
    session = _session(app="Chrome", domain="news.example",
                       title="Unrelated news article")
    classification = _classified(session, category="productive",
                                 productivity="productive", activity_type="reading")
    misaligned = AlignmentResult(
        session.id, ROBOTICS_GOAL.id, aligned=False, score=0.0, confidence=0.8,
        reason="unrelated", relation=RELATION_MISALIGNED, goal_relevance="none")
    obs = BehaviorEngine().evaluate(
        [session], {session.id: classification}, {session.id: misaligned})[0]
    assert obs.state == BehaviorState.DRIFTING


def test_failed_classification_not_consumed_by_alignment_or_behavior():
    session = _session(app="Chrome", domain="instagram.com", title="Instagram")
    failed = Classification(
        session.id, category="distractive", productivity="distracting",
        confidence=0.9, evidence_quality="strong",
        classification_status="classification_failed")
    # Alignment: a failed verdict cannot produce a misalignment.
    result = GoalAligner().align(session, ROBOTICS_GOAL, failed)
    assert result.relation != RELATION_MISALIGNED
    # Behavior: a failed verdict is invisible -> NORMAL, not DISTRACTED.
    obs = BehaviorEngine().evaluate([session], {session.id: failed})[0]
    assert obs.state == BehaviorState.NORMAL


# --- backward compatibility ----------------------------------------------


def test_legacy_bare_not_aligned_derives_unknown_relation():
    # A result constructed the old way (aligned=False, no relation) must be
    # UNKNOWN, never MISALIGNED, so legacy rows never invent a distraction.
    legacy = AlignmentResult("s1", "i1", False, 0.0, 0.5, "off goal")
    assert legacy.relation == RELATION_UNKNOWN
    legacy_aligned = AlignmentResult("s2", "i1", True, 0.8, 0.8, "on goal")
    assert legacy_aligned.relation == RELATION_ALIGNED


# --- persistence ----------------------------------------------------------


def test_store_round_trips_relation_and_relevance():
    store = SQLiteStore()
    session = _session(app="VS Code", title="detector.py crater yolo")
    classification = _classified(session, category="development",
                                 activity_type="coding", topic="yolo")
    result = GoalAligner().align(session, ROBOTICS_GOAL, classification)
    assert store.insert_alignment(result) is True
    loaded = store.query_alignments(intent_id=ROBOTICS_GOAL.id)[0]
    assert loaded.relation == result.relation
    assert loaded.goal_relevance == result.goal_relevance
    assert loaded.alignment_confidence is not None
    store.close()


def test_goal_change_does_not_rewrite_old_alignment():
    # Alignment rows are keyed by (session, intent). A new goal produces a new
    # row; the old goal's alignment is untouched.
    store = SQLiteStore()
    session = _session(app="VS Code", title="detector.py crater yolo")
    classification = _classified(session, category="development",
                                 activity_type="coding", topic="yolo")
    first = GoalAligner().align(session, ROBOTICS_GOAL, classification)
    store.insert_alignment(first)

    other_goal = Intent(
        text="Study power electronics for the exam",
        goal="Study power electronics",
        keywords=("power", "electronics"),
        created_at=datetime(2026, 9, 5, 9, tzinfo=timezone.utc),
    )
    second = GoalAligner().align(session, other_goal, classification)
    store.insert_alignment(second)

    kept = store.query_alignments(intent_id=ROBOTICS_GOAL.id)
    assert len(kept) == 1
    assert kept[0].relation == first.relation
    assert kept[0].intent_id == ROBOTICS_GOAL.id
    store.close()


# --- cache identity -------------------------------------------------------


def test_cache_key_changes_with_prompt_version(monkeypatch):
    session = _session(app="VS Code", title="train.py")
    key_v2 = Classifier._cache_key(session, [])
    monkeypatch.setattr(classification_mod, "PROMPT_VERSION", "999")
    key_v999 = Classifier._cache_key(session, [])
    assert key_v2 != key_v999


def test_cache_key_changes_with_classifier_version(monkeypatch):
    session = _session(app="VS Code", title="train.py")
    key_a = Classifier._cache_key(session, [])
    monkeypatch.setattr(classification_mod, "CLASSIFIER_VERSION", "9")
    key_b = Classifier._cache_key(session, [])
    assert key_a != key_b


def test_distinct_sessions_do_not_collide_in_cache():
    a = _session(app="VS Code", title="a.py")
    b = _session(app="VS Code", title="b.py")
    assert Classifier._cache_key(a, []) != Classifier._cache_key(b, [])


# --- lineage / fanout -----------------------------------------------------


def test_raw_fanout_preserves_episode_lineage_not_independent_judgments():
    """One episode verdict rendered on many raw rows keeps its lineage.

    Regression against the old "neutral 40%" phenomenon: the UI may show raw
    sessions, but the data model must preserve
    raw_session -> meaningful_episode -> classification, and only the episode
    carries the single AI judgment.
    """
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)

    raw_ids = []
    for minute in range(4):
        raw = ActivitySession(
            start="2026-09-04T10:0{}:00Z".format(minute),
            end="2026-09-04T10:0{}:30Z".format(minute),
            device="laptop", app="VS Code", title="detector.py",
            event_keys=("raw-{}".format(minute),),
        )
        store.insert_session(raw)
        raw_ids.append(raw.id)

    episode = MeaningfulSession(
        start_time="2026-09-04T10:00:00Z",
        end_time="2026-09-04T10:04:00Z",
        device_set=("laptop",),
        activity_session_ids=tuple(raw_ids),
        primary_project="Rover",
        evidence_quality=EvidenceQuality.STRONG,
    )
    store.insert_meaningful_session(episode)
    # Exactly ONE classification: on the episode.
    store.insert_classification(Classification(
        episode.id, category="development", activity_type="coding",
        productivity="productive", confidence=0.9,
        classification_status="classified",
        provider="gemini", model="gemini-test", source="gemini"))

    rows = service.recent_activity(
        start="2026-09-04T09:00:00Z", end="2026-09-04T11:00:00Z")
    raw_rows = [r for r in rows if r["id"] in raw_ids]
    assert len(raw_rows) == 4
    for r in raw_rows:
        # Every raw row points back to the same episode ...
        assert r["meaningful_session_id"] == episode.id
        # ... and its verdict is explicitly inherited, not independent.
        assert r["inherited_from"] == episode.id
        assert r["productivity"] == "productive"
    # The four raw rows share one and only one underlying verdict.
    assert len({r["inherited_from"] for r in raw_rows}) == 1
    store.close()
