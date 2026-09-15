"""V4 intervention loop: reason → decide → rank → copy → policy → outcome.

Drives production stages with dispatching fake legs (one fake serves
reasoning, intervention, ranking, and copy prompts by content): sustained
distraction enters a candidate, reasoning confirms, intervention reasoning
approves with a MEME strategy, a ranked asset binds, curated copy renders,
policy executes, the user locks in, and recovery measures DIRECT.
"""

from datetime import datetime, timedelta, timezone

from noema.api import NoemaService
from noema.application.classification import Classifier
from noema.application.meme_decision import MemeDecider
from noema.application.realtime import FastModelVerifier
from noema.application.realtime.intervention import InterventionReasoner
from noema.domain.intent import Intent
from noema.domain.intervention import (
    InterventionEngine,
    InterventionMode,
    InterventionPolicy,
)
from noema.domain.meme import MemeAsset, asset_id_for
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.infrastructure.database import SQLiteStore

from test_realtime_pipeline import (
    ConfirmLeg,
    CountingClient,
    classify_all,
    distracting_payload,
    make_service,
    make_sessions,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class DispatchLeg:
    """One fake serving every V4 prompt kind by content marker."""

    name = "gemini"
    model = "gemini-3.5-flash"

    INTERVENTION = {
        "should_intervene": True,
        "intervention_type": "MEME_NUDGE",
        "severity": 3,
        "tone": "PLAYFUL",
        "reason": "Sustained off-goal activity with no spontaneous recovery",
        "response_class": "MEME",
        "use_meme": True,
        "meme_intent": "procrastination callout",
        "confidence": 0.86,
    }
    RANK = {
        "selected_asset_id": "ASSET_B",
        "confidence": 0.89,
        "reason": "strong contextual match for playful procrastination callout",
        "alternatives": ["ASSET_A"],
    }
    COPY = {"copy": "You said you were locking in."}

    def __init__(self):
        self.calls = 0
        self.purposes = []

    def complete_json(self, prompt, max_tokens=300):
        self.calls += 1
        if "should_intervene" in prompt:
            self.purposes.append("intervene")
            return dict(self.INTERVENTION)
        if "selected_asset_id" in prompt:
            self.purposes.append("rank")
            return dict(self.RANK)
        if '"copy"' in prompt and "short intervention line" in prompt:
            self.purposes.append("copy")
            return dict(self.COPY)
        self.purposes.append("reason")
        raise AssertionError("unexpected prompt shape in V4 loop test")


class DeclineLeg(DispatchLeg):
    INTERVENTION = {
        "should_intervene": False,
        "intervention_type": "NONE",
        "severity": 2,
        "tone": "NEUTRAL",
        "reason": "Likely intentional; recent intervention already sent",
        "response_class": "NONE",
        "use_meme": False,
        "meme_intent": None,
        "confidence": 0.7,
    }


def _seed_assets(store):
    for filename, ocr in (("dog.jpg", "stop procrastinating now"),
                          ("sun.jpg", "beautiful evening sky")):
        store.upsert_meme_asset(MemeAsset(
            id=asset_id_for("test-corpus", filename),
            source="test-corpus", source_ref=filename, filename=filename,
            ocr_text=ocr, sentiment="neutral",
            created_at=datetime.now(timezone.utc)))


def _wire(service, leg):
    service.intervention_reasoner = InterventionReasoner(legs=[leg])
    service.meme_decider = MemeDecider(legs=[leg])
    return service


def test_full_loop_with_gemini_strategy_rank_copy_and_outcome():
    store, service = make_service(distracting_payload())
    try:
        sessions = make_sessions("d", 25)
        for session in sessions:
            store.insert_meaningful_session(session)
        classify_all(service, store, sessions)
        store.insert_intent(Intent(
            text="Finish Power Electronics assignment",
            goal="Finish Power Electronics assignment",
            topic="power electronics"))
        _seed_assets(store)
        leg = DispatchLeg()
        # Rank must name a real retrieved id; resolve the deterministic
        # top candidate first so the fake stays honest.
        from noema.application.meme_decision import retrieve_meme_candidates

        top = retrieve_meme_candidates(
            store, meme_intent="procrastination callout", tone="playful")[0]["id"]
        leg.RANK = dict(DispatchLeg.RANK, selected_asset_id=top,
                        alternatives=[])
        service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())
        service.intervention_engine = InterventionEngine(
            InterventionPolicy(mode=InterventionMode.MEME))
        _wire(service, leg)

        result = service.evaluate_realtime(now=NOW, execute_intervention=True)

        assert result["state"] == "INTERVENTION_COOLDOWN"
        assert result["reasoning"]["state"] == "DISTRACTED"
        intervention = result["intervention"]
        assert intervention["status"] == "EXECUTED"
        stored = store.get_intervention(intervention["id"])
        payload = dict(stored.payload)
        # Decision drove strategy, tone mapping, meme ranking, and copy.
        assert payload["response_kind"] == "MEME"
        decision = payload["intervention_decision"]
        assert decision["should_intervene"] is True
        assert decision["intervention_type"] == "MEME_NUDGE"
        assert decision["tone"] == "PLAYFUL"
        assert payload["image_url"] == "/api/meme-assets/{}/image".format(top)
        assert payload["meme_asset"]["id"] == top
        assert payload["message"]["body"] == "You said you were locking in."
        assert set(leg.purposes) == {"intervene", "rank", "copy"}
        # Detection row carries the decision for future reasoning.
        decisions = [item["decision"] for item in result["detections"]]
        assert "verified_concerning" in decisions
        assert "intervention_triggered" in decisions
        assert any(item.get("intervention_decision", {}).get("should_intervene") is True
                   for item in result["detections"]
                   if isinstance(item, dict))

        service.record_intervention_action(
            intervention["id"], "DISPLAYED", state="DISPLAYED")
        service.record_intervention_action(
            intervention["id"], "LOCK_IN", state="INTERACTED")
        from noema.application.classification import Classification
        from noema.domain.sessions import ActivitySession

        recovery = ActivitySession(
            start="2026-09-09T12:05:00Z", end="2026-09-09T12:10:00Z",
            device="laptop", app="Code", title="work.py",
            event_keys=("v4-rec",))
        store.insert_session(recovery)
        store.insert_classification(Classification(
            session_id=recovery.id, category="productive",
            productivity="productive", confidence=0.9,
            classification_status="classified", provider="test",
            model="test", source="test", evidence_quality="strong"))
        outcome = service.measure_outcome(
            intervention["id"], now=NOW + timedelta(minutes=30))
        assert outcome.recovery_status.value == "RECOVERED"
        assert outcome.attribution == "direct"
        assert outcome.user_action == "lock_in"
        assert outcome.response_id == payload.get("response_id")
    finally:
        store.close()


