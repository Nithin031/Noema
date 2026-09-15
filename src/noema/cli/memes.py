"""Meme Center dataset tools: ``python -m noema memes ...``.

Indexes a local meme corpus (CSV + image directory) into the MemeAsset
catalog. The dataset directory is only read; assets, thumbnails, and all
derived state stay outside the repository. Exit code is 0 on success,
1 when nothing was imported, 2 on usage errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence


def build_memes_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memes",
        description="Meme Center dataset ingestion (local corpus, idempotent)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="index a CSV + image directory")
    ingest.add_argument("--csv", required=True, help="path to labels.csv")
    ingest.add_argument("--dir", required=True, help="directory with image files")
    ingest.add_argument("--db", default="",
                        help="database path (empty = default local database)")
    ingest.add_argument("--thumbs-dir", default="",
                        help="thumbnail cache directory (empty = next to database)")
    ingest.add_argument("--no-thumbs", action="store_true",
                        help="skip thumbnail generation (serve originals)")
    ingest.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    stats = sub.add_parser("stats", help="show Meme Center catalog totals")
    stats.add_argument("--db", default="",
                       help="database path (empty = default local database)")
    stats.add_argument("--json", action="store_true",
                       help="emit machine-readable JSON")
    return parser


def _open_store(db_path: str):
    from noema.config.settings import DaemonConfig
    from noema.infrastructure.database import SQLiteStore

    if db_path.strip():
        return SQLiteStore(os.path.expanduser(db_path.strip()))
    return SQLiteStore(DaemonConfig().db_path)


def main_memes(argv: Optional[Sequence[str]] = None) -> int:
    args = build_memes_parser().parse_args(argv)
    if args.command == "stats":
        from noema.infrastructure.database import SQLiteStore

        store = _open_store(args.db)
        try:
            totals = store.meme_asset_stats()
        finally:
            store.close()
        if args.json:
            print(json.dumps(totals, ensure_ascii=False, indent=1, default=str))
        else:
            print("meme assets: {}".format(totals.get("assets", 0)))
            print("favorites: {}".format(totals.get("favorites", 0)))
            print("curated assets: {}".format(totals.get("curated_assets", 0)))
            print("active responses: {}".format(totals.get("active_responses", 0)))
            for sentiment, count in sorted((totals.get("by_sentiment") or {}).items()):
                print("sentiment {}: {}".format(sentiment, count))
        return 0
    # ingest
    from noema.api import NoemaService
    from noema.application.meme_assets import default_thumbs_dir
    from noema.infrastructure.activity_sources.activitywatch import ActivityWatchAdapter
    from noema.infrastructure.database import SQLiteStore

    csv_path = os.path.expanduser(args.csv)
    images_dir = os.path.expanduser(args.dir)
    if not os.path.isfile(csv_path):
        print("memes: csv not found: {}".format(args.csv), file=sys.stderr)
        return 2
    if not os.path.isdir(images_dir):
        print("memes: image directory not found: {}".format(args.dir), file=sys.stderr)
        return 2
    store = _open_store(args.db)
    try:
        service = NoemaService(ActivityWatchAdapter(), store)
        thumbs = (args.thumbs_dir.strip() or default_thumbs_dir(
            getattr(store, "path", None)))
        stats = service.ingest_meme_dataset(
            csv_path, images_dir,
            thumbs_dir=thumbs if not args.no_thumbs else None,
            make_thumbs=not args.no_thumbs)
    finally:
        store.close()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1, default=str))
    else:
        print("csv rows: {}".format(stats.get("csv_rows", 0)))
        print("images found: {}".format(stats.get("images_found", 0)))
        print("matched: {}".format(stats.get("matched", 0)))
        print("missing: {}".format(stats.get("missing", 0)))
        print("invalid: {}".format(stats.get("invalid", 0)))
        print("duplicates: {}".format(stats.get("duplicates", 0)))
        print("imported: {}".format(stats.get("imported", 0)))
        print("refreshed: {}".format(stats.get("refreshed", 0)))
        print("thumbnails: {}".format(stats.get("thumbnails", 0)))
        print("ocr coverage: {}".format(stats.get("ocr_coverage", 0)))
        print("corrected coverage: {}".format(stats.get("corrected_coverage", 0)))
        for problem in (stats.get("problems") or [])[:10]:
            print("note: {}".format(problem))
        if stats.get("problem_count", 0) > 10:
            print("... and {} more".format(stats["problem_count"] - 10))
    if stats.get("csv_rows", 0) and not stats.get("imported") and not stats.get("refreshed"):
        return 1
    return 0
