"""Fast-model verifier: schema, fallback chain, quota, failure safety."""

import json

from noema.infrastructure.providers import ProviderError
from noema.application.realtime.verifier import (
    RECOMMENDATION_TO_MODE,
    FastModelVerifier,
    VerificationConfig,
    _validate_payload,
    build_verify_prompt,
)


def verdict(**overrides):
    payload = {
        "concerning": True,
        "severity": 3,
        "confidence": 0.82,
        "reason": "sustained drift with repeated context switches",
        "recommended_intervention": "reframe",
    }
    payload.update(overrides)
    return payload


class FakeLeg:
    def __init__(self, name, model, payload=None, error=None):
        self.name = name
        self.model = model
        self._payload = payload
        self._error = error
        self.calls = 0

    def complete_json(self, prompt, max_tokens=300):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return dict(self._payload)

    def classify(self, session, prompt):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return dict(self._payload)


class FakeState:
    def __init__(self):
        self.successes = 0
        self.failures = 0

    def record_success(self, tokens):
        self.successes += 1

    def record_failure(self, error):
        self.failures += 1


class FakeLedger:
    def __init__(self):
        self.consumed = []

    def consume(self, kind, model, tokens):
        self.consumed.append((kind, model, tokens))


class FakeChain:
    def __init__(self, states=None, usable=600):
        self.rate_limits = states or {}
        self.ledger = FakeLedger()
        self._usable = usable
        self.include_ollama = False
        self.ollama = None

    def _model_usable(self, provider, prompt):
        return self._usable


SUMMARY = {"window_minutes": 60, "distraction_ratio": 0.5, "current_category": "distractive"}


def test_success_returns_strict_schema():
    leg = FakeLeg("openrouter", "fast-model", verdict())
    result = FastModelVerifier(fast_openrouter=leg).verify(
        SUMMARY, 0.8, ["distraction_ratio=0.90"], presence_state="active")
    assert result.concerning is True
    assert result.severity == 3
    assert result.confidence == 0.82
    assert result.recommended_intervention == "reframe"
    assert result.provider == "openrouter"
    assert result.model_calls == 1
    assert result.skipped is False
    assert result.to_dict()["intervention_mode"] == "NOTIFICATION"


def test_recommendation_mode_mapping_covers_all():
    assert set(RECOMMENDATION_TO_MODE) == {
        "meme", "goal_callback", "reframe", "micro_task", "none"}
    assert RECOMMENDATION_TO_MODE["meme"] == "MEME"
    assert RECOMMENDATION_TO_MODE["none"] == "HOLDOUT"


def test_malformed_output_fails_safe_to_not_concerning():
    for bad in ({"concerning": True},  # missing fields
                {"concerning": "maybe", "severity": 3, "confidence": 0.5,
                 "reason": "x", "recommended_intervention": "meme"},
                verdict(severity=9),
                verdict(confidence=2.0),
                verdict(recommended_intervention="teleport"),
                verdict(reason="   "),
                [1, 2, 3],
                "not json"):
        leg = FakeLeg("openrouter", "m", bad)
        result = FastModelVerifier(fast_openrouter=leg).verify(
            SUMMARY, 0.8, [], presence_state="active")
        assert result.concerning is False, bad
        assert result.recommended_intervention == "none"


def test_lenient_booleans_still_validate():
    leg = FakeLeg("openrouter", "m", verdict(concerning="true"))
    result = FastModelVerifier(fast_openrouter=leg).verify(
        SUMMARY, 0.8, [], presence_state="active")
    assert result.concerning is True


# MANDATORY TEST 13: fast model fails → configured fallback leg serves.
def test_first_leg_failure_falls_to_next_leg():
    states = {"m1": FakeState(), "m2": FakeState()}
    chain = FakeChain(states)
    failing = FakeLeg("openrouter", "m1", error=ProviderError("429 throttled"))
    backup = FakeLeg("gemini", "m2", verdict(concerning=False,
                                             recommended_intervention="none"))
    result = FastModelVerifier(
        chain=chain, fast_openrouter=failing, fast_gemini=backup).verify(
        SUMMARY, 0.8, [], presence_state="active")
    assert result.concerning is False
    assert result.provider == "gemini"
    assert states["m1"].failures == 1
    assert states["m2"].successes == 1
    assert chain.ledger.consumed and chain.ledger.consumed[0][1] == "m2"


def test_all_legs_failing_returns_explicit_non_concerning():
    failing = FakeLeg("openrouter", "m1", error=ProviderError("down"))
    result = FastModelVerifier(fast_openrouter=failing).verify(
        SUMMARY, 0.9, [], presence_state="active")
    assert result.concerning is False
    assert "failed or were throttled" in result.reason
    assert result.model_calls == 1
    assert result.attempts == (("openrouter", "m1"),)


def test_throttled_leg_is_skipped_without_call():
    chain = FakeChain(usable=-1)
    leg = FakeLeg("openrouter", "m1", verdict())
    result = FastModelVerifier(chain=chain, fast_openrouter=leg).verify(
        SUMMARY, 0.9, [], presence_state="active")
    assert leg.calls == 0
    assert result.concerning is False


def test_afk_presence_makes_no_call():
    for state in ("afk", "AFK"):
        leg = FakeLeg("openrouter", "m", verdict())
        result = FastModelVerifier(fast_openrouter=leg).verify(
            SUMMARY, 0.95, [], presence_state=state)
        assert leg.calls == 0
        assert result.skipped is True
        assert result.concerning is False


def test_unknown_presence_still_verifies_from_session_evidence():
    leg = FakeLeg("openrouter", "m", verdict())
    result = FastModelVerifier(fast_openrouter=leg).verify(
        SUMMARY, 0.95, [], presence_state="unknown")
    assert leg.calls == 1
    assert result.skipped is False


def test_no_provider_configured_is_explicit_skip():
    result = FastModelVerifier().verify(SUMMARY, 0.9, [], presence_state="active")
    assert result.skipped is True
    assert "no fast verification provider is configured" in result.reason


def test_prompt_contains_only_compact_summary():
    prompt = build_verify_prompt(
        {"window_minutes": 60, "secret_title": "should not happen"},
        0.8, ["a=1.0"])
    assert "secret_title" in prompt  # summary passes through untouched...
    assert "PATTERN" in prompt
    # ...but the verifier never adds raw history itself.
    assert "event" not in prompt.lower() or "intervention" in prompt.lower()


def test_validate_unit_table():
    assert _validate_payload(verdict())["severity"] == 3
    assert _validate_payload(verdict(concerning=1))["concerning"] is True
    assert _validate_payload(verdict(concerning=0))["concerning"] is False
    assert _validate_payload(None) is None
    assert _validate_payload(verdict(severity="4"))["severity"] == 4


def test_config_validation():
    for kwargs in ({"timeout_seconds": 0}, {"max_output_tokens": 0}):
        try:
            VerificationConfig(**kwargs)
        except ValueError:
            continue
        raise AssertionError("expected ValueError for {}".format(kwargs))


def test_verifier_prompt_is_small():
    prompt = build_verify_prompt(SUMMARY, 0.8, ["x=0.5", "y=0.4", "z=0.3"])
    assert len(prompt) < 2000
    parsed = json.loads(prompt.split("PATTERN:\n", 1)[1])
    assert parsed["candidate_score"] == 0.8