def test_decline_leaves_candidate_to_decay_without_intervention():
    store, service = make_service(distracting_payload())
    try:
        sessions = make_sessions("d", 25)
        for session in sessions:
            store.insert_meaningful_session(session)
        classify_all(service, store, sessions)
        service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())
        _wire(service, DeclineLeg())
        result = service.evaluate_realtime(now=NOW, execute_intervention=True)
        # Declined: still a candidate, nothing planned or executed.
        assert result["state"] == "CANDIDATE"
        assert result["intervention"] is None
        assert store.query_interventions(limit=100000) == []
        decisions = [item["decision"] for item in result["detections"]]
        assert "reasoning_declined" in decisions
        assert "intervention_triggered" not in decisions
    finally:
        store.close()


def test_reasoning_failure_falls_back_to_no_intervention():
    from noema.infrastructure.providers import ProviderError

    class DownLeg(DispatchLeg):
        def complete_json(self, prompt, max_tokens=300):
            self.calls += 1
            raise ProviderError("connection refused")

    store, service = make_service(distracting_payload())
    try:
        sessions = make_sessions("d", 25)
        for session in sessions:
            store.insert_meaningful_session(session)
        classify_all(service, store, sessions)
        service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())
        _wire(service, DownLeg())
        result = service.evaluate_realtime(now=NOW, execute_intervention=True)
        assert result["state"] == "CANDIDATE"
        assert result["intervention"] is None
        assert store.query_interventions(limit=100000) == []
    finally:
        store.close()


def test_break_mode_still_vetoes_before_any_model_call():
    store, service = make_service(distracting_payload())
    try:
        sessions = make_sessions("d", 25)
        for session in sessions:
            store.insert_meaningful_session(session)
        classify_all(service, store, sessions)
        leg = DispatchLeg()
        service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())
        _wire(service, leg)
        service.start_break(minutes=5, now=NOW)
        result = service.evaluate_realtime(now=NOW, execute_intervention=True)
        assert result["action"] == "none (break active)"
        assert leg.calls == 0
        assert store.query_interventions(limit=100000) == []
    finally:
        store.close()
