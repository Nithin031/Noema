"""Meme dataset ingestion for the Meme Center (V3 Phase 10).

Ingestion is idempotent, restartable, non-destructive, and deterministic:
asset identity derives from ``(dataset source, filename)``, so running
ingestion twice over the same corpus changes nothing. Re-ingestion
refreshes dataset-derived columns only — user curation (favorites, tags,
enabled) is preserved by ``upsert_meme_asset``.

The dataset directory itself is never modified and never committed;
thumbnails (when PIL is available) are cached in a separate local
directory, skipped when already fresh.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from noema.domain.meme import (
    ASSET_SENTIMENTS,
    DATASET_SOURCE,
    MemeAsset,
    asset_id_for,
)

#: CSV columns of the sentiment-analysis meme corpus.
CSV_COLUMNS = ("number", "image_name", "text_ocr", "text_corrected",
               "overall_sentiment")

#: Thumbnail cache: deterministic filenames, longest edge capped.
THUMB_MAX_EDGE = 480


def parse_dataset_csv(csv_path: str) -> Tuple[List[Dict[str, str]], List[str]]:
    """Read the dataset CSV. Returns (rows, problems).

    Malformed records are reported in ``problems``, never silently
    discarded. Encoding is expected to be UTF-8 (BOM tolerated).
    """
    problems: List[str] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = list(reader.fieldnames or [])
        # The corpus header leaves the row-number column unnamed
        # (",image_name,..."); normalize it so rows stay addressable.
        if "" in columns:
            reader.fieldnames = [
                "number" if name == "" else name for name in columns]
            columns = list(reader.fieldnames or [])
        missing = [name for name in CSV_COLUMNS if name not in columns]
        if missing:
            problems.append("csv is missing columns: {}".format(missing))
        rows: List[Dict[str, str]] = []
        for index, raw in enumerate(reader, start=2):
            if raw is None:
                problems.append("line {}: unreadable row".format(index))
                continue
            rows.append({key: (raw.get(key) or "") for key in CSV_COLUMNS})
    return rows, problems


def validate_row(row: Mapping[str, str], images_dir: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate one CSV row against the image directory.

    Returns (asset_fields, problem). Exactly one is non-None: invalid
    records are reported, never silently discarded, never ingested.
    """
    filename = str(row.get("image_name") or "").strip()
    if not filename:
        return None, "empty image_name (number={!r})".format(row.get("number"))
    if os.path.basename(filename) != filename:
        return None, "unsafe image_name (path components): {!r}".format(filename)
    path = os.path.join(images_dir, filename)
    if not os.path.isfile(path):
        return None, "missing image file: {}".format(filename)
    sentiment = str(row.get("overall_sentiment") or "").strip().lower() or None
    if sentiment is not None and sentiment not in ASSET_SENTIMENTS:
        return None, "unknown sentiment {!r} for {}".format(
            row.get("overall_sentiment"), filename)
    ocr = str(row.get("text_ocr") or "").strip() or None
    corrected = str(row.get("text_corrected") or "").strip() or None
    try:
        byte_size = os.path.getsize(path)
    except OSError:
        return None, "unreadable image file: {}".format(filename)
    width = height = None
    image_format = None
    try:
        from PIL import Image

        with Image.open(path) as probe:
            probe.verify()
        with Image.open(path) as image:
            width, height = int(image.width), int(image.height)
            image_format = str(image.format or "").strip().lower() or None
    except ImportError:
        pass
    except Exception:
        return None, "corrupt/unreadable image: {}".format(filename)
    return {
        "id": asset_id_for(DATASET_SOURCE, filename),
        "source": DATASET_SOURCE,
        "source_ref": filename,
        "filename": filename,
        "ocr_text": ocr,
        "corrected_text": corrected,
        "sentiment": sentiment,
        "width": width,
        "height": height,
        "byte_size": byte_size,
        "image_format": image_format,
    }, None


def thumbnail_path(thumbs_dir: str, asset_id: str) -> str:
    """Deterministic cached thumbnail location for one asset."""
    return os.path.join(str(thumbs_dir), "{}.jpg".format(asset_id))


def ensure_thumbnail(source_path: str, dest_path: str) -> bool:
    """Generate (or refresh when stale) a JPEG thumbnail. Never raises.

    Returns True when a fresh thumbnail exists afterwards, False when PIL
    is unavailable or the source cannot be read (callers then serve the
    original image instead).
    """
    try:
        if (os.path.isfile(dest_path)
                and os.path.getmtime(dest_path) >= os.path.getmtime(source_path)):
            return True
        from PIL import Image

        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with Image.open(source_path) as image:
            frame = image.convert("RGB")
            frame.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.LANCZOS)
            frame.save(dest_path, "JPEG", quality=82)
        return True
    except ImportError:
        return False
    except Exception:
        return False


