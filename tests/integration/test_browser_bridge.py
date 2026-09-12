from noema.api import NoemaService
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.infrastructure.browser import BrowserBridge
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification, Classifier
from noema.domain.intervention import InterventionEngine, InterventionMode, InterventionPolicy
from noema.domain.meaningful import MeaningfulSession


class CategorizingOllama:
    model = "test-local"

    def generate_json(self, prompt):
        return {
            "category": "productive",
            "topic": "python",
            "project": "activity os",
            "activity_type": "reading",
            "productivity": "productive",
            "activity": "Reading Python documentation",
            "signal": "The domain and title identify technical documentation.",
            "confidence": 0.9,
        }


def action(tab_id="42", window_id="17"):
    return {
        "intervention_id": "int-1",
        "type": "MEME",
        "severity": 2,
        "target": {
            "device": "laptop-01",
            "browser": "firefox",
            "window_id": window_id,
            "tab_id": tab_id,
        },
        "message": {"title": "BRO 💀", "body": "Lock in."},
        "meme": {"template": "drake", "top": "Work", "bottom": "Scroll"},
        "actions": ["LOCK_IN", "DISMISS_WORKING"],
        "expires_in": 5000,
    }


def test_browser_bridge_delivers_only_to_exact_active_tab():
    bridge = BrowserBridge()
    bridge.register("firefox", "laptop-01", "extension-1")
    bridge.report_event("extension-1", {
        "event_type": "tab_activated", "window_id": "17", "tab_id": "42",
    })

    delivered = bridge.publish(action())
    assert delivered.status == "DELIVERED"
    messages = bridge.poll("extension-1")
    assert messages[0]["payload"]["target"]["tab_id"] == "42"

    bridge.report_event("extension-1", {
        "event_type": "tab_activated", "window_id": "17", "tab_id": "43",
    })
    queued = bridge.publish(action())
    assert queued.status == "QUEUED"
    assert bridge.poll("extension-1") == []

    bridge.report_event("extension-1", {
        "event_type": "tab_activated", "window_id": "17", "tab_id": "42",
    })
    assert bridge.poll("extension-1")[0]["payload"]["intervention_id"] == "int-1"


def test_holdout_and_missing_tab_are_never_browser_delivered():
    bridge = BrowserBridge()
    bridge.register("firefox", "laptop-01", "extension-1")
    bridge.report_event("extension-1", {"event_type": "tab_activated", "window_id": "17", "tab_id": "42"})
    assert bridge.publish(dict(action(), type="HOLDOUT")).status == "SUPPRESSED"
    assert bridge.publish(dict(action(), target={"browser": "firefox"})).status == "NO_EXACT_TARGET"


def test_service_publishes_executed_intervention_and_persists_user_action():
    session = MeaningfulSession(
        start_time="2026-09-04T10:00:00Z", end_time="2026-09-04T10:10:00Z",
        device_set=("laptop-01",), activity_session_ids=("raw-1",),
        primary_task="Implement PPO reward shaping", primary_topic="reddit",
        dominant_category="entertainment", browser="firefox",
        browser_window_id="17", browser_tab_id="42",
    )
    classification = Classification(
        session.id, "entertainment", topic="reddit", productivity="distracting", confidence=.9
    )
    observation = BehaviorObservation(
        session.id, BehaviorState.DISTRACTED, session.start, .9, .9, True, "distracted"
    )
    store = SQLiteStore()
    store.insert_meaningful_session(session)
    store.insert_classification(classification)
    store.insert_behavior_observation(observation)
    service = NoemaService(
        ActivityWatchAdapter(), store,
        intervention_engine=InterventionEngine(
            InterventionPolicy(mode=InterventionMode.MEME, dry_run=True)
        ),
    )
    service.register_browser("firefox", "laptop-01", "extension-1")
    service.report_browser_event("extension-1", {
        "event_type": "tab_activated", "window_id": "17", "tab_id": "42",
    })

    intervention = service.consider_intervention(session.id, execute=True)

    assert intervention.action.target["tab_id"] == "42"
    assert service.poll_browser("extension-1")[0]["payload"]["intervention_id"] == intervention.id
    assert any(
        item["action"] == "triggered"
        for item in store.query_intervention_actions(intervention.id)
    )
    service.record_intervention_action(intervention.id, "CLICKED", state="INTERACTED")
    service.record_intervention_action(intervention.id, "DISMISS_WORKING", state="INTERACTED")
    actions = store.query_intervention_actions(intervention.id)
    assert any(item["action"] == "clicked" for item in actions)
    assert any(item["action"] == "dismissed_as_working" for item in actions)
    store.close()


def test_browser_tab_is_persisted_and_can_be_categorized_by_exact_identity():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    timestamp = now.isoformat().replace("+00:00", "Z")
    store = SQLiteStore()
    service = NoemaService(
        ActivityWatchAdapter(), store, classifier=Classifier(client=CategorizingOllama())
    )
    service.register_browser("firefox", "laptop-01", "extension-1")
    service.report_browser_event("extension-1", {
        "event_type": "tab_activated", "window_id": "17", "tab_id": "42",
        "url": "https://docs.python.org/3/", "title": "Python Documentation",
        "timestamp": timestamp,
    })

    result = service.categorize_browser_tab(
        "laptop-01", "firefox", "17", "42", classify=True
    )
    assert result["status"] == "categorized"
    assert result["category"] == "productive"
    assert result["activity_type"] == "reading"
    assert result["tab_id"] == "42"
    store.close()
