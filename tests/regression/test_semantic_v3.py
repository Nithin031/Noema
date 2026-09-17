"""Semantic V3 adversarial coverage: verified window extension.

A model verdict describes the evidence the model actually saw. Reusing it
for evidence it never saw is licensed ONLY through positive semantic
continuity established by deterministic local analysis — never through
mere absence of contradiction.

Covered here:

* The extension predicate's conjuncts (sufficient evidence, positive
  continuity with at least two distinct signals, temporal continuity, no
  material contradiction, contract-fresh verdict).
* Adversarial cases A-H: entertainment/ambiguous/unknown intervals must
  not inherit; coherent workflow continuation may.
* The original 0.78 fan-out replayed end to end through daily_summary.
* Reclassification preserves history (append-only revisions).
* Provenance labels on recent_activity and raw-event listings.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from noema.application.classification import Classification
from noema.domain.activity import ActivityEvent
from noema.domain.meaningful import MeaningfulSession
from noema.domain.meaningful.models import EvidenceQuality
from noema.domain.semantic import ExtensionProvenance, assess_extension
from noema.domain.sessions import ActivitySession
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore
from noema.api import NoemaService


DAY = datetime(2026, 9, 4, tzinfo=timezone.utc)


def _evidence(app=None, title=None, domain=None, seconds=30.0, start=None):
    moment = start or DAY.replace(hour=10, minute=0)
    return {
        "app": app, "title": title, "domain": domain, "url": None,
        "seconds": seconds, "start": moment,
        "end": moment + timedelta(seconds=seconds),
        "window_id": None, "tab_id": None,
    }


def _verdict(**overrides):
    base = {
        "category": "productive", "productivity": "productive",
        "topic": "Personal Behavioral Intelligence",
        "activity": "Viewing Noema dashboard",
    }
    base.update(overrides)
    return base


def _episode(**overrides):
    base = {
        "primary_task": "Noema Personal Behavioral Intelligence",
        "primary_topic": None,
        "start": DAY.replace(hour=10, minute=0),
        "end": DAY.replace(hour=10, minute=20),
        "browser_window_id": None, "browser_tab_id": None,
    }
    base.update(overrides)
    return base


def _assess(evidence, verdict=None, episode=None, apps=None, domains=None):
    return assess_extension(
        evidence,
        verdict if verdict is not None else _verdict(),
        episode if episode is not None else _episode(),
        set(apps if apps is not None else {"firefox.exe"}),
        set(domains if domains is not None else set()),
    )


# --- predicate structure ------------------------------------------------


def test_single_signal_never_licenses():
    """Same application alone is explicitly insufficient."""
    result = _assess(_evidence(app="firefox.exe", title="Totally unrelated xyzzy"))
    assert result.examined is True
    assert result.licensed is False
    assert result.positive_hits == ("same_app",)
    assert result.category == "absence_of_contradiction"


def test_missing_identity_is_unexamined():
    """No app/title/domain means the assessor cannot evaluate at all."""
    result = _assess(_evidence(app=None, title=None, domain=None))
    assert result.examined is False
    assert result.licensed is False
    assert result.category == "insufficient_evidence"


def test_zero_duration_is_unexamined():
    result = _assess(_evidence(app="firefox.exe", title="Noema dashboard", seconds=0.0))
    assert result.examined is False
    assert result.licensed is False


def test_old_contract_verdicts_keep_covering_history():
    """Coverage is not gated on contract versions.

    A verdict decided under a retired contract still describes the
    evidence it saw; gating its coverage on versions would rewrite every
    pre-bump timeline as unclassified. Rejected prototype behavior, pinned:
    version keys are simply not part of the licensing predicate.
    """
    verdict = _verdict()
    verdict["prompt_version"] = "retired"
    verdict["classifier_version"] = "0"
    evidence = _evidence(app="firefox.exe", title="Noema Personal Behavioral Intelligence dashboard")
    result = _assess(evidence, verdict=verdict)
    assert result.examined is True
    assert result.licensed is True
    assert result.provenance == ExtensionProvenance.VERIFIED_EXTENSION


def test_slice_outside_episode_span_is_rejected():
    evidence = _evidence(
        app="firefox.exe", title="Noema Personal Behavioral Intelligence dashboard",
        start=DAY.replace(hour=12, minute=0),
    )
    result = _assess(evidence)
    assert result.licensed is False


# --- adversarial cases --------------------------------------------------


def test_case_a_entertainment_interval_does_not_extend():
    """Productive episode -> 10s unrelated entertainment -> productive work."""
    entertainment = _evidence(
        app="firefox.exe", title="Cinepolis India showtimes", seconds=10.0,
        start=DAY.replace(hour=10, minute=5),
    )
    result = _assess(entertainment)
    assert result.licensed is False
    assert result.provenance == ExtensionProvenance.NONE

    coherent = _evidence(
        app="firefox.exe", title="Noema Personal Behavioral Intelligence dashboard",
        start=DAY.replace(hour=10, minute=6),
    )
    licensed = _assess(coherent)
    assert licensed.licensed is True
    assert licensed.provenance == ExtensionProvenance.VERIFIED_EXTENSION
    assert set(licensed.positive_hits) >= {"same_app", "title_overlap"}


def test_case_b_ambiguity_is_not_continuity():
    """Productive episode -> 20s ambiguous activity: no extension."""
    ambiguous = _evidence(
        app="word.exe", title="Document1 draft notes", seconds=20.0,
        start=DAY.replace(hour=10, minute=5),
    )
    result = _assess(ambiguous)
    assert result.licensed is False
    assert result.examined is True


def test_case_c_unknown_vocabulary_requires_examination():
    """Productive episode -> unrelated site with no known vocabulary."""
    unknown = _evidence(
        app="customapp", title="xyz123 portal qwerty", domain="xyz123.example",
        seconds=45.0, start=DAY.replace(hour=10, minute=5),
    )
    result = _assess(unknown)
    assert result.licensed is False
    # Unknown vocabulary is neither positive continuity nor contradiction.
    assert result.category == "absence_of_contradiction"


def test_case_d_coherent_workflow_may_extend():
    """Productive coding episode -> VS Code -> terminal -> GitHub."""
    verdict = _verdict(
        topic="Yolo crater detection", activity="Coding train.py",
    )
    episode = _episode(primary_task="Implement Yolo crater detection")
    apps = {"code.exe", "terminal", "brave.exe"}
    for app, title in [
        ("code.exe", "yolo train.py lens"),
        ("terminal", "yolo crater training bash"),
        ("brave.exe", "yolo-crater pull requests"),
    ]:
        result = _assess(
            _evidence(app=app, title=title, start=DAY.replace(hour=10, minute=5)),
            verdict=verdict, episode=episode, apps=apps,
        )
        assert result.licensed is True, (app, title, result.to_dict())
        assert result.provenance == ExtensionProvenance.VERIFIED_EXTENSION


def test_case_e_streaming_and_shopping_do_not_extend():
    """Productive episode -> YouTube / Netflix / shopping."""
    youtube = _evidence(
        app="firefox.exe", title="lofi hip hop beats to relax to", seconds=60.0,
        start=DAY.replace(hour=10, minute=5),
    )
    assert _assess(youtube).licensed is False

    netflix = _evidence(
        app="netflix", title="Continue watching", seconds=60.0,
        start=DAY.replace(hour=10, minute=6),
    )
    vetoed = _assess(netflix)
    assert vetoed.licensed is False
    assert vetoed.category == "strong_contradiction"

    shopping = _evidence(
        app="firefox.exe", title="cart checkout example-store", seconds=45.0,
        start=DAY.replace(hour=10, minute=7),
    )
    assert _assess(shopping).licensed is False


def test_case_f_neutral_activity_does_not_extend_distractive():
    """Distractive episode -> neutral new-tab page stays unknown."""
    verdict = _verdict(
        category="entertainment", productivity="distracting",
        topic="Evening videos", activity="Watching trailers",
    )
    neutral = _evidence(
        app="firefox.exe", title="Mozilla Firefox", seconds=8.0,
        start=DAY.replace(hour=10, minute=5),
    )
    result = _assess(neutral, verdict=verdict)
    # Browser chrome tokens are stopwords: no fake overlap, single signal.
    assert result.licensed is False


def test_case_g_no_verdict_means_no_extension():
    """An empty verdict mapping (unknown episode) never licenses."""
    evidence = _evidence(app="code.exe", title="train.py model work", seconds=120.0)
    result = assess_extension(
        evidence, {}, _episode(), {"code.exe"}, set(),
    )
    assert result.licensed is False


def test_case_h_exact_boundary_is_examined_not_assumed():
    """New activity at exactly previous end still needs positive signals."""
    coherent = _evidence(
        app="firefox.exe", title="Noema Personal Behavioral Intelligence dashboard",
        start=DAY.replace(hour=10, minute=20),
    )
    assert _assess(coherent).licensed is True

    divergent = _evidence(
        app="brave.exe", title="Claude Code session design", seconds=30.0,
        start=DAY.replace(hour=10, minute=20),
    )
    assert _assess(divergent).licensed is False


def test_veto_beats_matching_positives():
    """Gameplay veto wins even when app/title signals would match."""
    from noema.domain.semantic.continuity import positive_signals

    verdict = _verdict(
        topic="Rocket League bot development", activity="Coding RL bot",
    )
    episode = _episode(primary_task="Rocket League bot development")
    gameplay = _evidence(
        app="rocketleague.exe", title="Rocket League", seconds=30.0,
        start=DAY.replace(hour=10, minute=5),
    )
    apps = {"code.exe", "rocketleague.exe"}
    # Premise check: without the veto, positives alone would license.
    hits = positive_signals(gameplay, verdict, episode, apps, set())
    assert sum(1 for hit in hits.values() if hit) >= 2
    result = _assess(
        gameplay, verdict=verdict, episode=episode, apps=apps,
    )
    assert result.licensed is False
    assert result.category == "strong_contradiction"


# --- original-bug replay through daily_summary --------------------------


def _replay_fixture():
    """The 21:16 pattern: one Noema verdict, one mixed episode, many rows."""
    store = SQLiteStore()
    service = NoemaService(ActivityWatchAdapter(), store)

    specs = [
        ("firefox.exe", "Noema Personal Behavioral Intelligence dashboard",
         "2026-09-04T10:00:00Z", 30, "rk-a"),
        ("firefox.exe", "Noema behavioral models reference",
         "2026-09-04T10:01:00Z", 30, "rk-b"),
        ("brave.exe", "Claude Code session design",
         "2026-09-04T10:02:00Z", 30, "rk-c"),
        ("firefox.exe", "Cinepolis India showtimes",
         "2026-09-04T10:03:00Z", 30, "rk-d"),
        ("firefox.exe", "Avengers endgame trailer encore",
         "2026-09-04T10:04:00Z", 30, "rk-e"),
        ("explorer.exe", "unknown",
         "2026-09-04T10:05:00Z", 30, "rk-f"),
        ("brave.exe", "Noema behavioral dashboard review",
         "2026-09-04T10:06:00Z", 30, "rk-g"),
    ]
    events = [
        ActivityEvent(
            timestamp=start, duration=duration, device="laptop",
            app=app, title=title, bucket_id="window", source_event_id=key,
        )
        for app, title, start, duration, key in specs
    ]
    service.ingest_events(events)
    sessions = {}
    for app, title, start, duration, key in specs:
        event = next(e for e in events if e.source_event_id == key)
        raw = ActivitySession(
            start=start,
            end=(event.timestamp + timedelta(seconds=event.duration)).isoformat().replace("+00:00", "Z"),
            device="laptop", app=app, title=title,
            event_keys=(event.event_key,),
        )
        store.insert_session(raw)
        sessions[key] = raw

    episode = MeaningfulSession(
        start_time="2026-09-04T10:00:00Z",
        end_time="2026-09-04T10:07:00Z",
        device_set=("laptop",),
        activity_session_ids=tuple(s.id for s in sessions.values()),
        primary_project="Noema",
        primary_task="Noema Personal Behavioral Intelligence",
        evidence_quality=EvidenceQuality.MODERATE,
    )
    store.insert_meaningful_session(episode)
    store.insert_classification(Classification(
        episode.id, category="productive", activity_type="administration",
        productivity="productive", confidence=0.78,
        topic="Personal Behavioral Intelligence",
        activity="Viewing Noema dashboard",
        classification_status="classified",
        provider="gemini", model="gemini-3.8-flash", source="gemini"))
    return store, service, episode


def _timeline(service):
    zone = ZoneInfo("Asia/Kolkata")
    start = datetime(2026, 9, 4, tzinfo=zone)
    summary = service.daily_summary(
        start.astimezone(ZoneInfo("UTC")),
        start.replace(day=5).astimezone(ZoneInfo("UTC")),
        timezone_name="Asia/Kolkata",
    )
    return summary


def test_replay_no_row_outside_licensed_window_gets_verdict():
    """The original fan-out, replayed: divergent rows stay unknown."""
    store, service, _ = _replay_fixture()
    try:
        by_activity = {seg["activity"]: seg for seg in _timeline(service)["timeline"]}
        # Coherent member slices keep the verdict with explicit provenance.
        for title in (
            "Noema Personal Behavioral Intelligence dashboard",
            "Noema behavioral models reference",
            "Noema behavioral dashboard review",
        ):
            segment = by_activity[title]
            assert segment["productivity"] == "productive"
            assert segment["confidence"] == 0.78
            assert segment["provenance"] == ExtensionProvenance.VERIFIED_EXTENSION
            assert segment["inherited_from"] is not None
        # Divergent slices must not wear the verdict.
        for title in (
            "Claude Code session design",
            "Cinepolis India showtimes",
            "Avengers endgame trailer encore",
            "unknown",
        ):
            segment = by_activity[title]
            assert segment["productivity"] == "unclassified", title
            assert segment["classification_status"] == "pending", title
            assert segment["inherited_from"] is None, title
            assert segment["provenance"] == ExtensionProvenance.NONE, title
    finally:
        store.close()


def test_replay_dashboard_never_calls_extended_verdict_direct():
    """Every covered row is either DIRECT or VERIFIED_EXTENSION, never both
    confused: inherited rows are never labeled direct."""
    store, service, _ = _replay_fixture()
    try:
        for segment in _timeline(service)["timeline"]:
            if segment.get("inherited_from"):
                assert segment["provenance"] == ExtensionProvenance.VERIFIED_EXTENSION
            if segment.get("provenance") == ExtensionProvenance.DIRECT:
                assert not segment.get("inherited_from")
    finally:
        store.close()


def test_replay_unverified_time_counts_as_unclassified():
    """Aggregation follows licensing, not membership."""
    store, service, _ = _replay_fixture()
    try:
        summary = _timeline(service)
        # 3 licensed slices x 30s = 90s productive; the rest unclassified.
        assert summary["productive_time"] == 90.0
        assert summary["unclassified_time"] == 120.0
    finally:
        store.close()


# --- history preservation ------------------------------------------------


def test_reclassification_preserves_old_verdict():
    """Overwriting a classified row archives the previous verdict."""
    store = SQLiteStore()
    try:
        session = ActivitySession(
            start="2026-09-04T10:00:00Z", end="2026-09-04T10:05:00Z",
            device="laptop", app="Firefox", title="Research",
            event_keys=("hist-1",),
        )
        store.insert_session(session)
        store.insert_classification(Classification(
            session.id, category="research", activity_type="browsing",
            productivity="productive", confidence=0.78,
            classification_status="classified",
            provider="gemini", model="gemini-test", source="gemini"))
        # Contradiction arrives later: the unit is reclassified.
        store.insert_classification(Classification(
            session.id, category="entertainment", activity_type="browsing",
            productivity="distracting", confidence=0.81,
            classification_status="classified",
            provider="gemini", model="gemini-test", source="gemini"))
        current = store.query_classifications(session_id=session.id, limit=1)[0]
        assert current.productivity == "distracting"
        revisions = store.query_classification_revisions(session.id)
        assert len(revisions) == 1
        assert revisions[0]["productivity"] == "productive"
        assert revisions[0]["confidence"] == 0.78
        # Identical rewrites do not spam history.
        store.insert_classification(Classification(
            session.id, category="entertainment", activity_type="browsing",
            productivity="distracting", confidence=0.81,
            classification_status="classified",
            provider="gemini", model="gemini-test", source="gemini"))
        assert len(store.query_classification_revisions(session.id)) == 1
        # First verdicts (pending -> classified) archive nothing.
        fresh = ActivitySession(
            start="2026-09-04T11:00:00Z", end="2026-09-04T11:05:00Z",
            device="laptop", app="Firefox", title="Docs",
            event_keys=("hist-2",),
        )
        store.insert_session(fresh)
        store.insert_classification(Classification(
            fresh.id, category="research", activity_type="browsing",
            productivity="productive", confidence=0.7,
            classification_status="pending",
            provider="gemini", model="gemini-test", source="gemini"))
        store.insert_classification(Classification(
            fresh.id, category="research", activity_type="browsing",
            productivity="productive", confidence=0.7,
            classification_status="classified",
            provider="gemini", model="gemini-test", source="gemini"))
        assert store.query_classification_revisions(fresh.id) == []
    finally:
        store.close()


# --- API provenance labels -----------------------------------------------


def test_recent_activity_labels_direct_and_inherited():
    """Session listings distinguish own verdicts from inherited ones."""
    store = SQLiteStore()
    try:
        service = NoemaService(ActivityWatchAdapter(), store)
        direct = ActivitySession(
            start="2026-09-04T10:00:00Z", end="2026-09-04T10:01:00Z",
            device="laptop", app="Brave", title="Claude Code",
            event_keys=("prov-direct",),
        )
        member = ActivitySession(
            start="2026-09-04T10:02:00Z", end="2026-09-04T10:03:00Z",
            device="laptop", app="Firefox", title="Docs",
            event_keys=("prov-member",),
        )
        store.insert_session(direct)
        store.insert_session(member)
        episode = MeaningfulSession(
            start_time="2026-09-04T10:02:00Z",
            end_time="2026-09-04T10:03:00Z",
            device_set=("laptop",),
            activity_session_ids=(member.id,),
            evidence_quality=EvidenceQuality.MODERATE,
        )
        store.insert_meaningful_session(episode)
        store.insert_classification(Classification(
            direct.id, category="development", activity_type="coding",
            productivity="productive", confidence=0.9,
            classification_status="classified",
            provider="gemini", model="gemini-test", source="gemini"))
        store.insert_classification(Classification(
            episode.id, category="research", activity_type="reading",
            productivity="neutral", confidence=0.6,
            classification_status="classified",
            provider="gemini", model="gemini-test", source="gemini"))
        rows = {
            r["id"]: r for r in service.recent_activity(
                "2026-09-04T09:00:00Z", "2026-09-04T11:00:00Z")
        }
        assert rows[direct.id]["provenance"] == ExtensionProvenance.DIRECT
        assert rows[direct.id]["inherited_from"] is None
        assert rows[member.id]["provenance"] == ExtensionProvenance.INHERITED
        assert rows[member.id]["inherited_from"] == episode.id
    finally:
        store.close()


def test_raw_events_reuse_session_verdicts():
    """Raw-event listings mark re-displayed session verdicts REUSED."""
    store = SQLiteStore()
    try:
        service = NoemaService(ActivityWatchAdapter(), store)
        event = ActivityEvent(
            timestamp="2026-09-04T10:00:00Z", duration=30, device="laptop",
            app="Firefox", title="Docs", bucket_id="window",
            source_event_id="reuse-1",
        )
        service.ingest_events([event])
        session = ActivitySession(
            start="2026-09-04T10:00:00Z", end="2026-09-04T10:00:30Z",
            device="laptop", app="Firefox", title="Docs",
            event_keys=(event.event_key,),
        )
        store.insert_session(session)
        enriched = service.events_with_classifications(store.query(limit=100))
        pending = [row for row in enriched if row["provenance"] == ExtensionProvenance.NONE]
        assert pending, "expected rows without verdicts"
        store.insert_classification(Classification(
            session.id, category="research", activity_type="reading",
            productivity="neutral", confidence=0.6,
            classification_status="classified",
            provider="gemini", model="gemini-test", source="gemini"))
        enriched = service.events_with_classifications(store.query(limit=100))
        reused = [row for row in enriched if row["provenance"] == ExtensionProvenance.REUSED]
        assert reused, "expected rows reusing the session verdict"
        assert all(row["classification_status"] == "classified" for row in reused)
    finally:
        store.close()
