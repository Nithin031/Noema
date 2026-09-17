"""Semantic V3 adversarial regression suite.

These tests exist to break the classification pipeline, not to demonstrate
it. The central one is `test_forensic_078_fanout_is_structurally_impossible`:
it reproduces the exact historical failure (a four-minute PRODUCTIVE 0.78
verdict fanning out across twenty minutes of unrelated activity) and proves
the shape of it can no longer occur.

Invariants pinned here:

    NO FAILED CLASSIFICATION MAY BECOME A VERDICT
    NO UNCERTAIN CLASSIFICATION MAY BECOME AN INTERVENTION
    NO STALE VERDICT MAY BE PRESENTED AS CURRENT
    NO INHERITED VERDICT MAY BE PRESENTED AS DIRECT
    NO CLASSIFICATION MAY EXIST WITHOUT AN EVIDENCE WINDOW
    NO CACHE HIT MAY BYPASS SEMANTIC IDENTITY
    NO MODEL OUTPUT MAY BYPASS VALIDATION
"""

from datetime import datetime, timedelta, timezone

import pytest

from noema.api import NoemaService
from noema.application.classification import (
    CONFIDENCE_CEILING_BY_EVIDENCE,
    Classification,
    Classifier,
    ModelOutputError,
    validate_model_verdict,
)
from noema.domain.behavior import BehaviorEngine, BehaviorObservation, BehaviorState
from noema.domain.intervention import (
    InterventionEngine,
    InterventionPolicy,
    InterventionStatus,
)
from noema.domain.meaningful import MeaningfulSession
from noema.domain.semantic import (
    EvidenceChangeDetector,
    EvidenceWindow,
    Freshness,
    Provenance,
    SemanticSegment,
    assess_freshness,
    build_segments,
    resolve_for_interval,
)
from noema.domain.sessions import ActivitySession
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore


DAY = "2026-09-04"


def at(hhmm, seconds=0):
    return "{}T{}:{:02d}Z".format(DAY, hhmm, seconds)


def raw(app, title, start, end, domain=None, key=None):
    return ActivitySession(
        start=start, end=end, device="laptop", app=app, title=title,
        domain=domain, event_keys=(key or "{}-{}".format(app, start),),
    )


def obs(app, title, start, end, domain=None, session_id=None):
    delta = (datetime.fromisoformat(end.replace("Z", "+00:00"))
             - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    return {
        "session_id": session_id, "application": app, "title": title,
        "domain": domain, "start": start, "end": end, "timestamp": start,
        "duration_seconds": delta,
    }


def verdict(session_id, category="productive", confidence=0.78,
            window=("21:16", "21:20"), **kw):
    base = dict(
        session_id=session_id, category=category,
        productivity="distracting" if category == "distractive" else category,
        confidence=confidence, classification_status="classified",
        provider="gemini", model="gemini-3.5-flash-lite", source="gemini",
        evidence_quality="strong",
        evidence_start=at(window[0]), evidence_end=at(window[1]),
        evidence_digest="d-{}".format(session_id),
        evidence_observation_count=3,
    )
    base.update(kw)
    return Classification(**base)


# =====================================================================
# THE FORENSIC CASE
# =====================================================================

def test_forensic_078_fanout_is_structurally_impossible():
    """The exact historical failure, replayed end to end.

    21:16 Firefox/Noema classified PRODUCTIVE 0.78 over four minutes; the
    episode then grows to 21:36 across Brave, VS Code, OpenCode, Explorer
    and Cinepolis. Not one of those later rows may carry 0.78.
    """
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)

    rows = [
        raw("Firefox", "Noema behavioral intelligence", at("21:16"), at("21:20"),
            domain="github.com", key="r-firefox"),
        raw("Brave", "Cinepolis - Book movie tickets", at("21:20"), at("21:24"),
            domain="cinepolis.com", key="r-brave"),
        raw("Code", "settings.json", at("21:24"), at("21:28"), key="r-code"),
        raw("OpenCode", "scratch buffer", at("21:28"), at("21:31"), key="r-opencode"),
        raw("Explorer", "Downloads", at("21:31"), at("21:33"), key="r-explorer"),
        raw("Brave", "Netflix - Friends S03E12", at("21:33"), at("21:36"),
            domain="netflix.com", key="r-netflix"),
    ]
    for row in rows:
        store.insert_session(row)

    episode = MeaningfulSession(
        start_time=at("21:16"), end_time=at("21:36"), device_set=("laptop",),
        activity_session_ids=tuple(row.id for row in rows),
        active_duration_seconds=1200.0,
    )
    store.insert_meaningful_session(episode)

    # Exactly one verdict, formed from the first four minutes only.
    store.insert_classification(verdict(episode.id))

    activity = service.recent_activity(at("21:00"), at("22:00"))
    by_app = {item["application"]: item for item in activity}

    # The Firefox row is inside the evidence window: it legitimately carries
    # the verdict, and says so is inherited.
    firefox = by_app["Firefox"]
    assert firefox["category"] == "productive"
    assert firefox["confidence"] == pytest.approx(0.78)
    assert firefox["provenance"] == Provenance.INHERITED.value
    assert firefox["inherited_from"] == episode.id

    # Every later row is outside it. None of them may carry the verdict.
    # (Application names are normalized for display: "Code" -> "Visual
    # Studio Code".)
    for name in ("Visual Studio Code", "OpenCode", "Explorer"):
        row = by_app[name]
        assert row["category"] is None, "{} inherited a verdict it never earned".format(name)
        assert row["confidence"] == 0.0
        assert row["classification_status"] == "pending"
        assert "not this interval" in row["lineage_reason"]

    # Brave appears twice (Cinepolis and Netflix); neither may be productive.
    brave_rows = [item for item in activity if item["application"] == "Brave"]
    assert len(brave_rows) == 2
    for row in brave_rows:
        assert row["category"] is None

    # The headline assertion: 0.78 appears exactly once in the whole view.
    assert sum(1 for item in activity
               if item["confidence"] == pytest.approx(0.78)) == 1

    store.close()


