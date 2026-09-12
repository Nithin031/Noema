"""Local browser bridge for exact-tab interventions."""

from .bridge import BrowserBridge, BrowserDeliveryResult
from .models import BrowserEvent, BrowserRegistration
from .notifications import DesktopNotificationHandler
from .websocket import BrowserWebSocketServer, serve_websocket

__all__ = [
    "BrowserBridge",
    "BrowserDeliveryResult",
    "BrowserEvent",
    "BrowserRegistration",
    "DesktopNotificationHandler",
    "BrowserWebSocketServer",
    "serve_websocket",
]
