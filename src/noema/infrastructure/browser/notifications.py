"""Best-effort desktop notification fallback with no mandatory dependency."""

from __future__ import annotations

import os
from typing import Any, Dict


class DesktopNotificationHandler:
    """Send a local Windows toast when an optional toast runtime is present.

    The browser extension is the primary delivery path.  Missing optional
    Windows notification packages are reported as a result instead of raising
    from the activity pipeline.
    """

    def send(self, action: Any) -> Dict[str, Any]:
        if hasattr(action, "action"):
            payload = action.action.to_dict()
        elif hasattr(action, "to_dict"):
            payload = action.to_dict()
        else:
            payload = dict(action or {})
        if os.name != "nt":
            return {"delivered": False, "reason": "desktop notifications require Windows"}
        try:
            from winrt.windows.ui.notifications import ToastNotification, ToastNotificationManager
            from winrt.windows.data.xml.dom import XmlDocument
        except (ImportError, ModuleNotFoundError):
            return {"delivered": False, "reason": "optional winrt notification package is unavailable"}

        message = payload.get("message") or {}
        title = str(message.get("title") or "Noema")
        body = str(message.get("body") or "You may be drifting from your current goal.")
        escaped_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        escaped_body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        try:
            xml = XmlDocument()
            xml.load_xml(
                '<toast><visual><binding template="ToastGeneric">'
                '<text>{}</text><text>{}</text></binding></visual></toast>'.format(
                    escaped_title, escaped_body
                )
            )
            notifier = ToastNotificationManager.create_toast_notifier("Noema")
            notifier.show(ToastNotification(xml))
            return {"delivered": True, "channel": "windows_toast"}
        except Exception as exc:
            return {"delivered": False, "reason": "windows toast failed: {}".format(type(exc).__name__)}
