"""Meme ranking + copy: bounded calls, strict validation, safe fallback."""

from noema.application.meme_decision import (
    COPY_MAX_CHARS,
    MEME_CANDIDATE_LIMIT,
    MemeDecider,
    retrieve_meme_candidates,
)
from noema.infrastructure.providers import FailureKind


class FakeLeg:
    def __init__(self, name="gemini", model="gemini-3.5-flash",
                 payload=None, error=None):
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


def _candidates():
    return [
        {"id": "a1", "filename": "a1.jpg", "ocr_snippet": "back to work",
         "sentiment": "neutral", "tags": [], "favorite": False,
         "score": 0.9, "effectiveness": None},
        {"id": "b2", "filename": "b2.jpg", "ocr_snippet": "one more video",
         "sentiment": "positive", "tags": [], "favorite": False,
         "score": 0.7, "effectiveness": None},
        {"id": "c3", "filename": "c3.jpg", "ocr_snippet": "deadlines",
         "sentiment": "negative", "tags": [], "favorite": False,
         "score": 0.5, "effectiveness": None},
    ]


def _ranking_payload(**overrides):
    base = {
        "selected_asset_id": "b2",
        "confidence": 0.89,
        "reason": "caught-procrastinating match without aggression",
        "alternatives": ["a1", "c3"],
    }
    base.update(overrides)
    return base


def test_rank_selects_validated_id_in_one_call():
    decider = MemeDecider(legs=[FakeLeg(payload=_ranking_payload())])
    ranking = decider.rank(_candidates(), goal="Study", severity=3,
                           episode_id="ep-1")
    assert ranking.selected_asset_id == "b2"
    assert ranking.confidence == 0.89
    assert ranking.alternatives == ("a1", "c3")
    assert ranking.failure_kind is None
    assert sum(leg.calls for leg in decider.legs) == 1


def test_rank_rejects_unknown_id_to_deterministic_top():
    decider = MemeDecider(legs=[FakeLeg(payload=_ranking_payload(
        selected_asset_id="zzz", alternatives=["zzz", "nope", "a1"]))])
    ranking = decider.rank(_candidates(), goal="Study")
    assert ranking.selected_asset_id == "a1"
    assert ranking.alternatives == ("b2", "c3")
    assert ranking.failure_kind == "INVALID_OUTPUT"
    assert ranking.confidence == 0.0


def test_rank_failure_falls_back_without_loop():
    from noema.infrastructure.providers import ProviderError

    legs = [FakeLeg(error=ProviderError("HTTP 429 quota")),
            FakeLeg(error=TimeoutError("timed out"))]
    decider = MemeDecider(legs=legs)
    ranking = decider.rank(_candidates(), goal="Study")
    assert ranking.selected_asset_id == "a1"
    assert ranking.failure_kind in {"RATE_LIMITED", "TIMEOUT",
                                    "PROVIDER_ERROR", "INVALID_OUTPUT"}
    assert sum(leg.calls for leg in legs) == 2


def test_rank_no_candidates_is_unavailable():
    ranking = MemeDecider(legs=[FakeLeg(payload=_ranking_payload())]).rank([])
    assert ranking.selected_asset_id is None
    assert ranking.failure_kind == "UNAVAILABLE"


def test_rank_no_legs_falls_back_deterministically():
    ranking = MemeDecider(legs=[]).rank(_candidates())
    assert ranking.selected_asset_id == "a1"
    assert ranking.failure_kind == "UNAVAILABLE"
    assert ranking.confidence == 0.0


def test_rank_cache_converges_and_respects_inputs():
    leg = FakeLeg(payload=_ranking_payload())
    decider = MemeDecider(legs=[leg])
    first = decider.rank(_candidates(), goal="Study", severity=3,
                         episode_id="ep-1")
    second = decider.rank(_candidates(), goal="Study", severity=3,
                          episode_id="ep-1")
    assert leg.calls == 1
    assert second is first
    # Changed goal / severity / episode / candidates all bypass the cache.
    decider.rank(_candidates(), goal="Other", severity=3, episode_id="ep-1")
    decider.rank(_candidates(), goal="Study", severity=4, episode_id="ep-1")
    decider.rank(_candidates(), goal="Study", severity=3, episode_id="ep-2")
    decider.rank(_candidates()[:2], goal="Study", severity=3, episode_id="ep-1")
    assert leg.calls == 5


def test_copy_generation_constrained():
    decider = MemeDecider(legs=[FakeLeg(payload={"copy": "Back to control systems."})])
    copy = decider.generate_copy(goal="Study control systems",
                                 activity="watching videos",
                                 minutes_away=28, tone="playful")
    assert copy == "Back to control systems."
    assert decider.legs[0].calls == 1


def test_copy_rejects_urls_length_and_garbage():
    long_copy = {"copy": "x" * (COPY_MAX_CHARS + 1)}
    url_copy = {"copy": "See http://evil.example/x for focus tips"}
    decider = MemeDecider(legs=[
        FakeLeg(payload=long_copy),
        FakeLeg(payload=url_copy),
        FakeLeg(payload={"copy": "   "}),
        FakeLeg(payload=["not", "a", "mapping"]),
    ])
    assert decider.generate_copy(goal="g") is None
    assert decider.generate_copy(goal="g") is None
    assert decider.generate_copy(goal="g") is None
    assert decider.generate_copy(goal="g") is None


def test_copy_failure_returns_none_not_fabrication():
    from noema.infrastructure.providers import ProviderError

    decider = MemeDecider(legs=[FakeLeg(error=ProviderError("boom"))])
    assert decider.generate_copy(goal="Study") is None
    assert MemeDecider(legs=[]).generate_copy(goal="Study") is None


def test_candidate_limit_constant_sane():
    assert 10 <= MEME_CANDIDATE_LIMIT <= 30