def test_forensic_case_after_revision_produces_honest_segments():
    """The same episode, once reconsidered, reads as three honest claims."""
    episode_id = "79bc95ca"
    first = verdict(episode_id, "productive", 0.78, ("21:16", "21:20"))
    prior_window = EvidenceWindow.from_observations(
        at("21:16"), at("21:20"), [{"application": "Firefox", "title": "Noema"}])
    second = verdict(episode_id, "distractive", 0.91, ("21:20", "21:36"))
    new_window = EvidenceWindow.from_observations(
        at("21:20"), at("21:36"), [{"application": "Brave", "title": "Netflix Friends"}])

    segments = build_segments(episode_id, first, prior_window, second, new_window)
    assert [item.category for item in segments] == ["productive", "distractive"]
    # The earlier claim keeps its own window: revision does not rewrite it.
    assert segments[0].evidence_end == datetime.fromisoformat(at("21:20").replace("Z", "+00:00"))
    assert segments[1].evidence_start == segments[0].evidence_end
    assert segments[0].provenance == Provenance.DIRECT.value
    assert segments[1].provenance == Provenance.RECLASSIFIED.value

    for moment, expected in ((at("21:18"), "productive"),
                             (at("21:24"), "distractive"),
                             (at("21:34"), "distractive")):
        resolved = resolve_for_interval(moment, moment, episode_verdict=first,
                                        segments=segments, episode_id=episode_id)
        assert resolved.verdict.category == expected


# =====================================================================
# 1-11: episode growth and semantic coherence
# =====================================================================

DETECTOR = EvidenceChangeDetector()
PRIOR_WINDOW = EvidenceWindow.from_observations(
    at("21:16"), at("21:20"),
    [{"application": "Firefox", "title": "Noema behavioral intelligence",
      "domain": "github.com"}])
PRIOR_VERDICT = verdict("ep", "productive", 0.78)
BASE_OBS = obs("Firefox", "Noema behavioral intelligence", at("21:16"), at("21:20"),
               "github.com")


def assess(observations):
    return DETECTOR.assess(PRIOR_WINDOW, PRIOR_VERDICT, observations,
                           current_active_seconds=sum(
                               item["duration_seconds"] for item in observations))


def test_1_productive_episode_growing_into_entertainment_reconsiders():
    result = assess([
        BASE_OBS,
        obs("Brave", "Netflix - Friends S03E12", at("21:20"), at("21:32"), "netflix.com"),
    ])
    assert result.should_reconsider
    assert "verdict_contradiction" in result.fired


