"""Background activity watcher: apps that are open but not in front, and apps playing audio.

The foreground watcher answers "what am I looking at". This one answers "what else is running
while I work", e.g. Spotify playing, a video call left open, a game minimised. It is
deliberately separate from the foreground timeline: background time never becomes a session,
never feeds classification and never counts as screen time.

Privacy: only app names and seconds are kept (one JSON blob per day in the store's state
table). Window titles are read to filter noise and thrown away.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("noema.background")

# Shell / helper processes that always have a window but are never "an app you have open".
IGNORED_APPS = frozenset({
    "textinputhost", "searchhost", "startmenuexperiencehost", "shellhost", "shellexperiencehost",
    "lockapp", "systemsettings", "applicationframehost", "widgets", "dwm", "nvidia overlay",
    "nvcontainer", "explorer", "program manager", "msedgewebview2", "ctfmon", "noema",
})

OpenWindows = Callable[[], List[Tuple[str, str]]]
AudioApps = Callable[[], Optional[Set[str]]]


def _clean(name: Optional[str]) -> str:
    text = str(name or "").strip()
    return text[:-4] if text.lower().endswith(".exe") else text


def windows_open_apps(exclude_app: Optional[str] = None) -> List[Tuple[str, str]]:
    """(app, title) for visible, non-cloaked top-level windows; one entry per app."""
    import sys

    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    from .native_windows import _windows_process_name

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        dwm = ctypes.WinDLL("dwmapi")
    except OSError:
        dwm = None
    found: Dict[str, str] = {}
    exclude = _clean(exclude_app).lower()
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            ex_style = user32.GetWindowLongW(hwnd, -20)
            if ex_style & 0x80 and not ex_style & 0x40000:   # tool window without APPWINDOW
                return True
            if dwm is not None:
                cloaked = ctypes.c_int(0)
                dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
                if cloaked.value:
                    return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, len(buffer))
            app = _clean(_windows_process_name(user32, kernel32, hwnd))
            if app and app.lower() not in IGNORED_APPS and app.lower() != exclude:
                found.setdefault(app, buffer.value)
        except Exception:  # one odd window must never stop the scan
            return True
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return sorted(found.items())


def windows_audio_apps() -> Optional[Set[str]]:
    """Names of processes with an active audio session; None when pycaw is not installed."""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception:
        return None
    try:
        playing = set()
        for session in AudioUtilities.GetAllSessions():
            process = getattr(session, "Process", None)
            if process is not None and getattr(session, "State", 0) == 1:
                playing.add(_clean(process.name()))
        return playing
    except Exception:
        return set()


def _foreground_app() -> Optional[str]:
    try:
        from .native_windows import _windows_foreground_state

        state = _windows_foreground_state()
        return _clean(state[0]) if state else None
    except Exception:
        return None


class BackgroundWatcher:
    """Accumulate per-app 'open in background' and 'audio playing' seconds per local day."""

    STATE_PREFIX = "background:"

    def __init__(self, store: Any, interval_seconds: float = 15.0,
                 open_provider: Optional[OpenWindows] = None, audio_provider: Optional[AudioApps] = None,
                 foreground_provider: Optional[Callable[[], Optional[str]]] = None,
                 timezone_name: str = "UTC") -> None:
        self.store = store
        self.interval = max(1.0, float(interval_seconds))
        self._open = open_provider
        self._audio = audio_provider or windows_audio_apps
        self._foreground = foreground_provider or _foreground_app
        self.timezone_name = timezone_name
        self._last: Optional[float] = None
        self._lock = threading.Lock()
        self.last_snapshot: Dict[str, Any] = {"open": [], "audio": [], "audio_supported": None}
        self.last_error: Optional[str] = None

    def _day_key(self, now: datetime) -> str:
        try:
            from zoneinfo import ZoneInfo

            local = now.astimezone(ZoneInfo(self.timezone_name))
        except Exception:
            local = now
        return self.STATE_PREFIX + local.strftime("%Y-%m-%d")

    def _load(self, key: str) -> Dict[str, Dict[str, float]]:
        try:
            raw = self.store.get_state(key)
            data = json.loads(raw) if raw else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def tick(self, now: Optional[datetime] = None, monotonic: Optional[float] = None) -> bool:
        """Sample at most once per interval. Never raises. True when a sample was taken."""
        clock = time.monotonic() if monotonic is None else monotonic
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            if self._last is not None and clock - self._last < self.interval:
                return False
            elapsed = self.interval if self._last is None else min(clock - self._last, self.interval * 3)
            self._last = clock
        try:
            foreground = self._foreground()
            open_fn = self._open or (lambda: windows_open_apps(foreground))
            open_apps = [(a, t) for a, t in open_fn() if a.lower() != _clean(foreground).lower()]
            audio = self._audio()
            audio_set = {a for a in (audio or set()) if a.lower() not in IGNORED_APPS}
            names = {a for a, _ in open_apps}
            key = self._day_key(moment)
            totals = self._load(key)
            for app in names | audio_set:
                row = totals.setdefault(app, {"open_s": 0.0, "audio_s": 0.0})
                if app in names:
                    row["open_s"] = round(row.get("open_s", 0.0) + elapsed, 1)
                if app in audio_set:
                    row["audio_s"] = round(row.get("audio_s", 0.0) + elapsed, 1)
            self.store.set_state(key, json.dumps(totals))
            self.last_snapshot = {"open": sorted(names), "audio": sorted(audio_set),
                                  "audio_supported": audio is not None}
            self.last_error = None
        except Exception as exc:  # the watcher must never break ingestion
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            logger.warning("background watcher failed: %s", self.last_error)
        return True

    def today(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        totals = self._load(self._day_key(now or datetime.now(timezone.utc)))
        rows = [{"app": app, "open_minutes": round(v.get("open_s", 0) / 60, 1),
                 "audio_minutes": round(v.get("audio_s", 0) / 60, 1)} for app, v in totals.items()]
        return sorted(rows, key=lambda r: (r["audio_minutes"], r["open_minutes"]), reverse=True)

    def status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        return {"now": self.last_snapshot, "today": self.today(now), "error": self.last_error}
