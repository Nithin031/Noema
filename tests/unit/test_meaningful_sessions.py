from datetime import datetime, timezone

from noema.domain.activity import ActivityEvent
from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
from noema.api import NoemaService
from noema.infrastructure.database import SQLiteStore
from noema.domain.behavior import BehaviorState, BehaviorEngine
from noema.application.classification import Classification, Classifier
from noema.domain.intent import AlignmentResult, GoalAligner, Intent
from noema.domain.meaningful import MeaningfulSession, MeaningfulSessionEngine, MeaningfulSessionStatus, MeaningfulSessionSummarizer
from noema.infrastructure.ollama import OllamaError
from noema.domain.sessions import Sessionizer


def raw_session(timestamp, app, domain, title, event_id):
    return Sessionizer(max_gap_seconds=0).sessionize([
        ActivityEvent(
            timestamp=timestamp, duration=30, device="laptop", app=app, domain=domain,
            title=title, bucket_id="mixed", source_event_id=event_id,
        )
    ])[0]


def test_meaningful_engine_merges_semantically_continuous_task_and_preserves_phases():
    sessions = [
        raw_session("2026-09-04T10:00:00Z", "VS Code", None, "reward.py", "1"),
        raw_session("2026-09-04T10:01:00Z", "Chrome", "arxiv.org", "PPO Algorithms", "2"),
        raw_session("2026-09-04T10:02:00Z", "Chrome", "stackoverflow.com", "PPO reward clipping", "3"),
        raw_session("2026-09-04T10:03:00Z", "VS Code", None, "reward.py", "4"),
        raw_session("2026-09-04T10:04:00Z", "Chrome", "cooking.example", "Pasta recipe", "5"),
    ]
    intent = Intent(
        text="Implement PPO reward shaping for Unitree A1",
        goal="Implement PPO reward shaping",
        topic="reinforcement learning", project="Unitree A1",
        keywords=("ppo", "reward", "shaping"),
        created_at=datetime(2026, 9, 4, 9, tzinfo=timezone.utc),
    )
    classifications = {
        sessions[0].id: Classification(sessions[0].id, "development", "reinforcement_learning", "Unitree A1", "coding", "productive", .9),
        sessions[1].id: Classification(sessions[1].id, "research", "reinforcement_learning", "Unitree A1", "paper_reading", "productive", .9),
        sessions[2].id: Classification(sessions[2].id, "research", "reinforcement_learning", "Unitree A1", "research", "productive", .9),
        sessions[3].id: Classification(sessions[3].id, "development", "reinforcement_learning", "Unitree A1", "coding", "productive", .9),
        sessions[4].id: Classification(sessions[4].id, "entertainment", "cooking", None, "browsing", "distracting", .9),
    }
    alignments = {
        session.id: AlignmentResult(session.id, intent.id, session is not sessions[4], .9 if session is not sessions[4] else .0, .9, "intent evidence")
        for session in sessions
    }

    meaningful = MeaningfulSessionEngine().build(sessions, classifications, alignments, intent)

    assert len(meaningful) == 2
    task = meaningful[0]
    assert task.primary_project == "Unitree A1"
    assert task.primary_task == "Implement PPO reward shaping"
    assert task.activity_session_ids == tuple(session.id for session in sessions[:4])
    assert task.activities == ("implementation", "research")
    assert task.context_switch_count == 3
    assert task.status == MeaningfulSessionStatus.CLOSED
    assert "explicit intent" in task.evidence
    assert task.confidence > 0.7


def test_meaningful_session_persistence_and_observable_summary():
    raw = raw_session("2026-09-04T10:00:00Z", "VS Code", None, "reward.py", "1")
    meaningful = MeaningfulSessionEngine().build([raw])[0]
    # Keep this persistence test independent of whether a local Ollama server
    # happens to be running on the developer machine.
    summarized = MeaningfulSessionSummarizer(client=OfflineOllama()).summarize(meaningful, [raw])
    assert summarized.status == MeaningfulSessionStatus.SUMMARIZED
    assert summarized.summary["result"] == "Observable activity included: reward.py"

    store = SQLiteStore()
    assert store.insert_meaningful_session(summarized) is True
    assert store.insert_meaningful_session(summarized) is False
    found = store.query_meaningful_sessions()
    assert found[0].id == summarized.id
    assert found[0].summary["title"] == "reward.py"
    assert found[0].phases[0].phase_type == "implementation"
    store.close()


class OfflineOllama:
    model = "offline"

    def generate_json(self, prompt):
        raise OllamaError("offline")


def test_gen_15_meaningful_session_is_canonical_downstream_unit():
    canonical = MeaningfulSession(
        start_time="2026-09-04T10:00:00Z",
        end_time="2026-09-04T10:10:00Z",
        device_set=("laptop",),
        activity_session_ids=("raw-1", "raw-2"),
        primary_project="Unitree A1",
        primary_task="Implement PPO reward shaping",
        primary_topic="reinforcement learning",
        activities=("implementation", "research"),
        dominant_category="development",
        dominant_activity_type="coding",
        alignment_score=0.9,
        focus_score=0.9,
        confidence=0.9,
    )
    classifier = Classifier(client=OfflineOllama())
    classification = classifier.classify(canonical)
    assert classification.session_id == canonical.id
    assert classification.productivity == "neutral"
    assert classification.source == "pending"
    assert '"session_type": "meaningful"' in classifier.build_prompt(canonical)

    intent = Intent(
        text="Implement PPO reward shaping for Unitree A1",
        goal="Implement PPO reward shaping",
        topic="reinforcement learning",
        project="Unitree A1",
        keywords=("ppo", "reward", "shaping"),
    )
    alignment = GoalAligner().align(canonical, intent, classification)
    assert alignment.aligned is True

    observations = BehaviorEngine().evaluate(
        [canonical], {canonical.id: classification}, {canonical.id: alignment}
    )
    assert observations[0].session_id == canonical.id
    assert observations[0].state == BehaviorState.NORMAL

    store = SQLiteStore()
    store.insert_meaningful_session(canonical)
    store.insert_classification(classification)
    store.insert_alignment(alignment)
    service = NoemaService(ActivityWatchAdapter(), store)
    stored_observations = service.evaluate_stored_behavior()
    assert [item.session_id for item in stored_observations] == [canonical.id]
    store.close()
