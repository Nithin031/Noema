from noema.domain.behavior import BehaviorObservation, BehaviorState
from noema.application.classification import Classification
from noema.domain.intent import Intent
from noema.domain.meme import MemeIntelligence, MemePayload, MemeRenderer
from noema.domain.sessions import ActivitySession


class MemeModel:
    model = "meme-test"

    def generate_json(self, prompt):
        return {"template": "drake", "severity": 3, "top": "Ship reward.py", "bottom": "Become a Reddit historian instead"}


def test_meme_intelligence_and_svg_renderer():
    session = ActivitySession(
        start="2026-09-04T10:00:00Z", end="2026-09-04T10:15:00Z", device="laptop",
        app="Chrome", domain="reddit.com", event_keys=("meme",),
    )
    observation = BehaviorObservation(
        session_id=session.id, state=BehaviorState.DISTRACTED, started_at=session.start,
        confidence=.9, distraction_score=.9, actionable=True, reason="off goal",
    )
    intent = Intent(text="Implement PPO reward shaping", goal="Ship reward.py")
    classification = Classification(session.id, "entertainment", topic="reddit", productivity="distracting")
    meme = MemeIntelligence(client=MemeModel(), fallback=False).create(session, observation, intent, classification)

    assert isinstance(meme, MemePayload)
    assert meme.provider == "ollama"
    svg = MemeRenderer().render_svg(meme)
    assert "Ship reward.py" in svg
    assert "&lt;goal&gt;" in MemeRenderer().render_svg(MemePayload(session.id, top="<goal>"))
