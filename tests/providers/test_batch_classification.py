from noema.domain.activity import ActivityEvent
from noema.application.classification import Classifier
from noema.infrastructure.providers import ProviderChain
from noema.infrastructure.providers import (
    BATCH_TOKEN_BUDGET,
    ESCALATION_MODEL,
    WORKHORSE_MODEL,
    ProviderError,
)
from noema.domain.sessions import Sessionizer


def make_session(tag, title="Window work", domain="example.com"):
    event = ActivityEvent(
        timestamp="2026-09-06T10:00:00Z",
        duration=60,
        device="laptop",
        app="Chrome",
        title=title,
        domain=domain,
        bucket_id="window_laptop",
        source_event_id="batch-{}".format(tag),
    )
    return Sessionizer().sessionize([event])[0]


def valid_item(session_id, category="productive"):
    return {
        "session_id": session_id,
        "category": category,
        "subcategory": "research",
        "activity": "Batch classified work",
        "signal": "Batch evidence supports this result.",
        "confidence": 0.8,
        "productivity": "productive",
    }


class FakeBatchHost:
    name = "gemini"

    def __init__(self, model, handler):
        self.model = model
        self.handler = handler
        self.calls = 0

    def classify_batch(self, session_ids, evidences):
        self.calls += 1
        return self.handler(session_ids, evidences)


class FakeChain:
    """Mimics ProviderChain batch/single semantics for classifier tests."""

    name = "hosted_chain"
    model = None

    def __init__(self, payloads=None, fail_batch=False):
        self.payloads = payloads or {}
        self.fail_batch = fail_batch
        self.batch_calls = 0
        self.single_calls = 0
        self.last_batch = {}
        self.last_provider = "gemini"
        self.last_model = "fake-flash"

    def classify_batch(self, entries):
        self.batch_calls += 1
        if self.fail_batch:
            raise ProviderError("batch down")
        out = {}
        for entry in entries:
            session_id = entry["session"].id
            if session_id in self.payloads:
                out[session_id] = self.payloads[session_id]
                self.last_batch[session_id] = ("gemini", "fake-flash")
        return out

    def classify(self, session, prompt):
        self.single_calls += 1
        return {
            "category": "neutral",
            "subcategory": "ambiguous",
            "activity": "Single fallback",
            "signal": "Fell back to a single call.",
            "confidence": 0.5,
            "productivity": "neutral",
        }


def test_flash_models_lead_and_gemma_follows():
    # Workhorse first: high-frequency Lite model leads, escalation and
    # fallback models follow. Never an unconditional 20-RPD workhorse.
    assert ProviderChain.HOSTED_MODELS[:6] == (
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.8-flash",
    )
    assert WORKHORSE_MODEL == "gemini-3.5-flash-lite"
    assert ESCALATION_MODEL == "gemini-3.5-flash"
    assert ProviderChain.OPENROUTER_MODELS[0] == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"


