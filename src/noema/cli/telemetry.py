"""Live native-telemetry diagnostic: ``python -m noema telemetry``.

Polls the Noema-native collectors (foreground window, input presence,
browser-bridge health) directly on this machine and prints what they
observe. Needs no ActivityWatch server, no database, and no model
providers. Exit code is 0 when at least one OS collector is supported,
1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence


def build_telemetry_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemetry",
        description="Show live Noema-native telemetry (no ActivityWatch needed)",
    )
    parser.add_argument("--polls", type=int, default=3,
                        help="observation polls to run")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between polls")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="presence AFK timeout in seconds")
    parser.add_argument("--heartbeat", type=float, default=1.0,
                        help="window heartbeat interval in seconds")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    return parser


def _describe_event(event: Any) -> dict:
    metadata = dict(getattr(event, "metadata", None) or {})
    return {
        "timestamp": event.timestamp_iso,
        "duration": round(float(event.duration), 3),
        "app": event.app,
        "title": (event.title[:80] if event.title else None),
        "device": event.device,
        "source": event.source,
        "bucket_id": event.bucket_id,
        "source_event_id": event.source_event_id,
        "metadata": {key: metadata.get(key) for key in (
            "event_kind", "status", "noema_native_source")},
    }


def main_telemetry(argv: Optional[Sequence[str]] = None) -> int:
    args = build_telemetry_parser().parse_args(argv)
    if args.polls < 1:
        print("telemetry: --polls must be positive", file=sys.stderr)
        return 2
    if args.interval < 0 or args.timeout <= 0 or args.heartbeat <= 0:
        print("telemetry: intervals/timeouts must be positive", file=sys.stderr)
        return 2

    from noema.infrastructure.activity_sources import (
        BrowserActivitySource,
        CompositeActivitySource,
        NativeAFKSource,
        NativeWindowsActivitySource,
    )

    collector = CompositeActivitySource([
        NativeWindowsActivitySource(heartbeat_seconds=args.heartbeat),
        NativeAFKSource(timeout_seconds=args.timeout),
        BrowserActivitySource(),
    ])
    snapshots: List[dict] = []
    try:
        for index in range(args.polls):
            now = datetime.now(timezone.utc)
            try:
                poll = collector.poll(now)
            except Exception as exc:  # diagnostic must report, not crash
                snapshots.append({"poll": index + 1, "at": now.isoformat(),
                                  "error": "{}: {}".format(
                                      type(exc).__name__, exc)})
                break
            snapshots.append({
                "poll": index + 1,
                "at": now.isoformat().replace("+00:00", "Z"),
                "status": poll.status,
                "detail": poll.detail,
                "events": [_describe_event(event) for event in poll.events],
            })
            if index + 1 < args.polls:
                time.sleep(max(0.0, args.interval))
    finally:
        collector.close()

    try:
        health = collector.health()
    except Exception:
        health = {"sources": []}
    if args.json:
        print(json.dumps({"health": health, "polls": snapshots},
                         ensure_ascii=False, indent=1, default=str))
    else:
        for source in health.get("sources", []):
            print("{}: {} ({})".format(
                source.get("name"),
                source.get("status"),
                "supported" if source.get("supported") else "unsupported"))
        for snap in snapshots:
            if "error" in snap:
                print("poll {}: ERROR {}".format(snap["poll"], snap["error"]))
                continue
            print("poll {} [{}]: {} event(s)".format(
                snap["poll"], snap["status"], len(snap["events"])))
            for event in snap["events"]:
                meta = event.get("metadata", {})
                if meta.get("event_kind") == "presence":
                    print("  presence={} duration={}s".format(
                        meta.get("status"), event["duration"]))
                else:
                    print("  app={} title={} duration={}s".format(
                        event["app"], event["title"], event["duration"]))
    supported = any(bool(item.get("supported"))
                    for item in health.get("sources", []))
    return 0 if supported else 1