def test_2_coherent_work_across_firefox_vscode_terminal_stays_coherent():
    """Multi-application growth is reconsidered once, but never contradicted."""
    result = assess([
        BASE_OBS,
        obs("Code", "pipeline.py - Noema", at("21:20"), at("21:28")),
        obs("WindowsTerminal", "pytest tests/ - Noema", at("21:28"), at("21:34")),
    ])
    assert result.should_reconsider, "a materially larger episode deserves one look"
    assert "verdict_contradiction" not in result.fired
    assert "prolonged_outside_cluster" not in result.fired


def test_3_browser_title_changes_but_task_constant_costs_nothing():
    result = assess([
        BASE_OBS,
        obs("Firefox", "Noema - pull request 42 review", at("21:20"), at("21:26"), "github.com"),
        obs("Firefox", "Noema issues - github", at("21:26"), at("21:32"), "github.com"),
    ])
    assert not result.should_reconsider


def test_4_application_changes_but_task_constant_is_not_a_contradiction():
    result = assess([
        BASE_OBS,
        obs("Code", "noema/pipeline.py", at("21:20"), at("21:30")),
    ])
    assert "verdict_contradiction" not in result.fired


def test_5_same_application_task_changes_is_decisive():
    result = assess([
        BASE_OBS,
        obs("Firefox", "Netflix - Friends S03E12", at("21:20"), at("21:32"), "netflix.com"),
    ])
    assert result.should_reconsider
    assert "verdict_contradiction" in result.fired


def test_6_rapid_tab_flipping_on_one_topic_costs_nothing():
    flips = [BASE_OBS]
    for index in range(10):
        flips.append(obs("Firefox", "Noema docs part {}".format(index),
                         at("21:{}".format(20 + index)), at("21:{}".format(21 + index)),
                         "github.com"))
    assert not assess(flips).should_reconsider


def test_7_long_idle_does_not_age_a_verdict_by_itself():
    """Idle time is not new evidence; it must not buy a model call."""
    result = assess([BASE_OBS])
    assert not result.should_reconsider
    assert result.new_active_seconds == 0.0


def test_8_afk_only_session_is_never_queued():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    idle = MeaningfulSession(
        start_time=at("21:16"), end_time=at("21:36"), device_set=("laptop",),
        activity_session_ids=("afk-1",),
        active_duration_seconds=0.0, afk_duration_seconds=1200.0)
    assert service.pending_classification_sessions([idle], context_by_session={}) == []
    store.close()


def test_9_mixed_evidence_reconsiders_without_asserting_a_new_verdict():
    result = assess([
        BASE_OBS,
        obs("Code", "pipeline.py - Noema", at("21:20"), at("21:25")),
        obs("Brave", "Netflix - Friends", at("21:25"), at("21:30"), "netflix.com"),
        obs("Code", "store.py - Noema", at("21:30"), at("21:35")),
    ])
    assert result.should_reconsider


def test_10_thin_evidence_cannot_produce_a_strong_claim():
    fields = validate_model_verdict({
        "category": "productive", "productivity": "productive",
        "activity": "Browsing", "signal": "Firefox window",
        "confidence": 0.95, "evidence_quality": "weak",
    })
    assert fields["category"] == "uncertain"
    assert fields["confidence"] <= CONFIDENCE_CEILING_BY_EVIDENCE["weak"]


def test_11_conflicting_evidence_raises_uncertainty_not_confidence():
    result = assess([
        BASE_OBS,
        obs("Brave", "Netflix - Friends S03E12", at("21:20"), at("21:26"), "netflix.com"),
        obs("Code", "pipeline.py - Noema", at("21:26"), at("21:32")),
    ])
    assert result.should_reconsider
    assert "verdict_contradiction" in result.fired


# =====================================================================
# 12-15, 29-30: failure semantics
# =====================================================================

class Boom:
    name = "gemini"
    model = "gemini-3.5-flash-lite"

    def __init__(self, exc):
        self.exc = exc

    def classify(self, session, prompt):
        raise self.exc


@pytest.mark.parametrize("failure,label", [
    (TimeoutError("deadline exceeded"), "timeout"),
    (ConnectionError("429 Too Many Requests"), "429"),
    (ConnectionError("quota exhausted"), "quota"),
    (OSError("network unreachable"), "network"),
])
def test_12_to_15_provider_failures_never_become_verdicts(failure, label):
    session = raw("Firefox", "Noema", at("21:16"), at("21:20"))
    result = Classifier(provider=Boom(failure)).classify(session)
    assert result.classification_status != "classified", label
    assert result.category != "productive"
    assert not result.is_verdict


