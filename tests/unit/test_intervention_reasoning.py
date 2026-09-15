"""Intervention reasoning contract: schema, decline semantics, history."""

from noema.application.realtime.intervention import (
    CLASS_BREAK,
    CLASS_CHALLENGE,
    CLASS_ENCOURAGEMENT,
    CLASS_MEME,
    CLASS_NONE,
    CLASS_NUDGE,
    CLASS_QUIRK,
    CLASS_REMINDER,
    INTERVENTION_BREAK_SUGGESTION,
    INTERVENTION_MEME_NUDGE,
    INTERVENTION_NONE,
    INTERVENTION_NOTIFICATION,
    INTERVENTION_REASONING_VERSION,
    RESPONSE_CLASS_TO_SELECTOR_CLASS,
    TONE_CALM,
    TONE_DIRECT,
    TONE_ENCOURAGING,
    TONE_NEUTRAL,
    TONE_PLAYFUL,
    TONE_SARCASTIC_LIGHT,
    TONE_TO_RESPONSE_TONE,
    TONE_URGENT,
    InterventionReasoner,
    build_intervention_history,
    build_intervention_prompt,
    declined_result,
    validate_intervention_payload,
)


def _valid(**overrides):
    base = {
        "should_intervene": True,
        "intervention_type": "MEME_NUDGE",
        "severity": 3,
        "tone": "PLAYFUL",
        "reason": "Sustained off-goal activity with no spontaneous recovery",
        "response_class": "MEME",
        "use_meme": True,
        "meme_intent": "procrastination_callout",
        "delivery": "OVERLAY",
        "confidence": 0.86,
    }
    base.update(overrides)
    return base


def test_valid_decision_normalizes():
    result = validate_intervention_payload(_valid())
    assert result is not None
    assert result["should_intervene"] is True
    assert result["intervention_type"] == INTERVENTION_MEME_NUDGE
    assert result["tone"] == TONE_PLAYFUL
    assert result["response_class"] == CLASS_MEME
    assert result["use_meme"] is True
    assert result["meme_intent"] == "procrastination_callout"


def test_do_not_intervene_is_valid_and_explicit():
    result = validate_intervention_payload(_valid(
        should_intervene=False, intervention_type="NONE",
        response_class="NONE", use_meme=False, meme_intent=None,
        delivery="NONE"))
    assert result is not None
    assert result["should_intervene"] is False
    assert result["delivery"] == "NONE"


def test_incoherent_combinations_rejected():
    # Silence must agree with itself.
    assert validate_intervention_payload(_valid(
        should_intervene=False, intervention_type="MEME_NUDGE")) is None
    assert validate_intervention_payload(_valid(
        should_intervene=True, intervention_type="NONE")) is None
    # Worthwhile action needs a servable class.
    assert validate_intervention_payload(_valid(response_class="NONE")) is None
    assert validate_intervention_payload(_valid(
        should_intervene=False, response_class="MEME")) is None
    # A meme needs a meme-serving class; never coerce silently.
    assert validate_intervention_payload(_valid(
        response_class="NUDGE", use_meme=True)) is None
    assert validate_intervention_payload(_valid(
        intervention_type="VIDEO", response_class="MEME")) is None
    assert validate_intervention_payload(_valid(tone="SNARKY")) is None


def test_malformed_payloads_rejected():
    assert validate_intervention_payload(None) is None
    assert validate_intervention_payload("intervene") is None
    assert validate_intervention_payload({}) is None
    assert validate_intervention_payload(_valid(severity=0)) is None
    assert validate_intervention_payload(_valid(severity=6)) is None
    assert validate_intervention_payload(_valid(confidence=1.5)) is None
    assert validate_intervention_payload(_valid(reason="   ")) is None
    assert validate_intervention_payload(_valid(should_intervene="maybe")) is None
    assert validate_intervention_payload(_valid(use_meme="perhaps")) is None
    assert validate_intervention_payload(_valid(response_class="VIDEO")) is None
    assert validate_intervention_payload(_valid(delivery="SATELLITE")) is None
    without_delivery = _valid()
    del without_delivery["delivery"]
    assert validate_intervention_payload(without_delivery) is not None


def test_class_and_tone_mappings_cover_everything():
    assert set(RESPONSE_CLASS_TO_SELECTOR_CLASS) == {
        CLASS_NONE, CLASS_NUDGE, CLASS_MEME, CLASS_QUIRK, CLASS_CHALLENGE,
        CLASS_REMINDER, CLASS_ENCOURAGEMENT, CLASS_BREAK}
    assert RESPONSE_CLASS_TO_SELECTOR_CLASS[CLASS_MEME] == "MEME"
    assert RESPONSE_CLASS_TO_SELECTOR_CLASS[CLASS_BREAK] == "NONE"
    assert set(TONE_TO_RESPONSE_TONE) == {
        TONE_PLAYFUL, TONE_DIRECT, TONE_CALM, TONE_ENCOURAGING,
        TONE_URGENT, TONE_SARCASTIC_LIGHT, TONE_NEUTRAL}
    assert TONE_TO_RESPONSE_TONE[TONE_SARCASTIC_LIGHT] == "sarcastic"


def test_declined_result_never_confirms():
    declined = declined_result("thin evidence")
    assert declined.should_intervene is False
    assert declined.intervention_type == INTERVENTION_NONE
    assert declined.response_class == CLASS_NONE
    assert declined.use_meme is False
    assert declined.reasoning_version == INTERVENTION_REASONING_VERSION


