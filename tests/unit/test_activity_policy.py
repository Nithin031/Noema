"""Activity policy, evidence, and prompt-contract tests (spec 1-21, 47-48)."""

from noema.application.classification import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Classification,
    Classifier,
)
from noema.domain.meaningful.models import MeaningfulSession
from noema.domain.policy import assess_policy
from noema.infrastructure.providers import BATCH_INSTRUCTIONS


def make_episode(app=None, title=None, domain=None, minutes=5):
    return MeaningfulSession(
        start_time="2026-09-06T10:00:00Z",
        end_time="2026-09-06T10:{:02d}:00Z".format(minutes),
        device_set=("laptop",),
        activity_session_ids=("raw-1",),
        active_duration_seconds=float(minutes * 60),
    )


def evidence_for(app=None, title=None, domain=None, minutes=5):
    context = [{
        "application": app,
        "window_title": title,
        "domain": domain,
        "duration_seconds": minutes * 60,
    }]
    return Classifier.build_evidence(make_episode(minutes=minutes), context)


def test_browser_itself_is_not_distractive():
    prior = assess_policy("firefox.exe", "Mozilla Firefox", None, None, 3.0)
    assert prior.decision != "distractive"
    assert prior.decision == "container-neutral"


def test_firefox_generic_title_produces_neutral_low_evidence():
    prior = assess_policy("firefox.exe", "Mozilla Firefox", None, None, 3.0)
    assert prior.decision == "container-neutral"
    assert "container" in prior.reason


def test_youtube_technical_title_can_be_productive_learning():
    prior = assess_policy(
        "firefox.exe", "YouTube - PPO Tutorial", "youtube.com", None, 900.0)
    assert prior.decision == "inspect"
    assert prior.matched_term == "youtube"
    assert "technical" in SYSTEM_PROMPT and "learning" in SYSTEM_PROMPT


def test_youtube_entertainment_title_can_be_distractive():
    prior = assess_policy(
        "firefox.exe", "YouTube - funny videos compilation", None, None, 600.0)
    assert prior.decision == "inspect"
    assert "entertainment" in SYSTEM_PROMPT


def test_youtube_generic_title_remains_uncertain():
    prior = assess_policy("firefox.exe", "YouTube", None, None, 30.0)
    assert prior.decision == "inspect"


def test_whatsapp_short_episode_is_not_automatically_distractive():
    prior = assess_policy("firefox.exe", "WhatsApp Web", None, None, 240.0)
    assert prior.decision == "inspect"
    assert prior.decision != "distractive"


def test_whatsapp_long_continuous_episode_is_strong_distractive_evidence():
    prior = assess_policy("firefox.exe", "WhatsApp Web", None, None, 840.0)
    assert prior.decision == "distractive"
    assert "10 minutes" in prior.reason
    assert prior.continuous_seconds == 840.0


def test_whatsapp_duration_is_continuous_not_daily_sum():
    short = assess_policy("firefox.exe", "WhatsApp", None, None, 240.0)
    other_short = assess_policy("firefox.exe", "WhatsApp", None, None, 300.0)
    assert short.decision == "inspect"
    assert other_short.decision == "inspect"
    # Each call sees only its own episode duration; there is no accumulator.
    combined = assess_policy("firefox.exe", "WhatsApp", None, None, 540.0)
    assert combined.decision == "inspect"


def test_instagram_is_distractive_by_default():
    prior = assess_policy("Instagram", "Instagram", "instagram.com", None, 20.0)
    assert prior.decision == "distractive"


def test_netflix_is_distractive_by_default():
    prior = assess_policy("firefox.exe", "Netflix - Friends", "netflix.com", None, 900.0)
    assert prior.decision == "distractive"


def test_reddit_is_distractive_by_default():
    prior = assess_policy("firefox.exe", "Reddit front page", "reddit.com", None, 300.0)
    assert prior.decision == "distractive"


def test_known_game_gameplay_is_distractive():
    prior = assess_policy("RocketLeague.exe", "Rocket League", None, None, 1200.0)
    assert prior.decision == "distractive"


def test_rocket_league_gameplay_is_distractive():
    prior = assess_policy("chrome.exe", "Rocket League", None, None, 600.0)
    assert prior.decision == "distractive"


def test_chess_gameplay_is_distractive():
    prior = assess_policy("chrome.exe", "Chess - Chess.com", "chess.com", None, 600.0)
    assert prior.decision == "distractive"


def test_rocket_league_bot_research_is_not_gameplay():
    prior = assess_policy(
        "chrome.exe", "Rocket League bot development", None, None, 900.0)
    assert prior.decision == "inspect"


def test_chess_engine_implementation_is_not_gameplay():
    prior = assess_policy(
        "chrome.exe", "Chess engine implementation", "github.com", None, 900.0)
    assert prior.decision == "inspect"


def test_browser_title_is_preserved_as_semantic_evidence():
    payload = evidence_for(
        "firefox.exe", "YouTube - Deep Reinforcement Learning Lecture",
        "youtube.com")
    assert payload["primary_title"] == "YouTube - Deep Reinforcement Learning Lecture"
    assert payload["primary_app"] == "firefox.exe"


def test_firefox_executable_alone_cannot_make_activity_distractive():
    prior = assess_policy("firefox.exe", None, None, None, 60.0)
    assert prior.decision != "distractive"


def test_domain_and_url_are_never_fabricated():
    payload = evidence_for("firefox.exe", "Mozilla Firefox", None)
    assert payload["primary_domain"] is None
    assert payload["observed_domains"] == []
    assert payload["observed_urls"] == []


def test_generic_browser_title_has_low_evidence():
    assert "cap confidence at 0.59" in SYSTEM_PROMPT
    payload = evidence_for("firefox.exe", "Mozilla Firefox", None)
    assert payload["policy_prior"]["decision"] == "container-neutral"


def test_batch_prompt_applies_thin_evidence_confidence_rule():
    assert "0.0-0.59" in BATCH_INSTRUCTIONS
    assert "Never label thin evidence as distractive" in BATCH_INSTRUCTIONS
    assert "0.0-0.59" in SYSTEM_PROMPT


def test_evidence_builder_emits_policy_prior():
    payload = evidence_for("firefox.exe", "WhatsApp Web", None, minutes=14)
    assert payload["policy_prior"]["decision"] == "distractive"
    payload = evidence_for("firefox.exe", "Mozilla Firefox", None)
    assert payload["policy_prior"]["decision"] == "container-neutral"
    payload = evidence_for("notepad.exe", "meeting notes", None)
    assert payload["policy_prior"]["decision"] == "none"


def test_prompt_version_is_stored():
    assert PROMPT_VERSION == "2"
    item = Classification(session_id="s1")
    assert item.prompt_version == "1"
    assert item.to_dict()["prompt_version"] == "1"


def test_classifier_stamps_prompt_version():
    from noema.infrastructure.providers import ProviderError

    class Exploding:
        name = "gemini"
        model = "gemini-3.5-flash-lite"

        def classify(self, session, prompt):
            raise ProviderError("offline")

    classifier = Classifier(provider=Exploding())
    result = classifier.classify(make_episode())
    assert result.classification_status == "pending"
    assert result.prompt_version == PROMPT_VERSION
