from noema.domain.activity import ActivityEvent
from noema.infrastructure.database import SQLiteStore
from noema.application.classification import Classification, Classifier
from noema.infrastructure.ollama import OllamaError
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


def browser_session(domain, title, url=None):
    event = ActivityEvent(
        timestamp="2026-09-04T10:00:00Z",
        duration=120,
        device="laptop",
        app="Chrome",
        title=title,
        domain=domain,
        url=url,
        bucket_id="window_laptop",
        source_event_id="browser-1",
    )
    return Sessionizer().sessionize([event])[0]


class FakeOllama:
    model = "test-model"

    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    def generate_json(self, prompt):
        self.prompts.append(prompt)
        return self.payload


class FailingOllama:
    model = "offline-model"

    def generate_json(self, prompt):
        raise OllamaError("offline")


class CountingFailingOllama(FailingOllama):
    def __init__(self):
        self.calls = 0

    def generate_json(self, prompt):
        self.calls += 1
        return super().generate_json(prompt)


def test_ollama_classification_is_structured_and_persisted():
    current = browser_session("example.com", "Unclear project dashboard", "https://example.com/project")
    client = FakeOllama(
        {
            "category": "productive",
            "topic": "reinforcement_learning",
            "project": "robotics",
            "activity_type": "paper_reading",
            "productivity": "productive",
            "activity": "Reading project research",
            "signal": "The page title and project URL indicate focused research work.",
            "confidence": 0.94,
        }
    )
    classification = Classifier(client=client).classify(current)

    assert isinstance(classification, Classification)
    assert classification.session_id == current.id
    assert classification.category == "productive"
    assert classification.confidence == 0.94
    assert classification.provider == "ollama"
    assert '"title": "Unclear project dashboard"' in client.prompts[0]

    store = SQLiteStore()
    assert store.insert_classification(classification) is True
    assert store.insert_classification(classification) is False
    found = store.query_classifications(session_id=current.id)
    assert found[0].to_dict() == classification.to_dict()
    store.close()


def test_classifier_uses_explicit_low_confidence_offline_fallback():
    classification = Classifier(client=FailingOllama()).classify(session())

    assert classification.category == "neutral"
    assert classification.provider == "ollama"
    assert classification.source == "pending"
    assert classification.classification_status == "pending"
    assert classification.confidence == 0.0


def test_classifier_circuits_repeated_ollama_failures_for_daemon_batches():
    client = CountingFailingOllama()
    classifier = Classifier(client=client)
    current = browser_session("example.com", "Unclear project dashboard")

    first = classifier.classify(current)
    second = classifier.classify(current)

    assert first.provider == "ollama"
    assert second.provider == "ollama"
    assert first.source == "pending"
    assert second.source == "pending"
    assert first.classification_status == "pending"
    assert second.classification_status == "pending"
    # Failures are deliberately NOT cached: each attempt retries the provider
    # (3 transport attempts each). The scheduler gates retry frequency via
    # persisted backoff instead of serving stale failures.
    assert client.calls == 6
    assert first.last_error and second.last_error


def test_classifier_does_not_turn_unknown_productivity_into_distraction():
    client = FakeOllama({"category": "productive", "productivity": "maybe", "confidence": 0.8, "activity": "Unknown", "signal": "Insufficient evidence."})

    classification = Classifier(client=client).classify(browser_session("example.com", "Unclear project dashboard"))

    assert classification.productivity == "neutral"


def test_classifier_prompt_keeps_browser_url_evidence():
    current = browser_session(
        "example.com",
        "Unclear project dashboard",
        "https://example.com/project",
    )
    client = FakeOllama({"category": "neutral", "productivity": "neutral", "activity": "Browsing an unclear page", "signal": "The page purpose is not established by the available evidence.", "confidence": 0.51})

    Classifier(client=client).classify(current)

    assert '"url": "https://example.com/project"' in client.prompts[0]


def test_legacy_provider_categories_normalize_to_canonical_verdicts():
    """Providers may return historical category labels; the classifier owns
    the canonical productive/distractive/neutral vocabulary."""

    class LegacyProvider:
        name = "legacy"
        model = "legacy-1"

        def __init__(self):
            self.categories = iter(["development", "research"])

        def classify(self, session, prompt):
            return {
                "category": next(self.categories),
                "activity": "Legacy labeled work",
                "signal": "The provider used a historical category label.",
                "productivity": "productive",
                "confidence": 0.8,
            }

    classifier = Classifier(provider=LegacyProvider())
    first = classifier.classify(browser_session("example.com", "Work one"))
    second = classifier.classify(browser_session("example.com", "Work two"))

    assert first.category == "productive"
    assert second.category == "productive"
    assert first.classification_status == "classified"