def test_history_builder_bounds_and_shapes():
    history = build_intervention_history(
        recent_interventions=[
            {"mode": "MEME", "status": "EXECUTED",
             "created_at": "2026-01-01T00:00:00Z",
             "outcome": "NOT_RECOVERED", "user_action": "lock_in",
             "note": "should never surface", "extra": "x"},
            "not-a-mapping",
        ] * 4 + [
            {"mode": "NOTIFICATION", "status": "EXECUTED",
             "created_at": "2026-01-02T00:00:00Z"},
        ] * 6,
        recent_outcomes=[
            {"recovery_status": "NOT_RECOVERED", "intervention_type": "MEME",
             "attribution": "direct", "recovery_duration_seconds": 252.0,
             "note": "dropped"},
        ] * 10,
        break_state={"active": True, "extra": "dropped"},
        recent_feedback=[
            {"feedback_type": "USEFULNESS", "value": "UNHELPFUL",
             "note": "free text never ships"},
        ] * 20,
    )
    assert len(history["recent_interventions"]) == 5
    assert history["recent_interventions"][0]["mode"] == "MEME"
    assert "note" not in history["recent_interventions"][0]
    assert "extra" not in history["recent_interventions"][0]
    assert len(history["recent_outcomes"]) == 5
    assert history["break_active"] is True
    assert len(history["recent_feedback"]) == 10
    assert set(history["recent_feedback"][0]) == {"feedback_type", "value"}


def test_prompt_puts_current_evidence_before_history():
    prompt = build_intervention_prompt({"goal": {"text": "study"}}, {"x": 1})
    assert "should_intervene" in prompt
    assert "DO_NOT_INTERVENE" in prompt
    assert "UNKNOWN" in prompt
    body = prompt.split("EVIDENCE:\n", 1)[1]
    assert body.index('"current_evidence"') < body.index('"history"')


class FakeLeg:
    def __init__(self, name="gemini", model="gemini-3.5-flash",
                 payload=None, error=None):
        self.name = name
        self.model = model
        self._payload = payload
        self._error = error
        self.calls = 0

    def complete_json(self, prompt, max_tokens=500):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return dict(self._payload)


def _context():
    return {
        "goal": {"text": "Study control systems"},
        "current_episode": {"activity": "watching videos", "category": "distractive"},
        "alignment": {"relation": "misaligned"},
        "behavior": {"candidate_score": 0.8},
        "presence": {"state": "active", "break_active": False},
    }


def test_reasoner_returns_decision_on_valid_output():
    leg = FakeLeg(payload=_valid())
    reasoner = InterventionReasoner(legs=[leg])
    decision = reasoner.reason(_context(), {})
    assert decision.should_intervene is True
    assert decision.selector_class == "MEME"
    assert decision.selector_tone == "playful"
    assert decision.model_calls == 1
    assert leg.calls == 1


def test_reasoner_decline_on_do_not_intervene():
    leg = FakeLeg(payload=_valid(
        should_intervene=False, intervention_type="NONE",
        response_class="NONE", use_meme=False, meme_intent=None))
    decision = InterventionReasoner(legs=[leg]).reason(_context(), {})
    assert decision.should_intervene is False
    assert decision.skipped is False


def test_reasoner_failure_declines_with_explicit_kind():
    from noema.infrastructure.providers import ProviderError

    leg = FakeLeg(error=ProviderError("HTTP 429 quota"))
    decision = InterventionReasoner(legs=[leg]).reason(_context(), {})
    assert decision.should_intervene is False
    assert decision.failure_kind == "RATE_LIMITED"
    assert leg.calls == 1


def test_reasoner_timeout_is_not_neutral():
    decision = InterventionReasoner(
        legs=[FakeLeg(error=TimeoutError("timed out"))]).reason(_context(), {})
    assert decision.should_intervene is False
    assert decision.failure_kind == "TIMEOUT"


def test_reasoner_malformed_output_declines():
    decision = InterventionReasoner(
        legs=[FakeLeg(payload={"should_intervene": True})]).reason(_context(), {})
    assert decision.should_intervene is False
    assert decision.failure_kind == "INVALID_OUTPUT"


def test_reasoner_no_legs_is_unavailable_not_neutral():
    decision = InterventionReasoner(legs=[]).reason(_context(), {})
    assert decision.should_intervene is False
    assert decision.failure_kind == "UNAVAILABLE"


def test_reasoner_empty_context_declines_without_calling():
    leg = FakeLeg(payload=_valid())
    decision = InterventionReasoner(legs=[leg]).reason({}, {})
    assert decision.should_intervene is False
    assert leg.calls == 0


def test_reasoner_cache_converges_retries():
    leg = FakeLeg(payload=_valid())
    reasoner = InterventionReasoner(legs=[leg])
    first = reasoner.reason(_context(), {})
    second = reasoner.reason(_context(), {})
    assert first.should_intervene is True
    assert leg.calls == 1
    assert second is first


def test_reasoner_never_raises_for_provider_reasons():
    from noema.infrastructure.providers import ProviderError

    reasoner = InterventionReasoner(
        legs=[FakeLeg(error=ProviderError("boom")), FakeLeg(error=OSError("down"))])
    decision = reasoner.reason(_context(), {})
    assert decision.should_intervene is False
    assert decision.failure_kind in {"PROVIDER_ERROR", "INVALID_OUTPUT", "UNAVAILABLE"}


def test_reasoner_falls_forward_to_next_leg():
    from noema.infrastructure.providers import ProviderError

    legs = [FakeLeg(error=ProviderError("HTTP 429 quota")),
            FakeLeg(payload=_valid())]
    decision = InterventionReasoner(legs=legs).reason(_context(), {})
    assert decision.should_intervene is True
    assert [leg.calls for leg in legs] == [1, 1]
    assert decision.attempts[0][0] == "gemini"
