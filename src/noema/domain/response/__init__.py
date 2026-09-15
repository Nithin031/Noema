"""Curated response library: models, registry seeds, selection."""

from .models import (
    KIND_CHARACTER,
    KIND_CHALLENGE,
    KIND_ENCOURAGEMENT,
    KIND_MEME,
    KIND_NUDGE,
    KIND_STICKER,
    KIND_TEXT,
    MODE_RESPONSE_CLASSES,
    RESPONSE_KINDS,
    RESPONSE_TONES,
    TONE_FIRM,
    TONE_GENTLE,
    TONE_PLAYFUL,
    TONE_SARCASTIC,
    TONE_SUPPORTIVE,
    Response,
)
from .seeds import seed_responses
from .selector import Selection, select_response

__all__ = [
    "KIND_CHARACTER",
    "KIND_CHALLENGE",
    "KIND_ENCOURAGEMENT",
    "KIND_MEME",
    "KIND_NUDGE",
    "KIND_STICKER",
    "KIND_TEXT",
    "MODE_RESPONSE_CLASSES",
    "RESPONSE_KINDS",
    "RESPONSE_TONES",
    "TONE_FIRM",
    "TONE_GENTLE",
    "TONE_PLAYFUL",
    "TONE_SARCASTIC",
    "TONE_SUPPORTIVE",
    "Response",
    "Selection",
    "seed_responses",
    "select_response",
]
