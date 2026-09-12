import json
import os

from noema.domain.activity import ActivityEvent
from noema.application.classification import Classifier
from noema.infrastructure.providers import GeminiProvider
from noema.runtime import DaemonConfig
from noema.domain.sessions import Sessionizer


def session():
    event = ActivityEvent(
        timestamp="2026-09-04T10:00:00Z",
        duration=120,
        device="laptop",
        app="VS Code",
        title="reward.py",
        bucket_id="window_laptop",
        source_event_id="1",
    )
    return Sessionizer().sessionize([event])[0]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.payload


def test_gemini_provider_uses_env_key_and_returns_structured_payload():
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return FakeResponse(json.dumps({
            "candidates": [{
                "content": {
                    "parts": [{"text": json.dumps({
                        "category": "development",
                        "topic": "reinforcement_learning",
                        "activity_type": "coding",
                        "productivity": "productive",
                        "confidence": 0.91,
                    })}]
                }
            }]
        }).encode("utf-8"))

    previous = os.environ.get("NOEMA_TEST_GEMINI_KEY")
    os.environ["NOEMA_TEST_GEMINI_KEY"] = "test-only-secret"
    try:
        provider = GeminiProvider(
            model="gemini-test",
            api_key_env="NOEMA_TEST_GEMINI_KEY",
            urlopen_fn=fake_urlopen,
        )
        payload = provider.classify(session(), "classify this")
    finally:
        if previous is None:
            os.environ.pop("NOEMA_TEST_GEMINI_KEY", None)
        else:
            os.environ["NOEMA_TEST_GEMINI_KEY"] = previous

    request, timeout = calls[0]
    assert payload["category"] == "development"
    assert request.get_header("X-goog-api-key") == "test-only-secret"
    assert request.full_url.endswith("/models/gemini-test:generateContent")
    assert timeout == 15.0
    assert "test-only-secret" not in request.full_url


def test_classifier_accepts_any_named_provider_without_provider_specific_logic():
    class FakeProvider:
        name = "gemini"
        model = "fake-gemini"

        def classify(self, current, prompt):
            return {
                "category": "productive",
                "topic": "papers",
                "activity_type": "reading",
                "productivity": "productive",
                "activity": "Reading research papers",
                "signal": "The activity evidence points to technical research.",
                "confidence": 0.8,
            }

    result = Classifier(provider=FakeProvider()).classify(session())

    assert result.provider == "gemini"
    assert result.model == "fake-gemini"
    assert result.category == "productive"
    assert result.source == "gemini"
    assert result.classification_status == "classified"


def test_gemini_secret_is_not_part_of_public_daemon_configuration():
    config = DaemonConfig(provider="gemini", gemini_api_key_env="NOEMA_TEST_GEMINI_KEY")

    public_config = json.dumps(config.to_dict())

    assert "test-only-secret" not in public_config
    assert "NOEMA_TEST_GEMINI_KEY" in public_config
