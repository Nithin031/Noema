"""Workhorse/escalation routing and fallback-exclusion tests (spec 27-32)."""

from noema.application.classification import Classifier
from noema.domain.meaningful.models import MeaningfulSession
from noema.infrastructure.providers import (
    ESCALATION_MODEL,
    WORKHORSE_MODEL,
    ProviderChain,
)


class FakeProvider:
    name = "gemini"

    def __init__(self, model, payload=None, error=None):
        self.model = model
        self.payload = payload
        self.error = error
        self.calls = 0

    def classify(self, session, prompt):
        self.calls += 1
        if self.error:
            raise self.error
        return dict(self.payload)

    def count_tokens(self, prompt):
        return 100


def verdict(category="neutral", confidence=0.3, evidence="weak"):
    return {
        "category": category,
        "subcategory": "ambiguous",
        "activity": "Observed activity",
        "signal": "Observed evidence supports this result.",
        "productivity": "neutral",
        "confidence": confidence,
        "evidence_quality": evidence,
    }


def make_episode(tag="esc", minutes=10):
    return MeaningfulSession(
        start_time="2026-09-06T10:00:00Z",
        end_time="2026-09-06T10:{:02d}:00Z".format(minutes),
        device_set=("laptop",),
        activity_session_ids=("raw-{}".format(tag),),
        active_duration_seconds=float(minutes * 60),
    )


def make_chain(workhorse_payload, escalation_payload):
    workhorse = FakeProvider(WORKHORSE_MODEL, workhorse_payload)
    escalation = FakeProvider(ESCALATION_MODEL, escalation_payload)
    chain = ProviderChain(hosted=[workhorse, escalation], ollama=None,
                          include_ollama=False, openrouter=[])
    return chain, workhorse, escalation


def test_workhorse_model_routes_first():
    assert ProviderChain.HOSTED_MODELS[0] == WORKHORSE_MODEL
    chain, workhorse, escalation = make_chain(
        verdict("productive", 0.9, "strong"), verdict("productive", 0.9, "strong"))
    classifier = Classifier(provider=chain)
    result = classifier.classify(make_episode())
    assert result.category == "productive"
    assert workhorse.calls == 1
    assert escalation.calls == 0


def test_escalation_only_on_low_confidence_weak_long_episode():
    chain, workhorse, escalation = make_chain(
        verdict("neutral", 0.3, "weak"), verdict("productive", 0.8, "moderate"))
    classifier = Classifier(provider=chain)
    result = classifier.classify(make_episode())
    assert workhorse.calls == 1
    assert escalation.calls == 1
    assert result.category == "productive"
    assert result.model == ESCALATION_MODEL


def test_no_escalation_for_confident_verdict():
    chain, workhorse, escalation = make_chain(
        verdict("neutral", 0.7, "weak"), verdict("productive", 0.9, "strong"))
    classifier = Classifier(provider=chain)
    classifier.classify(make_episode())
    assert escalation.calls == 0


def test_no_escalation_for_short_episode():
    chain, workhorse, escalation = make_chain(
        verdict("neutral", 0.3, "weak"), verdict("productive", 0.9, "strong"))
    classifier = Classifier(provider=chain)
    short = MeaningfulSession(
        start_time="2026-09-06T10:00:00Z", end_time="2026-09-06T10:01:00Z",
        device_set=("laptop",), activity_session_ids=("raw-s",),
        active_duration_seconds=60.0)
    classifier.classify(short)
    assert escalation.calls == 0


def test_no_escalation_for_strong_evidence():
    chain, workhorse, escalation = make_chain(
        verdict("neutral", 0.3, "strong"), verdict("productive", 0.9, "strong"))
    classifier = Classifier(provider=chain)
    classifier.classify(make_episode())
    assert escalation.calls == 0


def test_escalation_keeps_workhorse_when_not_better():
    chain, workhorse, escalation = make_chain(
        verdict("neutral", 0.4, "weak"), verdict("neutral", 0.2, "weak"))
    classifier = Classifier(provider=chain)
    result = classifier.classify(make_episode())
    assert escalation.calls == 1
    assert result.confidence == 0.4
    assert result.model == WORKHORSE_MODEL


def test_escalation_budget_is_bounded(tmp_path):
    chain, workhorse, escalation = make_chain(
        verdict("neutral", 0.3, "weak"), verdict("productive", 0.9, "strong"))
    for _ in range(10):
        chain.ledger.consume("escalation", ESCALATION_MODEL, 100)
    classifier = Classifier(provider=chain)
    classifier.classify(make_episode())
    assert escalation.calls == 0


def test_openrouter_registry_has_no_unreliable_models():
    from noema.infrastructure.providers import ProviderChain as Chain
    assert Chain.OPENROUTER_MODELS == (
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",)
    for retired in ("nvidia/nemotron-3-super-120b-a12b:free",
                    "nvidia/nemotron-3-ultra-550b-a55b:free",
                    "google/gemma-4-26b-a4b-it:free",
                    "google/gemma-4-31b-it:free"):
        assert retired not in Chain.OPENROUTER_MODELS


def test_default_chain_is_google_only():
    chain = ProviderChain()
    assert chain.include_ollama is False
    assert chain.providers, "chain must serve hosted models"
    for provider in chain.providers:
        assert provider.name == "gemini"
        assert "gemma" not in str(provider.model)
        assert "nvidia" not in str(provider.model)
        assert "openrouter" not in str(provider.model)
