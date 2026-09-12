"""Reset old results and rerun inference in chunks (Google Gemini only).

Flow (raw telemetry is never touched):
1. Timestamped backup of the SQLite database.
2. Wipe derived results: semantic_classifications, classification_batches,
   behavior_observations, meaningful_sessions.
3. Rebuild meaningful episodes from raw sessions with the current
   continuity engine (evidence-quality merge bonus included).
4. Batch-classify every meaningful session through Google Gemini models
   only, in slices of --batch-size. Each slice goes through
   Classifier.classify_many -> ProviderChain.classify_batch, which packs
   sessions into token-budget groups (chunk processing, never row-by-row).
   Failed sessions stay pending/failed; nothing is fabricated.
5. Re-evaluate behavior observations from the fresh verdicts.

Usage:
    python rerun_inference.py --dry-run
    python rerun_inference.py --yes
    python rerun_inference.py --yes --batch-size 20 --limit 5
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from noema.config.settings import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from noema.infrastructure.activity_sources.activitywatch.adapter import (
    ActivityWatchAdapter,
)
from noema.infrastructure.database.store import SQLiteStore
from noema.infrastructure.providers import GeminiProvider, ProviderChain
from noema.application.classification import Classifier
from noema.application.pipeline import NoemaService

WIPE_TABLES = (
    "semantic_classifications",
    "classification_batches",
    "behavior_observations",
    "meaningful_sessions",
)


def table_counts(db_path):
    conn = sqlite3.connect(db_path)
    counts = {}
    for table in WIPE_TABLES + ("activity_sessions", "normalized_events"):
        try:
            counts[table] = conn.execute(
                "SELECT COUNT(*) FROM {}".format(table)
            ).fetchone()[0]
        except sqlite3.OperationalError:
            counts[table] = -1
    conn.close()
    return counts


def build_context(store, sessions):
    """Rich per-session evidence from raw sessions (daemon parity)."""
    if not sessions:
        return {}
    start = min(item.start_time for item in sessions)
    end = max(item.end_time for item in sessions)
    raw_by_id = {
        item.id: item
        for item in store.query_sessions(start=start, end=end, limit=100000)
    }
    context = {}
    for session in sessions:
        context[session.id] = [
            {
                "application": raw_by_id[item_id].app,
                "title": raw_by_id[item_id].title,
                "domain": raw_by_id[item_id].domain,
                "url": raw_by_id[item_id].url,
                "duration_seconds": raw_by_id[item_id].duration,
                "device": raw_by_id[item_id].device,
                "browser": raw_by_id[item_id].browser,
                "browser_window_id": raw_by_id[item_id].browser_window_id,
                "browser_tab_id": raw_by_id[item_id].browser_tab_id,
                "event_count": raw_by_id[item_id].event_count,
            }
            for item_id in session.activity_session_ids
            if item_id in raw_by_id
        ]
    return context


def main():
    parser = argparse.ArgumentParser(
        description="Reset results and rerun chunked Google-only inference"
    )
    parser.add_argument("--yes", action="store_true",
                        help="Actually wipe and rerun (required)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show counts only, change nothing")
    parser.add_argument("--limit", type=int, default=0,
                        help="Newest N sessions only (0 = all)")
    parser.add_argument("--batch-size", type=int, default=20,
                        help="Sessions per classify_many slice")
    parser.add_argument("--version", default="1",
                        help="Classifier version stamp for fresh rows")
    parser.add_argument("--sleep", type=float, default=3.0,
                        help="Seconds between slices")
    args = parser.parse_args()

    db_path = os.path.expandvars(r"%LOCALAPPDATA%\Noema\noema.sqlite3")
    if not os.path.exists(db_path):
        print("ERROR: database not found at {}".format(db_path), flush=True)
        sys.exit(1)

    print("Database: {}".format(db_path), flush=True)
    before = table_counts(db_path)
    print("Before: {}".format(before), flush=True)
    if args.dry_run:
        print("[DRY RUN] nothing changed", flush=True)
        return
    if not args.yes:
        print("Refusing to wipe without --yes (use --dry-run to preview)",
              flush=True)
        sys.exit(2)

    # 1. Backup.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = "{}.bak-{}".format(db_path, stamp)
    shutil.copy2(db_path, backup)
    print("Backup: {}".format(backup), flush=True)

    store = SQLiteStore(db_path)

    # 2. Wipe derived results (raw telemetry untouched).
    conn = sqlite3.connect(db_path)
    for table in WIPE_TABLES:
        conn.execute("DELETE FROM {}".format(table))
    conn.commit()
    conn.close()
    print("Wiped: {}".format(list(WIPE_TABLES)), flush=True)

    # 3. Rebuild episodes from raw sessions with the current engine.
    service_bootstrap = NoemaService(ActivityWatchAdapter(), store)
    rebuilt = service_bootstrap.build_meaningful_sessions(limit=100000)
    print("Rebuilt {} meaningful sessions from raw".format(len(rebuilt)),
          flush=True)
    sessions = store.query_meaningful_sessions(limit=100000)
    print("Stored meaningful sessions: {}".format(len(sessions)), flush=True)
    if args.limit > 0:
        sessions = sorted(sessions, key=lambda item: item.start_time)[-args.limit:]
        print("Limited to newest {} sessions".format(len(sessions)), flush=True)
    if not sessions:
        print("No sessions to classify", flush=True)
        return

    # 4. Google-only chain -> chunked batch classification.
    chain = ProviderChain(
        hosted=[GeminiProvider(model=model)
                for model in ProviderChain.HOSTED_MODELS],
        ollama=None,
        include_ollama=False,
        openrouter=[],
    )
    service = NoemaService(
        ActivityWatchAdapter(), store, classifier=Classifier(provider=chain)
    )
    context = build_context(store, sessions)
    covered = sum(1 for items in context.values() if items)
    print("Sessions with raw evidence context: {}/{}".format(covered, len(sessions)),
          flush=True)

    classified_total = failed_total = 0
    batch_size = max(1, int(args.batch_size))
    slices = [sessions[index:index + batch_size]
              for index in range(0, len(sessions), batch_size)]
    for number, sliver in enumerate(slices, 1):
        print("[{}/{}] classifying {} sessions...".format(
            number, len(slices), len(sliver)), flush=True)
        try:
            results = service.classify_meaningful_sessions(
                sliver,
                persist=True,
                context_by_session={item.id: context.get(item.id, [])
                                    for item in sliver},
                classifier_version=args.version,
                max_retries=5,
            )
        except Exception as exc:
            print("  slice error (continuing): {}".format(exc), flush=True)
            failed_total += len(sliver)
            continue
        ok = sum(1 for item in results
                 if item.classification_status == "classified")
        bad = len(results) - ok
        classified_total += ok
        failed_total += bad
        print("  classified={} pending/failed={}".format(ok, bad), flush=True)
        if number < len(slices) and args.sleep > 0:
            time.sleep(args.sleep)

    # 5. Fresh behavior observations from fresh verdicts.
    observations = service.evaluate_stored_behavior(limit=100000)
    print("Behavior observations: {}".format(len(observations)), flush=True)

    after = table_counts(db_path)
    print("After: {}".format(after), flush=True)
    print("Done! classified={} pending/failed={}".format(
        classified_total, failed_total), flush=True)


if __name__ == "__main__":
    main()
