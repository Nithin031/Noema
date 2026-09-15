"""Model tier separation: classification owns meaning, nothing else."""

import pytest

from noema.infrastructure.providers import (
    CLASSIFICATION_MODELS,
    REASONING_MODELS,
    TIER_AUXILIARY,
    TIER_CLASSIFICATION,
    TIER_REASONING,
    FailureKind,
    GeminiProvider,
    ProviderChain,
    ProviderError,
    assert_tier,
    classify_failure,
    models_for_tier,
    tier_for_model,
)


def test_tier_constants_match_spec():
    assert CLASSIFICATION_MODELS == (
        "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash")
    assert REASONING_MODELS == (
        "gemini-3.5-flash", "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite", "gemini-2.5-flash")
    assert set(CLASSIFICATION_MODELS).isdisjoint(set(REASONING_MODELS))


def test_tier_for_model():
    assert tier_for_model("gemini-3.8-flash") == TIER_CLASSIFICATION
    assert tier_for_model("gemini-2.5-flash") == TIER_REASONING
    assert tier_for_model("nope-1") is None
    assert tier_for_model(None) is None


def test_models_for_tier():
    assert models_for_tier(TIER_CLASSIFICATION) == CLASSIFICATION_MODELS
    assert models_for_tier(TIER_REASONING) == REASONING_MODELS
    assert models_for_tier(TIER_AUXILIARY) == REASONING_MODELS
    assert models_for_tier("bogus") == ()


def test_assert_tier_rejects_cross_tier_and_unknown():
    assert assert_tier(list(CLASSIFICATION_MODELS), TIER_CLASSIFICATION,
                       "classification_models") == list(CLASSIFICATION_MODELS)
    with pytest.raises(ValueError):
        assert_tier(["gemini-3.5-flash"], TIER_CLASSIFICATION,
                    "classification_models")
    with pytest.raises(ValueError):
        assert_tier(["gemini-3.8-flash"], TIER_REASONING, "reasoning_models")
    with pytest.raises(ValueError):
        assert_tier(["mystery-model"], TIER_REASONING, "reasoning_models")
    with pytest.raises(ValueError):
        assert_tier([], TIER_CLASSIFICATION, "classification_models")
    with pytest.raises(ValueError):
        assert_tier(["gemini-3.8-flash"], "bogus-tier", "x")


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
        return self.payload


def _payload(category="neutral"):
    return {
        "category": category,
        "productivity": "neutral",
        "activity": "Observed activity",
        "signal": "Observed evidence supports this result.",
    }


def test_classification_chain_uses_only_classification_tier():
    hosted = [FakeProvider(model, _payload()) for model in CLASSIFICATION_MODELS]
    chain = ProviderChain(hosted=hosted)
    assert chain.classify(object(), "prompt")["category"] == "neutral"
    assert [provider.model for provider in hosted] == list(CLASSIFICATION_MODELS)
    assert all(tier_for_model(provider.model) == TIER_CLASSIFICATION
               for provider in hosted)


def test_classification_falls_through_tier_in_order():
    hosted = [
        FakeProvider("gemini-3.8-flash", error=ProviderError("offline")),
        FakeProvider("gemini-3.7-flash", error=ProviderError("offline")),
        FakeProvider("gemini-3.6-flash", _payload("productive")),
    ]
    chain = ProviderChain(hosted=hosted)
    assert chain.classify(object(), "prompt")["category"] == "productive"
    assert [provider.calls for provider in hosted] == [1, 1, 1]


def test_all_classification_models_failing_raises_without_fabrication():
    hosted = [FakeProvider(model, error=ProviderError("offline"))
              for model in CLASSIFICATION_MODELS]
    chain = ProviderChain(hosted=hosted)
    try:
        chain.classify(object(), "prompt")
        assert False, "expected ProviderError"
    except ProviderError:
        pass
    # No fallback into the lower tier happened: lower-tier models exist
    # in the codebase but were never constructed for this chain.


def test_reasoning_legs_never_include_classification_models():
    legs = [FakeProvider(model, _payload()) for model in REASONING_MODELS]
    assert all(tier_for_model(leg.model) == TIER_REASONING for leg in legs)
    assert not set(leg.model for leg in legs) & set(CLASSIFICATION_MODELS)