def resolve_image_path(images_dir: str, filename: str) -> Optional[str]:
    """Resolve an asset filename inside the dataset dir (traversal-safe).

    Returns the absolute path, or None when the name is unsafe or the
    file does not resolve inside ``images_dir``.
    """
    name = str(filename or "").strip()
    if not name or os.path.basename(name) != name:
        return None
    try:
        base = os.path.realpath(images_dir)
        candidate = os.path.realpath(os.path.join(base, name))
    except (OSError, ValueError):
        return None
    if candidate != base and not candidate.startswith(base + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


def ingest_dataset(csv_path: str, images_dir: str, store: Any,
                   thumbs_dir: Optional[str] = None,
                   make_thumbs: bool = True) -> Dict[str, Any]:
    """Ingest the meme corpus into the asset catalog. Idempotent.

    Returns a statistics dict: csv_rows, images_found, matched, missing,
    invalid, duplicates, sentiment counts, ocr/corrected coverage, assets
    imported (new), assets refreshed (already present), thumbnails made.
    """
    started = datetime.now(timezone.utc)
    rows, problems = parse_dataset_csv(csv_path)
    try:
        disk_files = set(os.listdir(images_dir))
    except OSError:
        disk_files = set()
    stats: Dict[str, Any] = {
        "csv_rows": len(rows),
        "images_found": len(disk_files),
        "matched": 0,
        "missing": 0,
        "invalid": 0,
        "duplicates": 0,
        "imported": 0,
        "refreshed": 0,
        "thumbnails": 0,
        "problems": list(problems),
    }
    sentiment_counts: Dict[str, int] = {}
    ocr_covered = 0
    corrected_covered = 0
    seen_filenames = set()
    for row in rows:
        fields, problem = validate_row(row, images_dir)
        if problem is not None:
            if problem.startswith("missing image file:"):
                stats["missing"] += 1
            else:
                stats["invalid"] += 1
            stats["problems"].append(problem)
            continue
        assert fields is not None
        filename = str(fields["filename"])
        if filename in seen_filenames:
            stats["duplicates"] += 1
            stats["problems"].append("duplicate image_name: {}".format(filename))
            continue
        seen_filenames.add(filename)
        stats["matched"] += 1
        sentiment_counts[fields["sentiment"] or "unknown"] = (
            sentiment_counts.get(fields["sentiment"] or "unknown", 0) + 1)
        if fields["ocr_text"]:
            ocr_covered += 1
        if fields["corrected_text"]:
            corrected_covered += 1
        asset = MemeAsset(
            id=str(fields["id"]), source=str(fields["source"]),
            source_ref=str(fields["source_ref"]), filename=filename,
            ocr_text=fields["ocr_text"], corrected_text=fields["corrected_text"],
            sentiment=fields["sentiment"], width=fields["width"],
            height=fields["height"], byte_size=fields["byte_size"],
            image_format=fields["image_format"],
            created_at=started,
        )
        try:
            inserted = store.upsert_meme_asset(asset)
        except (AttributeError, OSError, TypeError, ValueError):
            stats["problems"].append("persistence failed: {}".format(filename))
            continue
        if inserted:
            stats["imported"] += 1
        else:
            stats["refreshed"] += 1
        if make_thumbs and thumbs_dir:
            source_path = resolve_image_path(images_dir, filename)
            if source_path and ensure_thumbnail(
                    source_path, thumbnail_path(thumbs_dir, asset.id)):
                stats["thumbnails"] += 1
    stats["sentiment_counts"] = sentiment_counts
    stats["ocr_coverage"] = ocr_covered
    stats["corrected_coverage"] = corrected_covered
    stats["problem_count"] = len(stats["problems"])
    stats["problems"] = stats["problems"][:50]
    return stats


def default_thumbs_dir(db_path: Optional[str] = None) -> str:
    """Local thumbnail cache next to the database (never in the repo)."""
    if db_path and str(db_path) not in (":memory:", ""):
        parent = str(Path(str(db_path)).expanduser().parent)
    else:
        parent = os.path.join(os.path.expanduser("~"), ".noema")
    return os.path.join(parent, "meme-thumbs")
