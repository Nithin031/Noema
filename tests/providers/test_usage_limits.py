from noema.infrastructure.providers import ProviderChain, ProviderError
from noema.infrastructure.providers import MODEL_LIMITS, QuotaLedger


class FakeProvider:
    name = "gemini"

    def __init__(self, model, payload=None, error=None, tokens=100):
        self.model = model
        self.payload = payload
        self.error = error
        self.tokens = tokens
        self.calls = 0

    def classify(self, session, prompt):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload

    def count_tokens(self, prompt):
        return self.tokens


def payload(category="neutral"):
    return {
        "category": category,
        "productivity": "neutral",
        "activity": "Observed activity",
        "signal": "Observed evidence supports this result.",
    }


def test_per_model_limits_come_from_the_quota_table():
    chain = ProviderChain(
        hosted=[FakeProvider("gemma-4-31b-it", payload()), FakeProvider("mystery-model", payload())],
        ollama=FakeProvider("llama3.2:3b", payload()),
    )
    assert (chain.rate_limits["gemma-4-31b-it"].rpm_limit,
            chain.rate_limits["gemma-4-31b-it"].tpm_limit,
            chain.rate_limits["gemma-4-31b-it"].rpd_limit) == MODEL_LIMITS["gemma-4-31b-it"]
    assert (chain.rate_limits["mystery-model"].rpm_limit,
            chain.rate_limits["mystery-model"].tpm_limit,
            chain.rate_limits["mystery-model"].rpd_limit) == (5, 250000, 20)


def test_rpd_exhaustion_skips_model_without_any_api_call(tmp_path):
    usage = tmp_path / "usage.json"
    first = FakeProvider("gemma-4-31b-it", payload())
    second = FakeProvider("gemma-4-26b-a4b-it", payload("productive"))
    ollama = FakeProvider("llama3.2:3b", payload("distractive"))
    ollama.name = "ollama"
    chain = ProviderChain(hosted=[first, second], ollama=ollama, usage_path=usage)
    # Burn the whole daily allowance for the first model.
    for _ in range(MODEL_LIMITS["gemma-4-31b-it"][2]):
        chain.ledger.consume("llm", "gemma-4-31b-it", 700)

    assert chain.classify(object(), "prompt")["category"] == "productive"
    assert first.calls == 0
    assert second.calls == 1
    assert ollama.calls == 0


def test_ledger_rolls_over_at_local_midnight(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone

    from zoneinfo import ZoneInfo

    usage = tmp_path / "usage.json"
    zone = ZoneInfo("Asia/Kolkata")
    yesterday = (datetime.now(zone) - timedelta(days=1)).date().isoformat()
    usage.write_text(json.dumps({
        "day": yesterday,
        "counts": {"llm:gemini-2.5-flash": {"requests": 20, "tokens": 999}},
    }))
    ledger = QuotaLedger(usage)
    assert ledger.day != yesterday
    assert ledger.usage("llm", "gemini-2.5-flash") == {"requests": 0, "tokens": 0}
    assert ledger.check("llm", "gemini-2.5-flash", 700, 20) is True


def test_ledger_keeps_same_day_counts(tmp_path):
    import json
    from datetime import datetime

    from zoneinfo import ZoneInfo

    usage = tmp_path / "usage.json"
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    usage.write_text(json.dumps({
        "day": today,
        "counts": {"llm:gemini-2.5-flash": {"requests": 20, "tokens": 999}},
    }))
    ledger = QuotaLedger(usage)
    assert ledger.day == today
    assert ledger.usage("llm", "gemini-2.5-flash") == {"requests": 20, "tokens": 999}
    assert ledger.check("llm", "gemini-2.5-flash", 700, 20) is False


def test_ledger_survives_chain_restart(tmp_path):
    usage = str(tmp_path / "usage.json")
    first = ProviderChain(hosted=[FakeProvider("gemma-4-31b-it", payload())],
                                ollama=FakeProvider("llama3.2:3b", payload()))
    first.ledger = QuotaLedger(usage)
    first.ledger.consume("llm", "gemma-4-31b-it", 700)

    second = ProviderChain(hosted=[FakeProvider("gemma-4-31b-it", payload())],
                                 ollama=FakeProvider("llama3.2:3b", payload()),
                                 usage_path=usage)
    assert second.usage()["llm:gemma-4-31b-it"]["requests"] == 1
    assert second.rate_limits["gemma-4-31b-it"].requests_today == 1


def test_token_budget_skip_never_calls_the_model():
    big = FakeProvider("gemma-4-31b-it", payload(), tokens=15_999)
    small = FakeProvider("gemma-4-26b-a4b-it", payload("productive"), tokens=100)
    ollama = FakeProvider("llama3.2:3b", payload("distractive"))
    ollama.name = "ollama"
    chain = ProviderChain(hosted=[big, small], ollama=ollama)

    assert chain.classify(object(), "prompt")["category"] == "productive"
    assert big.calls == 0  # 15999 + 600 reserve > 16000 TPM
    assert small.calls == 1


def test_embedding_usage_is_tracked_against_its_own_quota(tmp_path):
    usage = str(tmp_path / "usage.json")
    chain = ProviderChain(hosted=[FakeProvider("gemma-4-31b-it", payload())],
                                ollama=FakeProvider("llama3.2:3b", payload()),
                                usage_path=usage)
    model = "gemini-embedding-001"
    assert chain.embedding_allowed(model, 100) is True
    assert chain.track_embedding(model, 100) is True
    for _ in range(999):
        assert chain.track_embedding(model, 100) is True
    assert chain.embedding_allowed(model, 100) is False
    assert chain.track_embedding(model, 100) is False
    snapshot = chain.usage()
    assert snapshot["embedding:{}".format(model)]["requests"] == 1000
    assert "llm:{}".format(model) not in snapshot