def test_classify_failure_states_are_explicit():
    assert classify_failure(ProviderError("HTTP 429 quota")) == FailureKind.RATE_LIMITED
    assert classify_failure(ProviderError("resource_exhausted")) == FailureKind.RATE_LIMITED
    assert classify_failure(TimeoutError("timed out")) == FailureKind.TIMEOUT
    assert classify_failure(ProviderError("request timeout")) == FailureKind.TIMEOUT
    assert classify_failure(ProviderError("connection reset")) == FailureKind.PROVIDER_ERROR
    assert classify_failure(OSError("boom")) == FailureKind.PROVIDER_ERROR
    assert FailureKind.SUCCESS != FailureKind.RATE_LIMITED
    assert len(FailureKind.ALL) == 6


def test_settings_tier_defaults_and_validation():
    from noema.config.settings import DaemonConfig

    config = DaemonConfig()
    assert config.classification_models == list(CLASSIFICATION_MODELS)
    assert config.reasoning_models == list(REASONING_MODELS)
    assert config.meme_models == list(REASONING_MODELS)
    assert config.auxiliary_models == list(REASONING_MODELS)
    dumped = config.to_dict()
    assert dumped["classification_models"] == list(CLASSIFICATION_MODELS)
    assert dumped["reasoning_models"] == list(REASONING_MODELS)
    # Legacy alias still feeds the classification tier.
    legacy = DaemonConfig.from_mapping(
        {"gemini_models": ["gemini-3.8-flash", "gemini-3.7-flash"]})
    assert legacy.classification_models == ["gemini-3.8-flash", "gemini-3.7-flash"]
    # Cross-tier substitution is rejected, never silently spent.
    import pytest

    with pytest.raises(ValueError):
        DaemonConfig(classification_models=["gemini-3.5-flash"])
    with pytest.raises(ValueError):
        DaemonConfig(reasoning_models=["gemini-3.8-flash"])
    with pytest.raises(ValueError):
        DaemonConfig(meme_models=["mystery-model"])
    with pytest.raises(ValueError):
        DaemonConfig(auxiliary_models=[])


def test_settings_tier_env_overrides():
    import os

    from noema.config.settings import DaemonConfig

    previous = dict(os.environ)
    os.environ["NOEMA_CLASSIFICATION_MODELS"] = "gemini-3.8-flash,gemini-3.7-flash"
    os.environ["NOEMA_REASONING_MODELS"] = "gemini-2.5-flash"
    try:
        config = DaemonConfig.from_environment()
        assert config.classification_models == ["gemini-3.8-flash", "gemini-3.7-flash"]
        assert config.reasoning_models == ["gemini-2.5-flash"]
    finally:
        os.environ.clear()
        os.environ.update(previous)


def test_daemon_wiring_keeps_tiers_separate():
    from noema.cli import daemon as daemon_module
    from noema.config.settings import DaemonConfig

    config = DaemonConfig()
    classifier = daemon_module._classifier(config)
    chain = classifier.provider
    hosted_models = [getattr(provider, "model", None)
                     for provider in getattr(chain, "providers", [])]
    assert hosted_models == list(CLASSIFICATION_MODELS)
    assert all(tier_for_model(model) == TIER_CLASSIFICATION
               for model in hosted_models)


def test_realtime_wiring_uses_reasoning_tier_only():
    import types

    from noema.cli import daemon as daemon_module
    from noema.config.settings import DaemonConfig

    config = DaemonConfig()
    service = types.SimpleNamespace()
    daemon_module._realtime_setup(service, config, classifier=None)
    verifier = service.fast_verifier
    ordered = ([getattr(verifier.fast_gemini, "model", None)]
               + [getattr(leg, "model", None)
                  for leg in list(getattr(verifier, "reasoning_providers", []))])
    assert ordered == list(REASONING_MODELS)
    assert all(tier_for_model(model) == TIER_REASONING for model in ordered)
    assert service.intervention_reasoner is not None
    reasoner_models = [getattr(leg, "model", None)
                       for leg in service.intervention_reasoner.legs]
    assert reasoner_models == list(REASONING_MODELS)
    assert service.meme_decider is not None
    decider_models = [getattr(leg, "model", None)
                      for leg in service.meme_decider.legs]
    assert decider_models == list(REASONING_MODELS)
    assert not set(reasoner_models + decider_models) & set(CLASSIFICATION_MODELS)
