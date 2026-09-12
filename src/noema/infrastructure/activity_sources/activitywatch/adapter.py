"""Read-only compatibility adapter for the collector REST protocol."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Dict, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from noema.domain.activity import ActivityEvent, coerce_timestamp


AFK_BUCKET_TYPES = frozenset({"afkstatus", "afk", "afkwatcher"})
WINDOW_BUCKET_TYPES = frozenset({"currentwindow", "current_window"})

KNOWN_FIELDS = {
    "app",
    "application",
    "title",
    "window_title",
    "url",
    "href",
    "domain",
    "device",
    "timestamp",
    "duration",
    "browser",
    "browser_name",
    "browser_window_id",
    "browserWindowId",
    "window_id",
    "windowId",
    "browser_tab_id",
    "browserTabId",
    "tab_id",
    "tabId",
}


def _first_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _domain_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    if not hostname:
        return None
    hostname = hostname.lower().rstrip(".")
    return hostname[4:] if hostname.startswith("www.") else hostname


class ActivityWatchClient:
    """Client for the supported ActivityWatch-compatible REST protocol."""

    def __init__(self, base_url: str = "http://127.0.0.1:5600", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        query = ""
        if params:
            query = "?" + urlencode(
                [(key, value) for key, value in params.items() if value is not None]
            )
        request = Request(
            self.base_url + "/" + path.lstrip("/") + query,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            raise ConnectionError("could not read collector at {}".format(self.base_url)) from exc

    def list_buckets(self) -> Any:
        return self._get_json("api/0/buckets/")

    def get_bucket(self, bucket_id: str) -> Any:
        return self._get_json("api/0/buckets/{}".format(quote(bucket_id, safe="")))

    def get_events(
        self,
        bucket_id: str,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
    ) -> Any:
        params: Dict[str, Any] = {}
        if start is not None:
            params["starttime"] = coerce_timestamp(start).isoformat()
        if end is not None:
            params["endtime"] = coerce_timestamp(end).isoformat()
        return self._get_json(
            "api/0/buckets/{}/events".format(quote(bucket_id, safe="")), params
        )

    def query(self, query_lines: Sequence[str], start: Any, end: Any) -> Any:
        """Run an ActivityWatch query language request."""
        timeperiod = "{}/{}".format(
            coerce_timestamp(start).isoformat(),
            coerce_timestamp(end).isoformat(),
        )
        body = json.dumps({
            "timeperiods": [timeperiod],
            "query": ["\n".join(query_lines)],
        }).encode("utf-8")
        request = Request(
            self.base_url + "/api/0/query/",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            raise ConnectionError("could not query collector at {}".format(self.base_url)) from exc


def source_event_kind(bucket: Optional[Mapping[str, Any]], bucket_id: Optional[str]) -> str:
    """Map ActivityWatch bucket metadata onto Noema event kinds."""
    payload = bucket or {}
    bucket_type = str(payload.get("type") or "").casefold()
    identity = str(bucket_id or payload.get("id") or payload.get("name") or "")
    if identity.startswith("aw-watcher-afk") or bucket_type in AFK_BUCKET_TYPES:
        return "presence"
    if bucket_type in WINDOW_BUCKET_TYPES:
        return "window"
    return "activity"


class ActivityWatchAdapter:
    """Translate ActivityWatch-compatible bucket/event payloads."""

    def __init__(
        self,
        client: Optional[ActivityWatchClient] = None,
        device: Optional[str] = None,
    ):
        self.client = client
        self.device = device

    def normalize_event(
        self,
        event: Mapping[str, Any],
        bucket: Optional[Mapping[str, Any]] = None,
        device: Optional[str] = None,
        bucket_id: Optional[str] = None,
    ) -> ActivityEvent:
        if not isinstance(event, Mapping):
            raise TypeError("collector events must be mappings")
        bucket = bucket or {}
        data = event.get("data")
        if not isinstance(data, Mapping):
            data = event

        url = _first_value(data, "url", "href")
        bucket_hostname = _first_value(bucket, "hostname", "device")
        selected_device = _first_value(data, "device") or bucket_hostname or device or self.device
        selected_bucket_id = bucket_id or _first_value(bucket, "id", "name")
        source_event_id = _first_value(event, "id", "_id")
        browser = _first_value(data, "browser", "browser_name")
        if not browser:
            app_name = str(_first_value(data, "app", "application") or "").casefold()
            known_browsers = {
                "firefox": "firefox", "mozilla firefox": "firefox",
                "chrome": "chrome", "google chrome": "chrome",
                "edge": "edge", "microsoft edge": "edge",
                "safari": "safari", "brave": "brave", "opera": "opera",
            }
            browser = known_browsers.get(app_name)

        metadata = {
            str(key): value
            for key, value in data.items()
            if key not in KNOWN_FIELDS
        }
        if bucket.get("type") is not None:
            metadata["activitywatch_bucket_type"] = bucket.get("type")
        if bucket.get("client") is not None:
            metadata["activitywatch_client"] = bucket.get("client")
        metadata["event_kind"] = source_event_kind(bucket, selected_bucket_id)

        return ActivityEvent(
            timestamp=_first_value(event, "timestamp", "time"),
            duration=_first_value(event, "duration", "length") or 0,
            device=selected_device or "unknown",
            app=_first_value(data, "app", "application"),
            title=_first_value(data, "title", "window_title"),
            domain=_first_value(data, "domain") or _domain_from_url(str(url) if url else None),
            url=str(url) if url is not None else None,
            source="activitywatch",
            bucket_id=str(selected_bucket_id) if selected_bucket_id is not None else None,
            source_event_id=str(source_event_id) if source_event_id is not None else None,
            metadata=metadata,
            browser=str(browser) if browser is not None else None,
            browser_window_id=_first_value(data, "browser_window_id", "browserWindowId", "window_id", "windowId"),
            browser_tab_id=_first_value(data, "browser_tab_id", "browserTabId", "tab_id", "tabId"),
        )

    def iter_bucket(
        self,
        bucket: Mapping[str, Any],
        device: Optional[str] = None,
    ) -> Iterator[ActivityEvent]:
        if not isinstance(bucket, Mapping):
            raise TypeError("collector buckets must be mappings")
        events = bucket.get("events", [])
        if isinstance(events, Mapping):
            events = events.values()
        for event in events:
            yield self.normalize_event(event, bucket=bucket, device=device)

    def iter_buckets(self, buckets: Any) -> Iterator[ActivityEvent]:
        """Accept a bucket object, a list, or the dict returned by the API."""

        if isinstance(buckets, Mapping):
            if "events" in buckets:
                yield from self.iter_bucket(buckets)
                return
            for bucket_id, bucket in buckets.items():
                if isinstance(bucket, Mapping):
                    payload = dict(bucket)
                    payload.setdefault("id", bucket_id)
                    yield from self.iter_bucket(payload)
            return
        if isinstance(buckets, Iterable) and not isinstance(buckets, (str, bytes)):
            for bucket in buckets:
                yield from self.iter_bucket(bucket)
            return
        raise TypeError("buckets must be a bucket mapping or iterable of buckets")

    def fetch_events(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        bucket_ids: Optional[Sequence[str]] = None,
    ) -> Iterator[ActivityEvent]:
        """Read current collector buckets/events through its REST API."""

        for item in self.fetch_raw_events(start=start, end=end, bucket_ids=bucket_ids):
            yield item["normalized"]

    def fetch_raw_events(
        self,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        bucket_ids: Optional[Sequence[str]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield raw AW payloads together with their normalized representation.

        ActivityWatch versions differ in whether the events endpoint applies
        ``starttime``/``endtime`` server-side, so the adapter always applies
        an interval-overlap filter locally as well.
        """

        if self.client is None:
            raise RuntimeError("ActivityWatchAdapter.fetch_events requires an ActivityWatchClient")
        start_timestamp = coerce_timestamp(start) if start is not None else None
        end_timestamp = coerce_timestamp(end) if end is not None else None
        buckets = self.client.list_buckets()
        if isinstance(buckets, Mapping):
            available = buckets.items()
        else:
            available = (
                (_first_value(bucket, "id", "name"), bucket)
                for bucket in buckets
            )
        requested = set(bucket_ids) if bucket_ids else None
        for bucket_id, bucket_metadata in available:
            if bucket_id is None or (requested is not None and bucket_id not in requested):
                continue
            events = self.client.get_events(str(bucket_id), start=start, end=end)
            if isinstance(events, Mapping) and "events" in events:
                events = events["events"]
            bucket = dict(bucket_metadata) if isinstance(bucket_metadata, Mapping) else {}
            bucket.setdefault("id", bucket_id)
            for event in events:
                normalized = self.normalize_event(event, bucket=bucket)
                if start_timestamp is not None and normalized.end_timestamp <= start_timestamp:
                    continue
                if end_timestamp is not None and normalized.timestamp >= end_timestamp:
                    continue
                yield {
                    "bucket_id": str(bucket_id),
                    "bucket": dict(bucket),
                    "event": dict(event),
                    "normalized": normalized,
                    "start": normalized.timestamp,
                    "end": normalized.end_timestamp,
                }

    def today_active_snapshot(self, start: Any, end: Any) -> Dict[str, Any]:
        """Calendar-window activity totals using the source query language.

        Returns Noema-owned summary fields. Watcher bucket names stay inside
        this adapter.
        """
        if self.client is None:
            raise RuntimeError("today_active_snapshot requires an ActivityWatchClient")
        app_result = self.client.query([
            'afk_events = query_bucket(find_bucket("aw-watcher-afk_"));',
            'window_events = query_bucket(find_bucket("aw-watcher-window_"));',
            'window_events = filter_period_intersect(window_events, filter_keyvals(afk_events, "status", ["not-afk"]));',
            'app_events = merge_events_by_keys(window_events, ["app"]);',
            'RETURN = sort_by_duration(app_events);',
        ], start, end)
        title_result = self.client.query([
            'afk_events = query_bucket(find_bucket("aw-watcher-afk_"));',
            'window_events = query_bucket(find_bucket("aw-watcher-window_"));',
            'window_events = filter_period_intersect(window_events, filter_keyvals(afk_events, "status", ["not-afk"]));',
            'title_events = merge_events_by_keys(window_events, ["app", "title"]);',
            'RETURN = sort_by_duration(title_events);',
        ], start, end)
        apps_raw = app_result[0] if app_result and isinstance(app_result, list) else []
        titles_raw = title_result[0] if title_result and isinstance(title_result, list) else []
        active_seconds = sum(
            max(0.0, float(item.get("duration", 0) or 0))
            for item in apps_raw if isinstance(item, dict)
        )
        top_apps = []
        for item in apps_raw[:15]:
            if not isinstance(item, dict):
                continue
            data = item.get("data", {}) or {}
            top_apps.append({
                "raw_name": str(data.get("app") or "Other"),
                "seconds": round(max(0.0, float(item.get("duration", 0) or 0)), 1),
            })
        top_titles = []
        for item in titles_raw[:15]:
            if not isinstance(item, dict):
                continue
            data = item.get("data", {}) or {}
            top_titles.append({
                "name": str(data.get("title") or "Unknown"),
                "app": str(data.get("app") or ""),
                "seconds": round(max(0.0, float(item.get("duration", 0) or 0)), 1),
            })
        latest = None
        buckets = self.client.list_buckets()
        items = buckets.items() if isinstance(buckets, dict) else []
        for bucket_id, bucket in items:
            if not isinstance(bucket, dict) or source_event_kind(bucket, str(bucket_id)) != "window":
                continue
            events = self.client.get_events(str(bucket_id), start=start, end=end)
            if isinstance(events, dict):
                events = events.get("events", [])
            for event in (events or [])[:1]:
                timestamp = event.get("timestamp") if isinstance(event, dict) else None
                if timestamp:
                    latest = timestamp
                    break
            if latest:
                break
        local_start = coerce_timestamp(start)
        return {
            "available": True,
            "date": local_start.date().isoformat(),
            "active_seconds": round(active_seconds, 1),
            "latest_event_at": latest,
            "top_applications": top_apps,
            "top_titles": top_titles,
        }