def test_14_malformed_model_json_is_rejected_not_softened():
    class Garbage:
        name = "gemini"
        model = "gemini-3.5-flash-lite"

        def classify(self, session, prompt):
            return {"response": "not json at all"}

    session = raw("Firefox", "Noema", at("21:16"), at("21:20"))
    result = Classifier(provider=Garbage()).classify(session)
    assert result.classification_status == "classification_failed"
    assert not result.is_verdict


@pytest.mark.parametrize("payload,reason", [
    ({"category": "amazing", "activity": "a", "signal": "b", "confidence": 0.5}, "enum"),
    ({"category": "productive", "activity": "a", "signal": "b", "confidence": 5}, "range"),
    ({"category": "productive", "activity": "a", "signal": "b", "confidence": -1}, "range"),
    ({"category": "productive", "activity": "a", "signal": "b", "confidence": "high"}, "type"),
    ({"category": "productive", "activity": "", "signal": "b", "confidence": 0.5}, "evidence"),
    ({"category": "productive", "activity": "a", "signal": "", "confidence": 0.5}, "evidence"),
    ({"category": "productive", "activity": "a", "signal": "b", "confidence": True}, "bool"),
    ({"category": "productive", "activity": "a", "signal": "b", "confidence": 0.5,
      "evidence_quality": "excellent"}, "grade"),
    ("not a mapping", "structure"),
], ids=lambda value: str(value)[:28])
def test_15_model_output_never_bypasses_validation(payload, reason):
    with pytest.raises(ModelOutputError):
        validate_model_verdict(payload)


def test_29_no_api_key_leaves_rows_retryable():
    session = raw("Firefox", "Noema", at("21:16"), at("21:20"))
    result = Classifier(provider=Boom(ConnectionError("GEMINI_API_KEY is not set"))).classify(session)
    assert result.classification_status != "classified"
    assert not result.is_verdict


def test_30_provider_unavailable_is_not_a_neutral_verdict():
    session = raw("Firefox", "Noema", at("21:16"), at("21:20"))
    result = Classifier(provider=Boom(ConnectionError("service unavailable"))).classify(session)
    assert result.classification_status != "classified"
    # The internal placeholder category must never be read as a judgment.
    assert not result.is_verdict


# =====================================================================
# 16-17, 27-28: cache identity
# =====================================================================

class Stub:
    name = "gemini"
    model = "gemini-3.5-flash-lite"

    def classify(self, session, prompt):
        return {"category": "productive", "productivity": "productive",
                "activity": "a", "signal": "b", "confidence": 0.8,
                "evidence_quality": "strong"}


def episode_at(end, members, episode_id="79bc95ca"):
    return MeaningfulSession(
        start_time=at("21:16"), end_time=end, device_set=("laptop",),
        activity_session_ids=members, session_id=episode_id)


def test_16_distinct_episodes_never_collide_in_cache():
    classifier = Classifier(provider=Stub())
    left = classifier._scoped_cache_key(episode_at(at("21:20"), ("r1",), "ep-a"), [])
    right = classifier._scoped_cache_key(episode_at(at("21:20"), ("r1",), "ep-b"), [])
    assert left != right


def test_17_a_grown_episode_is_a_different_cache_question():
    """The defect that made a stale hit possible at all."""
    classifier = Classifier(provider=Stub())
    before = classifier._scoped_cache_key(
        episode_at(at("21:20"), ("r1",)), [BASE_OBS])
    after = classifier._scoped_cache_key(
        episode_at(at("21:36"), ("r1", "r2")),
        [BASE_OBS, obs("Brave", "Netflix", at("21:20"), at("21:36"), "netflix.com")])
    assert before != after


def test_17b_same_bounds_different_evidence_is_a_different_question():
    classifier = Classifier(provider=Stub())
    left = classifier._scoped_cache_key(episode_at(at("21:20"), ("r1",)), [BASE_OBS])
    right = classifier._scoped_cache_key(
        episode_at(at("21:20"), ("r1",)),
        [obs("Firefox", "Netflix", at("21:16"), at("21:20"), "netflix.com")])
    assert left != right


def test_27_model_change_invalidates_cache_identity():
    class Other(Stub):
        model = "gemini-3.8-flash"

    episode = episode_at(at("21:20"), ("r1",))
    assert (Classifier(provider=Stub())._scoped_cache_key(episode, [BASE_OBS])
            != Classifier(provider=Other())._scoped_cache_key(episode, [BASE_OBS]))


