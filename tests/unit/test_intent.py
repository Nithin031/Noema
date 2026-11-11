from datetime import datetime, timezone

from noema.domain.activity import ActivityEvent
from noema.infrastructure.database import SQLiteStore
from noema.classification.core import Classification
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


def test_strict_alignment_flags_unrelated_work_but_default_does_not():
    current = make_session()
    intent = Intent(text="study for SM 300 end sem exam", goal="study for SM 300 end sem exam",
                    keywords=("study", "sm", "300", "exam"),
                    created_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc))
    classification = Classification(
        session_id=current.id, category="development", topic="noema", project=None,
        activity_type="coding", productivity="productive", confidence=0.8, classification_status="classified",
        provider="test", model="test")
    default = GoalAligner().align(current, intent, classification)
    strict = GoalAligner(strict_unrelated=True).align(current, intent, classification)
    assert default.relation == "unknown"              # documented default: uncertainty never drifts
    assert strict.relation == "misaligned"
    assert "strict alignment" in strict.reason
    # strict mode never flags work that DOES relate to the goal
    related = Intent(text="fix reward.py", goal="fix reward.py", keywords=("reward",),
                     created_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc))
    assert GoalAligner(strict_unrelated=True).align(current, related, classification).relation != "misaligned"


def test_allow_rules_let_a_side_project_tool_count_as_on_task():
    current = make_session()   # VS Code / reward.py
    exam = Intent(text="study for SM 300 end sem exam", goal="study for SM 300 end sem exam",
                  keywords=("study", "sm", "300", "exam"),
                  created_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc))
    classification = Classification(
        session_id=current.id, category="development", topic="noema", project=None,
        activity_type="coding", productivity="productive", confidence=0.8,
        classification_status="classified", provider="test", model="test")
    strict = GoalAligner(strict_unrelated=True).align(current, exam, classification)
    assert strict.relation == "misaligned"                       # building the app is off the exam goal
    # Study time is isolated: an exam intent keeps the build rules OFF, so building is off-task.
    studying = GoalAligner(strict_unrelated=True, allow_rules=("vs code",)).align(current, exam, classification)
    assert studying.relation == "misaligned"
    # With a build intent the same rule makes the tool on-task.
    build = Intent(text="build the noema app", goal="build the noema app", keywords=("build", "noema", "app"),
                   created_at=datetime(2026, 9, 4, 10, tzinfo=timezone.utc))
    allowed = GoalAligner(strict_unrelated=True, allow_rules=("vs code",)).align(current, build, classification)
    assert allowed.relation == "aligned" and "allowed by your rule: vs code" in allowed.reason
    # rules are case-insensitive and ignore blanks
    assert GoalAligner(allow_rules=(" VS CODE ", "")).align(current, build, classification).aligned is True
