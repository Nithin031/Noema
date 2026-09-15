"""V3 reasoning contract tests: states, validation, fail-safe behavior."""

from noema.application.realtime.reasoning import (
    RESPONSE_MEME,
    RESPONSE_NONE,
    STATE_DISTRACTED,
    STATE_INTENTIONAL_BREAK,
    STATE_NOT_DISTRACTED,
    STATE_UNCERTAIN,
    build_reasoning_context,
    build_reasoning_prompt,
    uncertain_result,
    validate_reasoning_payload,
)


def _valid(**overrides):
    base = {
        "state": "DISTRACTED",
        "confidence": 0.8,
        "severity": 3,
        "evidence_quality": "strong",
        "reason": "32 minutes of entertainment with no return to the goal",
        "intervention_worthwhile": True,
        "recommended_response_class": "MEME",
        "evidence_gaps": [],
    }
    base.update(overrides)
    return base


def test_valid_distracted_payload_normalizes():
    result = validate_reasoning_payload(_valid())
    assert result is not None
    assert result["state"] == STATE_DISTRACTED
    assert result["intervention_worthwhile"] is True
    assert result["recommended_response_class"] == RESPONSE_MEME


def test_all_four_states_accepted():
    for state in (STATE_NOT_DISTRACTED, STATE_UNCERTAIN, STATE_DISTRACTED,
                  STATE_INTENTIONAL_BREAK):
        result = validate_reasoning_payload(_valid(
            state=state, intervention_worthwhile=False,
            recommended_response_class="NONE"))
        assert result is not None, state
        assert result["state"] == state


def test_worthwhile_only_valid_for_distracted():
    for state in (STATE_NOT_DISTRACTED, STATE_UNCERTAIN, STATE_INTENTIONAL_BREAK):
        assert validate_reasoning_payload(_valid(
            state=state, intervention_worthwhile=True)) is None


def test_none_class_with_worthwhile_rejected():
    assert validate_reasoning_payload(_valid(
        recommended_response_class="NONE")) is None


def test_malformed_payloads_rejected():
    assert validate_reasoning_payload(None) is None
    assert validate_reasoning_payload("DISTRACTED") is None
    assert validate_reasoning_payload({}) is None
    assert validate_reasoning_payload(_valid(state="MAYBE")) is None
    assert validate_reasoning_payload(_valid(confidence=1.5)) is None
    assert validate_reasoning_payload(_valid(severity=0)) is None
    assert validate_reasoning_payload(_valid(severity=6)) is None
    assert validate_reasoning_payload(_valid(evidence_quality="solid")) is None
    assert validate_reasoning_payload(_valid(reason="   ")) is None
    assert validate_reasoning_payload(_valid(recommended_response_class="VIDEO")) is None
    assert validate_reasoning_payload(_valid(intervention_worthwhile="maybe")) is None
    assert validate_reasoning_payload(_valid(evidence_gaps="thin")) is None


def test_may_confirm_only_distracted_worthwhile():
    assert validate_reasoning_payload(_valid()) is not None
    from noema.application.realtime.reasoning import ReasoningResult
    confirmed = ReasoningResult(
        state=STATE_DISTRACTED, confidence=0.8, severity=3,
        evidence_quality="strong", reason="r", intervention_worthwhile=True,
        recommended_response_class=RESPONSE_MEME, evidence_gaps=(),
        provider="gemini", model="m", latency_ms=1.0, model_calls=1,
        attempts=(("gemini", "m"),), skipped=False)
    assert confirmed.may_confirm is True
    for state in (STATE_NOT_DISTRACTED, STATE_UNCERTAIN, STATE_INTENTIONAL_BREAK):
        other = ReasoningResult(
            state=state, confidence=0.9, severity=4,
            evidence_quality="strong", reason="r", intervention_worthwhile=False,
            recommended_response_class=RESPONSE_NONE, evidence_gaps=(),
            provider="gemini", model="m", latency_ms=1.0, model_calls=1,
            attempts=(("gemini", "m"),), skipped=False)
        assert other.may_confirm is False


def test_uncertain_result_never_confirms():
    result = uncertain_result("no legs configured")
    assert result.state == STATE_UNCERTAIN
    assert result.skipped is True
    assert result.may_confirm is False
    assert result.intervention_worthwhile is False


def test_context_assembler_tolerates_missing_pieces():
    context = build_reasoning_context()
    assert context["goal"] == {"text": None, "topic": None, "project": None}
    assert context["recent_episodes"] == []
    assert context["presence"] == {"state": "unknown", "break_active": False}
    prompt = build_reasoning_prompt(context)
    assert "EVIDENCE:" in prompt
    assert "intervention_worthwhile" in prompt


def test_context_caps_recent_episodes_and_omits_urls():
    recent = [
        {"activity": "a{}".format(i), "category": "distractive",
         "duration_seconds": 60.0, "url": "https://example.com/secret",
         "minutes_ago": float(i)}
        for i in range(10)
    ]
    context = build_reasoning_context(
        goal={"text": "Finish report", "topic": "work", "project": "Q3"},
        current_episode={"activity": "video binge", "category": "distractive",
                         "duration_seconds": 1900.0, "evidence_quality": "strong",
                         "confidence": 0.9},
        recent_episodes=recent,
        alignment={"relation": "misaligned", "goal_relevance": "none", "confidence": 0.85},
        behavior={"distraction_ratio_60m": 0.7, "candidate_score": 0.82,
                  "top_signals": ["sustained_run=0.9"]},
        presence={"state": "active", "break_active": False},
    )
    assert len(context["recent_episodes"]) == 6
    assert "url" not in context["recent_episodes"][0]
    assert "secret" not in build_reasoning_prompt(context)
    assert context["goal"]["text"] == "Finish report"
    assert context["alignment"]["relation"] == "misaligned"