def test_28_prompt_version_change_invalidates_cache_identity(monkeypatch):
    import noema.application.classification as mod

    classifier = Classifier(provider=Stub())
    episode = episode_at(at("21:20"), ("r1",))
    before = classifier._scoped_cache_key(episode, [BASE_OBS])
    monkeypatch.setattr(mod, "PROMPT_VERSION", "999")
    assert classifier._scoped_cache_key(episode, [BASE_OBS]) != before


# =====================================================================
# 18-22: revision, segments, provenance, presentation
# =====================================================================

def test_18_growth_reopens_a_classified_episode_for_revision():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    episode = episode_at(at("21:36"), ("r1", "r2"))
    store.insert_meaningful_session(episode)
    store.insert_classification(verdict(episode.id, window=("21:16", "21:20")))

    grown = [BASE_OBS,
             obs("Brave", "Netflix - Friends", at("21:20"), at("21:36"), "netflix.com")]
    pending = service.pending_classification_sessions(
        [episode], classifier_version="1",
        context_by_session={episode.id: grown})
    assert pending, "a grown, contradicted episode must be reconsidered"
    store.close()


def test_18b_an_unchanged_episode_is_never_reconsidered():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    episode = episode_at(at("21:20"), ("r1",))
    store.insert_meaningful_session(episode)
    store.insert_classification(verdict(episode.id, window=("21:16", "21:20")))
    assert service.pending_classification_sessions(
        [episode], classifier_version="1",
        context_by_session={episode.id: [BASE_OBS]}) == []
    store.close()


def test_19_segment_creation_binds_each_claim_to_its_own_window():
    store = SQLiteStore()
    segments = [
        SemanticSegment(episode_id="ep", sequence=0, start_time=at("21:16"),
                        end_time=at("21:20"), category="productive", confidence=0.78,
                        classification_status="classified",
                        evidence_start=at("21:16"), evidence_end=at("21:20")),
        SemanticSegment(episode_id="ep", sequence=1, start_time=at("21:20"),
                        end_time=at("21:36"), category="distractive", confidence=0.91,
                        classification_status="classified",
                        evidence_start=at("21:20"), evidence_end=at("21:36")),
    ]
    store.insert_semantic_segments(segments)
    stored = store.query_semantic_segments(episode_id="ep")
    assert [item.category for item in stored] == ["productive", "distractive"]
    assert stored[0].covers(at("21:18")) and not stored[0].covers(at("21:30"))
    # Recomputation replaces rather than accumulates.
    store.insert_semantic_segments(segments)
    assert len(store.query_semantic_segments(episode_id="ep")) == 2
    store.close()


def test_20_inherited_verdict_is_never_presented_as_direct():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    row = raw("Firefox", "Noema", at("21:16"), at("21:20"), "github.com", key="r-1")
    store.insert_session(row)
    episode = MeaningfulSession(
        start_time=at("21:16"), end_time=at("21:20"), device_set=("laptop",),
        activity_session_ids=(row.id,))
    store.insert_meaningful_session(episode)
    store.insert_classification(verdict(episode.id))

    item = service.recent_activity(at("21:00"), at("22:00"))[0]
    assert item["provenance"] == Provenance.INHERITED.value
    assert item["provenance"] != Provenance.DIRECT.value
    assert item["inherited_from"] == episode.id
    store.close()


def test_21_superseded_verdict_is_reported_invalidated():
    superseded = verdict("ep", superseded_at=at("21:40"))
    assert superseded.freshness_for(at("21:16"), at("21:20")) == Freshness.INVALIDATED.value


def test_22_dashboard_summary_never_counts_time_the_verdict_never_saw():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    covered = raw("Firefox", "Noema", at("21:16"), at("21:20"), "github.com", key="c")
    uncovered = raw("Brave", "Netflix", at("21:20"), at("21:36"), "netflix.com", key="u")
    for row in (covered, uncovered):
        store.insert_session(row)
    episode = MeaningfulSession(
        start_time=at("21:16"), end_time=at("21:36"), device_set=("laptop",),
        activity_session_ids=(covered.id, uncovered.id))
    store.insert_meaningful_session(episode)
    store.insert_classification(verdict(episode.id, window=("21:16", "21:20")))

    rows = service.recent_activity(at("21:00"), at("22:00"))
    productive = [item for item in rows if item["category"] == "productive"]
    assert len(productive) == 1
    assert productive[0]["application"] == "Firefox"
    store.close()


