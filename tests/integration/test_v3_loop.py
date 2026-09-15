"""V3 full loop: detector → reasoning → policy → response → action → outcome.

Drives production stages with scripted providers only: sustained
distraction enters a candidate, the stubbed Gemini reasoning confirms
DISTRACTED + MEME, policy executes, a curated response binds, the user
locks in, and recovery measures DIRECT with response linkage.
"""

from datetime import datetime, timedelta, timezone

from noema.api import NoemaService
from noema.application.classification import Classifier
from noema.application.realtime import FastModelVerifier
from noema.domain.intervention import InterventionEngine, InterventionMode, InterventionPolicy
from noema.domain.sessions import ActivitySession
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


def test_v3_full_loop_reasoning_to_direct_recovery():
    store, service = make_service(distracting_payload())
    try:
        sessions = make_sessions("d", 25)
        for session in sessions:
            store.insert_meaningful_session(session)
        classify_all(service, store, sessions)
        service.fast_verifier = FastModelVerifier(fast_openrouter=ConfirmLeg())
        service.intervention_engine = InterventionEngine(
            InterventionPolicy(mode=InterventionMode.MEME))
        from noema.domain.intent import Intent
        store.insert_intent(Intent(
            text="Finish Power Electronics assignment",
            goal="Finish Power Electronics assignment",
            topic="power electronics"))

        result = service.evaluate_realtime(now=NOW, execute_intervention=True)

        assert result["state"] == "INTERVENTION_COOLDOWN"
        reasoning = result["reasoning"]
        assert reasoning["state"] == "DISTRACTED"
        assert reasoning["intervention_worthwhile"] is True
        assert reasoning["recommended_response_class"] == "MEME"
        assert reasoning["reasoning_version"] == "1"
        intervention = result["intervention"]
        assert intervention["status"] == "EXECUTED"
        stored = store.get_intervention(intervention["id"])
        assert stored.payload.get("response_id")
        assert stored.payload.get("response_kind") == "MEME"
        assert stored.payload.get("context_line")
        detections = store.query_detections(limit=100)
        assert any(row.response_class == "MEME" for row in detections)
        assert any((row.reasoning_version or "") == "1" for row in detections)

        # User locks in through the overlay contract.
        service.record_intervention_action(
            intervention["id"], "DISPLAYED", state="DISPLAYED")
        service.record_intervention_action(
            intervention["id"], "LOCK_IN", state="INTERACTED")

        # Productive return afterwards.
        recovery = ActivitySession(
            start="2026-09-09T12:05:00Z", end="2026-09-09T12:10:00Z",
            device="laptop", app="Code", title="work.py",
            event_keys=("v3-rec",))
        store.insert_session(recovery)
        from noema.application.classification import Classification
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
        assert outcome.response_id == stored.payload.get("response_id")
        assert store.get_response(outcome.response_id).recovery_count == 1

        # Feed surfaces the whole story for UI.
        feed = service.intervention_feed(limit=10)
        assert len(feed) == 1
        assert feed[0]["interacted"] is True
        assert feed[0]["outcome"]["attribution"] == "direct"
    finally:
        store.close()


def test_v3_uncertain_reasoning_never_confirms():
    class UncertainLeg(ConfirmLeg):
        def complete_json(self, prompt, max_tokens=300):
            self.calls += 1
            return {
                "state": "UNCERTAIN",
                "confidence": 0.4,
                "severity": 2,
                "evidence_quality": "weak",
                "reason": "thin conflicting evidence",
                "intervention_worthwhile": False,
                "recommended_response_class": "NONE",
                "evidence_gaps": ["goal text"],
            }

    store, service = make_service(distracting_payload())
    try:
        sessions = make_sessions("d", 25)
        for session in sessions:
            store.insert_meaningful_session(session)
        classify_all(service, store, sessions)
        service.fast_verifier = FastModelVerifier(fast_openrouter=UncertainLeg())
        result = service.evaluate_realtime(now=NOW, execute_intervention=True)
        assert result["state"] == "CANDIDATE"
        assert result["reasoning"]["state"] == "UNCERTAIN"
        assert result["intervention"] is None
        assert store.query_interventions(limit=100000) == []
    finally:
        store.close()


def test_v3_migration_adds_columns_and_keeps_history():
    import sqlite3

    store = SQLiteStore()
    try:
        columns = {row["name"] for row in store._connection.execute(
            "PRAGMA table_info(distraction_detections)").fetchall()}
        assert {"reasoning_json", "reasoning_version", "response_class"} <= columns
        out_columns = {row["name"] for row in store._connection.execute(
            "PRAGMA table_info(intervention_outcomes)").fetchall()}
        assert {"detection_id", "response_id", "delivery_state", "user_action",
                "attribution", "break_context"} <= out_columns
        tables = {row["name"] for row in store._connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"responses", "intervention_feedback"} <= tables
        # Legacy-style insert without new columns still reads back cleanly.
        store._connection.execute(
            """INSERT INTO intervention_outcomes
               (id, intervention_id, intervention_time, recovery_status)
               VALUES ('legacy-1', 'int-legacy', '2026-01-01T00:00:00Z', 'RECOVERED')""")
        legacy = store.query_outcomes(intervention_id="int-legacy", limit=1)[0]
        assert legacy.attribution is None
        assert legacy.response_id is None
        assert legacy.break_context is False
    finally:
        store.close()