def test_pack_batches_respects_token_budget_not_session_count():
    sessions = [make_session("pack-{}".format(i)) for i in range(8)]
    big_evidence = {"blob": "x" * 120000}
    entries = [
        {"session": session, "evidence": big_evidence, "prompt": "p"}
        for session in sessions
    ]
    host = FakeBatchHost("gemini-2.5-flash", lambda ids, evs: [])
    chain = ProviderChain(hosted=[host], ollama=FakeBatchHost("x", lambda i, e: []))
    budget = ProviderChain._batch_budget("gemini-2.5-flash")
    groups = chain._pack_batches(entries, host, budget)
    assert len(groups) > 1
    for group, _measured in groups:
        estimate = sum(len(str(item["evidence"])) // 4 + 500 for item in group)
        assert estimate <= budget <= BATCH_TOKEN_BUDGET


def test_batch_budgets_follow_model_windows():
    flash = ProviderChain._batch_budget("gemini-2.5-flash")
    gemma = ProviderChain._batch_budget("gemma-4-31b-it")
    unknown = ProviderChain._batch_budget("mystery-model")
    assert flash > gemma  # 250K TPM vs 16K TPM binds first
    assert flash <= BATCH_TOKEN_BUDGET
    assert gemma < 20000
    assert unknown > 0


def test_packer_verifies_with_the_real_tokenizer():
    sessions = [make_session("verify-{}".format(i)) for i in range(2)]

    class CountingHost(FakeBatchHost):
        def count_tokens(self, prompt):
            # Claim the pair never fits, whatever the estimate says.
            return 10 ** 9

    host = CountingHost("gemini-2.5-flash", lambda ids, evs: [])
    chain = ProviderChain(hosted=[host], ollama=FakeBatchHost("x", lambda i, e: []))
    entries = [{"session": s, "evidence": {"t": "tiny"}, "prompt": "p"} for s in sessions]
    groups = chain._pack_batches(entries, host, chain._batch_budget("gemini-2.5-flash"))
    assert [len(group) for group, _ in groups] == [1, 1]


def test_quota_throttle_splits_for_a_smaller_model():
    sessions = [make_session("quota-split-{}".format(i)) for i in range(8)]
    # ~13K-token group: over Gemma's 12K budget (forces the split) but both
    # halves fit its 16K TPM in the same minute.
    evidence = {"blob": "y" * 3000}
    entries = [{"session": s, "evidence": evidence, "prompt": "p"} for s in sessions]

    def failing(ids, evs):
        raise ProviderError("429 RESOURCE_EXHAUSTED quota exceeded")

    flash = FakeBatchHost("gemini-2.5-flash", failing)
    gemma = FakeBatchHost(
        "gemma-4-31b-it", lambda ids, evs: [valid_item(session_id) for session_id in ids])
    chain = ProviderChain(hosted=[flash, gemma], ollama=FakeBatchHost("x", lambda i, e: []))

    results = chain.classify_batch(entries)

    assert set(results) == {s.id for s in sessions}
    # Whole group fails (no cooldown: packing issue), first half fails once
    # (cooldown starts), second half skips the cooling provider entirely.
    assert flash.calls == 2
    assert gemma.calls == 2


def test_chain_batch_maps_results_by_session_id():
    sessions = [make_session("map-{}".format(i)) for i in range(3)]
    host = FakeBatchHost("gemini-2.5-flash",
                         lambda ids, evs: [valid_item(session_id) for session_id in ids])
    ollama = FakeBatchHost("llama3.2:3b", lambda ids, evs: [])
    ollama.name = "ollama"
    chain = ProviderChain(hosted=[host], ollama=ollama)
    entries = [{"session": s, "evidence": {"t": "e"}, "prompt": "p"} for s in sessions]

    results = chain.classify_batch(entries)

    assert set(results) == {s.id for s in sessions}
    assert host.calls == 1
    assert chain.last_batch[sessions[0].id] == ("gemini", "gemini-2.5-flash")


def test_chain_splits_group_on_length_error():
    sessions = [make_session("split-{}".format(i)) for i in range(4)]

    def handler(ids, evs):
        if len(ids) > 2:
            raise ProviderError("request exceeds maximum context length")
        return [valid_item(session_id) for session_id in ids]

    host = FakeBatchHost("gemini-2.5-flash", handler)
    chain = ProviderChain(hosted=[host], ollama=FakeBatchHost("x", lambda i, e: []))
    entries = [{"session": s, "evidence": {"t": "e"}, "prompt": "p"} for s in sessions]

    results = chain.classify_batch(entries)

    assert set(results) == {s.id for s in sessions}
    assert host.calls == 3  # full group fails, two halves succeed


def test_classifier_batches_many_and_falls_back_per_missing_item():
    sessions = [make_session("cls-{}".format(i)) for i in range(3)]
    payloads = {sessions[0].id: valid_item(sessions[0].id),
                sessions[1].id: valid_item(sessions[1].id, "distractive")}
    chain = FakeChain(payloads=payloads)
    classifier = Classifier(provider=chain)

    results = classifier.classify_many(sessions)

    assert chain.batch_calls == 1
    assert chain.single_calls == 1  # only the missing session falls back
    by_id = {item.session_id: item for item in results}
    assert by_id[sessions[0].id].category == "productive"
    assert by_id[sessions[0].id].source == "gemini"
    assert by_id[sessions[1].id].category == "distractive"
    assert by_id[sessions[2].id].activity == "Single fallback"


def test_classifier_batch_outage_falls_back_to_single_calls():
    sessions = [make_session("out-{}".format(i)) for i in range(2)]
    chain = FakeChain(fail_batch=True)
    classifier = Classifier(provider=chain)

    results = classifier.classify_many(sessions)

    assert chain.batch_calls == 1
    assert chain.single_calls == 2
    assert all(item.classification_status == "classified" for item in results)


def test_single_session_never_uses_batch():
    sessions = [make_session("solo")]
    chain = FakeChain(payloads={sessions[0].id: valid_item(sessions[0].id)})
    classifier = Classifier(provider=chain)

    (result,) = classifier.classify_many(sessions)

    assert chain.batch_calls == 0
    assert chain.single_calls == 1
    assert result.activity == "Single fallback"