# =====================================================================
# 23-24: behavior and intervention safety
# =====================================================================

def session_for(start, end):
    return raw("Brave", "Netflix - Friends", start, end, "netflix.com", key="d")


def distracted(session):
    return BehaviorObservation(
        session_id=session.id, state=BehaviorState.DISTRACTED,
        started_at=session.start, confidence=0.9, distraction_score=0.9,
        actionable=True, reason="entertainment")


def test_23_behavior_ignores_a_verdict_that_does_not_cover_the_session():
    session = session_for(at("21:20"), at("21:36"))
    stale = verdict(session.id, "distractive", 0.91, ("21:16", "21:20"))
    observations = BehaviorEngine().evaluate([session], {session.id: stale})
    assert observations[0].state != BehaviorState.DISTRACTED


def test_23b_behavior_ignores_uncertain_verdicts():
    session = session_for(at("21:16"), at("21:20"))
    unsure = verdict(session.id, "uncertain", 0.3, ("21:16", "21:20"))
    observations = BehaviorEngine().evaluate([session], {session.id: unsure})
    assert observations[0].state != BehaviorState.DISTRACTED


def test_24_uncertain_classification_never_becomes_an_intervention():
    session = session_for(at("21:16"), at("21:20"))
    unsure = verdict(session.id, "uncertain", 0.4, ("21:16", "21:20"))
    result = InterventionEngine(InterventionPolicy()).consider(
        distracted(session), session, None, unsure,
        now=datetime.fromisoformat(at("21:21").replace("Z", "+00:00")))
    assert result.status == InterventionStatus.SKIPPED
    assert "uncertain" in result.reason


def test_24b_stale_classification_never_becomes_an_intervention():
    session = session_for(at("21:20"), at("21:36"))
    stale = verdict(session.id, "distractive", 0.91, ("21:16", "21:20"))
    result = InterventionEngine(InterventionPolicy()).consider(
        distracted(session), session, None, stale,
        now=datetime.fromisoformat(at("21:37").replace("Z", "+00:00")))
    assert result.status == InterventionStatus.SKIPPED
    assert "current verdict" in result.reason


def test_24c_failed_classification_never_becomes_an_intervention():
    session = session_for(at("21:16"), at("21:20"))
    failed = Classification(
        session_id=session.id, category="neutral",
        classification_status="classification_failed", provider="gemini",
        source="pending", last_error="429")
    result = InterventionEngine(InterventionPolicy()).consider(
        distracted(session), session, None, failed,
        now=datetime.fromisoformat(at("21:21").replace("Z", "+00:00")))
    assert result.status == InterventionStatus.SKIPPED


def test_24d_thin_evidence_never_becomes_an_intervention():
    session = session_for(at("21:16"), at("21:20"))
    thin = verdict(session.id, "distractive", 0.9, ("21:16", "21:20"),
                   evidence_quality="weak")
    result = InterventionEngine(InterventionPolicy()).consider(
        distracted(session), session, None, thin,
        now=datetime.fromisoformat(at("21:21").replace("Z", "+00:00")))
    assert result.status == InterventionStatus.SKIPPED
    assert "evidence quality" in result.reason


# =====================================================================
# 25-26: recovery and idempotence
# =====================================================================

def test_25_restart_with_an_open_episode_keeps_the_verdict_bounded():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    row = raw("Firefox", "Noema", at("21:16"), at("21:20"), "github.com", key="r-open")
    store.insert_session(row)
    episode = MeaningfulSession(
        start_time=at("21:16"), end_time=at("21:20"), device_set=("laptop",),
        activity_session_ids=(row.id,))
    store.insert_meaningful_session(episode)
    store.insert_classification(verdict(episode.id))

    # Simulate a restart: a brand new service over the same database.
    reopened = NoemaService(ActivityWatchAdapter(), store)
    stored = store.query_classifications(session_id=episode.id)[0]
    assert stored.evidence_window is not None
    assert not stored.covers(at("21:34"))
    assert reopened.recent_activity(at("21:00"), at("22:00"))[0]["category"] == "productive"
    store.close()


def test_26_duplicate_scheduler_runs_do_not_duplicate_lineage():
    store = SQLiteStore()
    entry = verdict("ep", revision=1)
    store.record_classification_revision(entry)
    store.record_classification_revision(entry)
    assert len(store.query_classification_revisions("ep")) == 1
    store.close()


