from datetime import datetime, timezone

from noema.domain.activity import ActivityEvent
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification
from noema.domain.intent import GoalAligner, Intent, IntentEngine
from noema.domain.sessions import Sessionizer


class IntentModel:
    model = "intent-test"

    def generate_json(self, prompt):
        return {
            "goal": "Implement PPO reward shaping",
            "topic": "reinforcement learning",
            "project": "Unitree A1",
            "keywords": ["ppo", "reward", "shaping"],
            "success_criteria": "reward.py has passing tests",
            "confidence": 0.91,
        }


def make_session():
    event = ActivityEvent(
        timestamp="2026-09-04T10:00:00Z",
        duration=300,
        device="laptop",
        app="VS Code",
        title="reward.py",
        bucket_id="window_laptop",
        source_event_id="1",
    )
    return Sessionizer().sessionize([event])[0]


def test_intent_engine_parses_structured_ollama_output():
    intent = IntentEngine(client=IntentModel(), fallback=False).capture(
        "Implement PPO reward shaping for Unitree A1",
        created_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc),
    )

    assert intent.provider == "ollama"
    assert intent.project == "Unitree A1"
    assert intent.keywords == ("ppo", "reward", "shaping")
    assert intent.id


def test_goal_alignment_is_explainable_and_persistable():
    current = make_session()
    intent = Intent(
        text="Implement PPO reward shaping for Unitree A1",
        goal="Implement PPO reward shaping",
        topic="reinforcement learning",
        project="Unitree A1",
        keywords=("ppo", "reward", "shaping"),
        created_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc),
    )
    classification = Classification(
        session_id=current.id,
        category="development",
        topic="reinforcement_learning",
        project="Unitree A1",
        activity_type="coding",
        productivity="productive",
        confidence=0.9,
        provider="ollama",
        model="test",
    )
    alignment = GoalAligner().align(current, intent, classification)

    assert alignment.aligned is True
    assert alignment.score >= 0.35
    assert "activity type matches goal verb" in alignment.reason

    store = SQLiteStore()
    assert store.insert_alignment(alignment) is True
    assert store.insert_alignment(alignment) is False
    assert store.query_alignments(intent_id=intent.id)[0].aligned is True
    store.close()
