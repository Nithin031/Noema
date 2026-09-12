from noema.domain.activity import ActivityEvent
from noema.application.classification import Classifier
from noema.infrastructure.providers import ProviderError
from noema.domain.sessions import Sessionizer


def make_session(app, title, domain=None, url=None):
    event = ActivityEvent(
        timestamp="2026-09-06T10:00:00Z",
        duration=60,
        device="laptop",
        app=app,
        title=title,
        domain=domain,
        url=url,
        bucket_id="window_laptop",
        source_event_id="contract-{}-{}".format(app, title),
    )
    return Sessionizer().sessionize([event])[0]


def test_provider_output_uses_only_the_canonical_categories():
    """Whatever labels a provider returns, stored verdicts use only the
    canonical productive/distractive/neutral vocabulary."""

    class LegacyProvider:
        name = "legacy"
        model = "legacy-1"

        def __init__(self):
            self.categories = iter([
                ("development", "productive"),
                ("research", "productive"),
                ("distractive", "distracting"),
                ("neutral", "neutral"),
            ])

        def classify(self, session, prompt):
            category, productivity = next(self.categories)
            return {
                "category": category,
                "subcategory": "legacy",
                "activity": "Legacy labeled activity",
                "signal": "The provider used a historical category label.",
                "productivity": productivity,
                "confidence": 0.8,
            }

    classifier = Classifier(provider=LegacyProvider())
    sessions = [
        make_session("Chrome", "Work {}".format(index), "example{}.com".format(index))
        for index in range(4)
    ]

    for session in sessions:
        result = classifier.classify(session)
        assert result.category in {"productive", "distractive", "neutral"}
        assert result.signal.casefold() not in {result.category, result.productivity}
        assert result.activity
        assert result.subcategory
        assert result.source


def test_unknown_ollama_category_is_neutral_validation_fallback():
    class MalformedProvider:
        name = "ollama"
        model = "test"

        def classify(self, session, prompt):
            return {
                "category": "unknown",
                "activity": "",
                "signal": "unknown",
                "confidence": 0.99,
            }

    result = Classifier(provider=MalformedProvider()).classify(
        make_session("Chrome", "Unclear page", "example.com")
    )

    assert result.category == "neutral"
    assert result.confidence == 0.0
    assert result.source == "pending"
    assert result.classification_status == "classification_failed"
    assert result.signal == "Classifier response invalid or unavailable"


def test_ollama_failure_falls_back_without_blocking_classification():
    class OfflineProvider:
        name = "ollama"
        model = "test"

        def classify(self, session, prompt):
            raise ProviderError("offline")

    result = Classifier(provider=OfflineProvider()).classify(
        make_session("Chrome", "Unclear page", "example.com")
    )

    assert result.category == "neutral"
    assert result.confidence == 0.0
    assert result.source == "pending"
    assert result.classification_status == "pending"
    assert result.signal == "Ollama classification unavailable"


def test_cache_key_contains_page_evidence_not_only_executable():
    class CountingProvider:
        name = "ollama"
        model = "test"

        def __init__(self):
            self.calls = 0

        def classify(self, session, prompt):
            self.calls += 1
            return {
                "category": "neutral",
                "subcategory": "ambiguous",
                "activity": "Browsing an unclear page",
                "signal": "The page purpose is not established by the available evidence.",
                "confidence": 0.4,
            }

    provider = CountingProvider()
    classifier = Classifier(provider=provider)
    first = make_session("Chrome", "Unclear page", "example.com")
    second = make_session("Chrome", "Different page", "other.example")

    classifier.classify(first)
    classifier.classify(first)
    classifier.classify(second)

    assert provider.calls == 2