# =====================================================================
# INVARIANTS
# =====================================================================

def test_invariant_every_new_verdict_carries_an_evidence_window():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    row = raw("Firefox", "Noema", at("21:16"), at("21:20"), "github.com", key="r-inv")
    store.insert_session(row)
    episode = MeaningfulSession(
        start_time=at("21:16"), end_time=at("21:20"), device_set=("laptop",),
        activity_session_ids=(row.id,))
    store.insert_meaningful_session(episode)

    service.classifier = Classifier(provider=Stub())
    results = service.classify_meaningful_sessions(
        [episode], context_by_session={episode.id: [BASE_OBS]})
    assert results
    for item in results:
        if item.is_verdict:
            assert item.evidence_window is not None
            assert item.evidence_start is not None and item.evidence_end is not None
    store.close()


def test_invariant_a_verdict_with_no_window_covers_nothing():
    unscoped = Classification(
        session_id="ep", category="productive", classification_status="classified",
        provider="gemini", source="gemini")
    assert unscoped.evidence_window is None
    assert not unscoped.covers(at("21:18"))
    assert unscoped.freshness_for(at("21:16"), at("21:36")) == Freshness.STALE.value


def test_invariant_no_verdict_resolves_to_an_explicit_absence():
    resolved = resolve_for_interval(at("21:34"), at("21:36"))
    assert resolved.verdict is None
    assert not resolved.presentable_as_current
    # An absent verdict is NOT a neutral verdict.
    assert resolved.reason


def test_invariant_stale_is_never_presentable_as_current():
    for freshness in (Freshness.STALE, Freshness.INVALIDATED):
        assert not resolve_for_interval(
            at("21:34"), at("21:36"),
            episode_verdict=verdict("ep", window=("21:16", "21:20")),
            episode_id="ep").presentable_as_current
    assert assess_freshness(PRIOR_WINDOW, at("21:16"), at("21:20")) == Freshness.FRESH


def test_invariant_uncertain_is_not_neutral():
    unsure = verdict("ep", "uncertain", 0.3)
    neutral = verdict("ep", "neutral", 0.6)
    assert unsure.is_uncertain and not neutral.is_uncertain
    assert unsure.is_verdict, "uncertain is a real, stored answer"
    assert not unsure.is_actionable_verdict, "but never grounds to act"
    assert neutral.is_actionable_verdict


# =====================================================================
# BOUNDARY SEMANTICS
#
# Found by adversarial replay: a zero-length row sitting exactly on a
# verdict's closing boundary inherited that verdict through the jitter
# tolerance. Tolerance exists to absorb sub-second clock skew between the
# sessionizer and the episode builder; it must never let a verdict reach
# content it never observed.
# =====================================================================

def test_boundary_row_at_window_end_does_not_inherit():
    """An event starting where a verdict's evidence stopped is the next thing."""
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    work = raw("Code", "pipeline.py - noema", at("17:00"), at("17:10"), key="w")
    blip = raw("Brave", "Netflix", at("17:10"), at("17:10", 20), "netflix.com", key="b")
    for row in (work, blip):
        store.insert_session(row)
    episode = MeaningfulSession(
        start_time=at("17:00"), end_time=at("17:10", 20), device_set=("laptop",),
        activity_session_ids=(work.id, blip.id))
    store.insert_meaningful_session(episode)
    store.insert_classification(verdict(
        episode.id, "productive", 0.86, window=("17:00", "17:10")))

    rows = {item["application"]: item for item in
            service.recent_activity(at("16:00"), at("18:00"))}
    assert rows["Visual Studio Code"]["category"] == "productive"
    assert rows["Brave"]["category"] is None, "a boundary-abutting row inherited a verdict"
    store.close()


def test_point_coverage_is_half_open_at_the_end():
    window = EvidenceWindow(start=at("17:00"), end=at("17:10"))
    assert window.covers_interval(at("17:00"), at("17:00"))
    assert window.covers_interval(at("17:05"), at("17:05"))
    # The closing instant belongs to whatever follows.
    assert not window.covers_interval(at("17:10"), at("17:10"))


def test_majority_of_an_interval_must_be_inside_the_window():
    window = EvidenceWindow(start=at("17:00"), end=at("17:10"))
    # Mostly inside: covered.
    assert window.covers_interval(at("17:02"), at("17:09"))
    # Straddling the end so that most of it lies outside: not covered, even
    # though both endpoints fall within tolerance. Only 10s of this 40s row
    # is inside the window.
    assert not window.covers_interval(at("17:09", 50), at("17:10", 30),
                                      tolerance_seconds=60)


