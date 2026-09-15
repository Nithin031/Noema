"""Meme asset catalog for the Meme Center (V3 Phase 10).

Conceptual rule: ASSET ≠ RESPONSE. A MemeAsset is a curated image from a
local dataset (metadata + provenance, no behavioral meaning). A Response
(domain/response) is an intervention that *uses* an asset via
``Response.asset_id``. One asset may back zero, one, or many responses.

Dataset sentiment labels are preserved verbatim as *dataset metadata*.
They are never interpreted as intervention suitability, tone, or quality.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from noema.domain.activity import coerce_timestamp

#: Provenance constant for the bundled sentiment-analysis meme corpus.
#: Individual image rights are unknown; assets are local-use only and are
#: never committed to the repository or redistributed by Noema.
DATASET_SOURCE = "meme-dataset-v1"
DATASET_LICENSE_NOTE = (
    "Dataset distribution listed as GPL-2.0; individual image rights "
    "unknown. Local use only — do not redistribute."
)

#: Sentiment vocabulary taken verbatim from the dataset CSV. ``None``
#: means the row carried no usable label (preserved as unknown, never
#: defaulted to neutral — neutral is itself a real label here).
ASSET_SENTIMENTS = frozenset({
    "very_positive",
    "positive",
    "neutral",
    "negative",
    "very_negative",
})


def asset_id_for(source: str, filename: str) -> str:
    """Stable, deterministic asset id from dataset + filename.

    Re-running ingestion over the same corpus yields identical ids, so
    ingestion is idempotent and responses keep pointing at the same
    assets. A new dataset version changes ``source`` and therefore ids.
    """
    digest = hashlib.sha256(
        (source + chr(0) + filename).encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass(frozen=True)
class MemeAsset:
    """One catalogued meme image (metadata only; bytes stay on disk)."""

    id: str
    source: str = DATASET_SOURCE
    source_ref: str = ""
    filename: str = ""
    ocr_text: Optional[str] = None
    corrected_text: Optional[str] = None
    sentiment: Optional[str] = None
    tags: Tuple[str, ...] = ()
    favorite: bool = False
    enabled: bool = True
    width: Optional[int] = None
    height: Optional[int] = None
    byte_size: Optional[int] = None
    image_format: Optional[str] = None
    created_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id or "").strip())
        if not self.id:
            raise ValueError("asset id cannot be empty")
        object.__setattr__(self, "source", str(self.source or "").strip() or DATASET_SOURCE)
        object.__setattr__(self, "source_ref", str(self.source_ref or "").strip())
        object.__setattr__(self, "filename", str(self.filename or "").strip())
        for field_name in ("ocr_text", "corrected_text"):
            value = getattr(self, field_name)
            object.__setattr__(
                self, field_name, str(value).strip() or None if value is not None else None)
        sentiment = getattr(self, "sentiment")
        if sentiment is not None:
            sentiment = str(sentiment).strip().lower() or None
            if sentiment is not None and sentiment not in ASSET_SENTIMENTS:
                raise ValueError("unknown asset sentiment: {}".format(sentiment))
            object.__setattr__(self, "sentiment", sentiment)
        object.__setattr__(
            self, "tags",
            tuple(dict.fromkeys(str(tag).strip().lower()
                                for tag in (self.tags or ())
                                if str(tag).strip())))
        object.__setattr__(self, "favorite", bool(self.favorite))
        object.__setattr__(self, "enabled", bool(self.enabled))
        for field_name in ("width", "height", "byte_size"):
            value = getattr(self, field_name)
            if value is None:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ValueError("{} must be an integer".format(field_name))
            if number < 0:
                raise ValueError("{} cannot be negative".format(field_name))
            object.__setattr__(self, field_name, number)
        image_format = getattr(self, "image_format")
        object.__setattr__(
            self, "image_format",
            str(image_format).strip().lower() or None if image_format is not None else None)
        created_at = getattr(self, "created_at")
        object.__setattr__(
            self, "created_at",
            coerce_timestamp(created_at) if created_at is not None else None)

    @property
    def display_text(self) -> Optional[str]:
        """Best available human text: corrected OCR, else raw OCR, else None."""
        return self.corrected_text or self.ocr_text

    def to_dict(self, response_count: int = 0) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "source_ref": self.source_ref,
            "filename": self.filename,
            "ocr_text": self.ocr_text,
            "corrected_text": self.corrected_text,
            "display_text": self.display_text,
            "sentiment": self.sentiment,
            "tags": list(self.tags),
            "favorite": self.favorite,
            "enabled": self.enabled,
            "width": self.width,
            "height": self.height,
            "byte_size": self.byte_size,
            "image_format": self.image_format,
            "created_at": self.created_at.isoformat().replace("+00:00", "Z") if self.created_at else None,
            "response_count": int(response_count),
            "curated": bool(response_count),
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "MemeAsset":
        import json as _json

        try:
            tags = _json.loads(row["tags_json"] or "[]")
        except (TypeError, ValueError, KeyError):
            tags = []
        created_at = row.get("created_at") if isinstance(row, dict) else row["created_at"]
        return cls(
            id=str(row["id"]),
            source=str(row.get("source") or DATASET_SOURCE),
            source_ref=str(row.get("source_ref") or ""),
            filename=str(row.get("filename") or ""),
            ocr_text=row.get("ocr_text"),
            corrected_text=row.get("corrected_text"),
            sentiment=row.get("sentiment"),
            tags=tuple(tags) if isinstance(tags, list) else (),
            favorite=bool(row.get("favorite", False)),
            enabled=bool(row.get("enabled", True)),
            width=row.get("width"),
            height=row.get("height"),
            byte_size=row.get("byte_size"),
            image_format=row.get("image_format"),
            created_at=created_at,
        )
