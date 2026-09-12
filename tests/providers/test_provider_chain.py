from noema.infrastructure.providers import ProviderChain, ProviderError


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


def payload(category="neutral"):
    return {
        "category": category,
        "productivity": "neutral",
        "activity": "Observed activity",
        "signal": "Observed evidence supports this result.",
    }


def test_hosted_chain_stops_at_first_success():
    first = FakeProvider("gemma-4-31b", payload())
    second = FakeProvider("gemma-4-26b", payload("productive"))
    ollama = FakeProvider("llama3.2:3b", payload("distractive"))
    ollama.name = "ollama"

    chain = ProviderChain(hosted=[first, second], ollama=ollama)

    assert chain.classify(object(), "prompt")["category"] == "neutral"
    assert first.calls == 1
    assert second.calls == 0
    assert ollama.calls == 0


def test_hosted_chain_reaches_ollama_only_after_all_hosted_fail():
    hosted = [
        FakeProvider("gemma-4-31b", error=ProviderError("offline")),
        FakeProvider("gemma-4-26b", error=ProviderError("offline")),
    ]
    ollama = FakeProvider("llama3.2:3b", payload("neutral"))
    ollama.name = "ollama"

    chain = ProviderChain(hosted=hosted, ollama=ollama, include_ollama=True)

    assert chain.classify(object(), "prompt")["category"] == "neutral"
    assert [provider.calls for provider in hosted] == [1, 1]
    assert ollama.calls == 1
    assert chain.last_provider == "ollama"
    assert chain.last_model == "llama3.2:3b"


def test_hosted_chain_attempts_all_six_before_fallback():
    hosted = [FakeProvider("model-{}".format(index), error=ProviderError("offline")) for index in range(1, 7)]
    ollama = FakeProvider("llama3.2:3b", payload())
    ollama.name = "ollama"
    chain = ProviderChain(hosted=hosted, ollama=ollama, include_ollama=True)

    chain.classify(object(), "prompt")

    assert [provider.calls for provider in hosted] == [1] * 6
    assert ollama.calls == 1


def test_gemini_only_mode_never_calls_ollama():
    hosted = [FakeProvider("gemini-3.5-flash-lite", error=ProviderError("offline"))]
    ollama = FakeProvider("llama3.2:3b", payload("distractive"))
    ollama.name = "ollama"
    chain = ProviderChain(hosted=hosted, ollama=ollama, include_ollama=False)

    try:
        chain.classify(object(), "prompt")
        assert False, "expected ProviderError"
    except ProviderError:
        pass
    assert [provider.calls for provider in hosted] == [1]
    assert ollama.calls == 0


def test_hosted_capacity_exhaustion_skips_model_without_calling_ollama():
    first = FakeProvider("gemma-4-31b", payload())
    second = FakeProvider("gemma-4-26b", payload("productive"))
    ollama = FakeProvider("llama3.2:3b", payload("distractive"))
    ollama.name = "ollama"
    chain = ProviderChain(hosted=[first, second], ollama=ollama)
    chain.rate_limits[first.model].rpm_limit = 0

    assert chain.classify(object(), "prompt")["category"] == "productive"
    assert first.calls == 0
    assert second.calls == 1
    assert ollama.calls == 0