# =====================================================================
# VERIFIED WINDOW EXTENSION
#
# A verdict may cover new evidence without a fresh model call, but ONLY
# when Stage A actually examined that evidence and found continuity.
# Declining to look (below the new-evidence floor) is a different
# conclusion and licenses nothing.
# =====================================================================

def test_examined_continuity_extends_a_verdict_without_a_model_call():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    rows = [
        raw("Code", "pipeline.py - noema", at("10:00"), at("10:20"), key="x1"),
        raw("Code", "store.py - noema", at("10:20"), at("10:45"), key="x2"),
    ]
    for row in rows:
        store.insert_session(row)
    episode = MeaningfulSession(
        start_time=at("10:00"), end_time=at("10:45"), device_set=("laptop",),
        activity_session_ids=tuple(row.id for row in rows),
        active_duration_seconds=2700.0)
    store.insert_meaningful_session(episode)
    store.insert_classification(verdict(
        episode.id, "productive", 0.86, window=("10:00", "10:20")))

    context = {episode.id: [obs(r.app, r.title, r.start.isoformat().replace("+00:00", "Z"),
                                r.end.isoformat().replace("+00:00", "Z"), r.domain, r.id)
                            for r in rows]}
    pending = service.pending_classification_sessions(
        [episode], classifier_version="1", context_by_session=context)
    assert pending == [], "coherent continuation must not spend a model call"

    widened = store.query_classifications(session_id=episode.id)[0]
    assert widened.evidence_end == datetime.fromisoformat(at("10:45").replace("Z", "+00:00"))
    # The verdict itself is untouched; only its recorded scope moved, and
    # provenance says the model was not re-asked.
    assert widened.category == "productive"
    assert widened.confidence == pytest.approx(0.86)
    assert widened.revision == 1
    assert widened.provenance == Provenance.REUSED.value
    assert "without re-asking" in widened.revision_reason
    store.close()


def test_unexamined_evidence_below_the_floor_is_never_covered():
    """The 20-second blip. Too small to examine is not the same as consistent."""
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    work = raw("Code", "pipeline.py - noema", at("17:00"), at("17:10"), key="w2")
    blip = raw("Brave", "Netflix", at("17:10"), at("17:10", 20), "netflix.com", key="b2")
    for row in (work, blip):
        store.insert_session(row)
    episode = MeaningfulSession(
        start_time=at("17:00"), end_time=at("17:10", 20), device_set=("laptop",),
        activity_session_ids=(work.id, blip.id))
    store.insert_meaningful_session(episode)
    store.insert_classification(verdict(
        episode.id, "productive", 0.86, window=("17:00", "17:10")))

    context = {episode.id: [
        obs("Code", "pipeline.py - noema", at("17:00"), at("17:10"), None, work.id),
        obs("Brave", "Netflix", at("17:10"), at("17:10", 20), "netflix.com", blip.id),
    ]}
    assert service.pending_classification_sessions(
        [episode], classifier_version="1", context_by_session=context) == []

    unchanged = store.query_classifications(session_id=episode.id)[0]
    assert unchanged.evidence_end == datetime.fromisoformat(at("17:10").replace("Z", "+00:00")), \
        "a verdict swallowed evidence Stage A never examined"

    rows = {item["application"]: item for item in
            service.recent_activity(at("16:00"), at("18:00"))}
    assert rows["Visual Studio Code"]["category"] == "productive"
    assert rows["Brave"]["category"] is None
    store.close()


def test_contradicting_growth_is_never_extended_over():
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)
    episode = episode_at(at("21:36"), ("r1", "r2"))
    store.insert_meaningful_session(episode)
    store.insert_classification(verdict(episode.id, window=("21:16", "21:20")))
    context = {episode.id: [
        BASE_OBS,
        obs("Brave", "Netflix - Friends S03E12", at("21:20"), at("21:36"), "netflix.com"),
    ]}
    assert service.pending_classification_sessions(
        [episode], classifier_version="1", context_by_session=context), \
        "contradicted growth must be reconsidered, not quietly absorbed"
    still_bounded = store.query_classifications(session_id=episode.id)[0]
    assert still_bounded.evidence_end == datetime.fromisoformat(at("21:20").replace("Z", "+00:00"))
    store.close()
