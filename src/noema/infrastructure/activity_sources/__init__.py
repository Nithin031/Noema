"""Noema telemetry sources.

Native Noema collectors (preferred production path) live alongside the
legacy ActivityWatch compatibility adapter::

    ActivitySource
    ├── NativeWindowsActivitySource  (foreground app/title, Windows)
    ├── NativeAFKSource              (input presence, Windows)
    ├── BrowserActivitySource        (extension bridge health; push-based)
    └── ActivityWatchSourceAdapter   (legacy ActivityWatch REST source)

Only this package speaks ActivityWatch concepts; the domain and
application layers consume canonical ``ActivityEvent`` rows.
"""

from .activitywatch import ActivityWatchAdapter, ActivityWatchClient  # noqa: F401
from .base import (  # noqa: F401
    ERROR,
    OK,
    UNAVAILABLE,
    DEGRADED,
    ActivitySource,
    CompositeActivitySource,
    SourcePoll,
    utcnow,
)
from .browser_source import BrowserActivitySource  # noqa: F401
from .native_presence import NativeAFKSource  # noqa: F401
from .native_windows import NativeWindowsActivitySource  # noqa: F401

# Legacy alias: the ActivityWatch adapter is the optional compatibility
# source. New code should depend on ``ActivitySource`` instead.
ActivityWatchSourceAdapter = ActivityWatchAdapter
