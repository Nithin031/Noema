"""Thread-safe browser registration, targeting, and delivery queues."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Union

from .models import BrowserEvent, BrowserRegistration


@dataclass(frozen=True)
class BrowserDeliveryResult:
    status: str
    intervention_id: str
    recipients: int = 0
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "intervention_id": self.intervention_id,
            "recipients": self.recipients,
            "reason": self.reason,
        }


@dataclass
class _BrowserClient:
    registration: BrowserRegistration
    queue: List[Dict[str, Any]]


class BrowserBridge:
    """Keep exact-tab browser state separate from ActivityWatch telemetry.

    A delivery is queued only for the registered extension whose current
    window/tab matches the action target.  An unrelated active tab is never a
    valid recipient.
    """

    def __init__(self, pending_ttl_seconds: float = 30.0):
        if pending_ttl_seconds < 0:
            raise ValueError("pending_ttl_seconds cannot be negative")
        self.pending_ttl_seconds = float(pending_ttl_seconds)
        self._clients: Dict[str, _BrowserClient] = {}
        self._pending: List[tuple] = []
        self._condition = threading.Condition(threading.RLock())

    def register(
        self,
        browser: str,
        device_id: str,
        extension_instance_id: str,
    ) -> BrowserRegistration:
        registration = BrowserRegistration(browser, device_id, extension_instance_id)
        with self._condition:
            self._clients[registration.extension_instance_id] = _BrowserClient(registration, [])
            self._flush_pending_locked(self._clients[registration.extension_instance_id])
            self._condition.notify_all()
        return registration

    def unregister(self, extension_instance_id: str) -> bool:
        with self._condition:
            existed = self._clients.pop(str(extension_instance_id), None) is not None
            self._condition.notify_all()
            return existed

    def report_event(self, extension_instance_id: str, event: Union[BrowserEvent, Mapping[str, Any]]) -> BrowserRegistration:
        event = event if isinstance(event, BrowserEvent) else BrowserEvent.from_mapping(event)
        with self._condition:
            client = self._clients.get(str(extension_instance_id))
            if client is None:
                raise KeyError("browser extension is not registered")
            registration = client.registration
            if event.window_id is not None:
                registration.current_window_id = event.window_id
            if event.tab_id is not None:
                registration.current_tab_id = event.tab_id
            if event.private is not None:
                registration.private = event.private
            registration.last_seen_at = event.timestamp
            self._flush_pending_locked(client)
            self._condition.notify_all()
            return registration

    def current_target(self, device_id: Optional[str] = None, browser: Optional[str] = None) -> Dict[str, str]:
        with self._condition:
            for client in self._clients.values():
                registration = client.registration
                if device_id and registration.device_id != str(device_id):
                    continue
                if browser and registration.browser != str(browser).casefold():
                    continue
                if registration.current_window_id is None or registration.current_tab_id is None:
                    continue
                target = {
                    "device": registration.device_id,
                    "browser": registration.browser,
                    "window_id": registration.current_window_id,
                    "tab_id": registration.current_tab_id,
                }
                return target
        return {}

    @staticmethod
    def _target_matches(registration: BrowserRegistration, target: Mapping[str, Any]) -> bool:
        if registration.private:
            return False
        if not target.get("tab_id"):
            return False
        if target.get("device") and str(target["device"]) != registration.device_id:
            return False
        if target.get("browser") and str(target["browser"]).casefold() != registration.browser:
            return False
        if str(target["tab_id"]) != str(registration.current_tab_id):
            return False
        if target.get("window_id") and str(target["window_id"]) != str(registration.current_window_id):
            return False
        return True

    @staticmethod
    def _action_dict(action: Any) -> Dict[str, Any]:
        if hasattr(action, "action"):
            action = action.action
        if hasattr(action, "to_dict"):
            action = action.to_dict()
        payload = dict(action or {})
        if "intervention_id" not in payload and "id" in payload:
            payload["intervention_id"] = payload["id"]
        return payload

    def publish(self, action: Any) -> BrowserDeliveryResult:
        payload = self._action_dict(action)
        intervention_id = str(payload.get("intervention_id") or "")
        action_type = str(payload.get("type") or "NONE").upper()
        if action_type in {"HOLDOUT", "NONE"}:
            return BrowserDeliveryResult("SUPPRESSED", intervention_id, reason="non-deliverable intervention type")
        target = payload.get("target") or {}
        if not target.get("tab_id"):
            return BrowserDeliveryResult("NO_EXACT_TARGET", intervention_id, reason="target.tab_id is required")
        envelope = {
            "type": "intervention",
            "delivery_state": "RECEIVED",
            "payload": payload,
        }
        with self._condition:
            self._expire_pending_locked()
            recipients = 0
            for client in self._clients.values():
                if self._target_matches(client.registration, target):
                    client.queue.append(envelope)
                    recipients += 1
            if recipients:
                self._condition.notify_all()
                return BrowserDeliveryResult("DELIVERED", intervention_id, recipients)
            # Keep a targeted message briefly so a tab activation race can be
            # resolved without ever sending it to an unrelated tab.
            self._pending.append((time.monotonic() + self.pending_ttl_seconds, envelope))
            return BrowserDeliveryResult("QUEUED", intervention_id, reason="target tab is not currently active")

    def poll(self, extension_instance_id: str, timeout_seconds: float = 0.0, limit: int = 10) -> List[Dict[str, Any]]:
        if timeout_seconds < 0 or limit < 1:
            raise ValueError("timeout_seconds must be non-negative and limit must be positive")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._condition:
            while True:
                client = self._clients.get(str(extension_instance_id))
                if client is None:
                    raise KeyError("browser extension is not registered")
                self._expire_pending_locked()
                if client.queue:
                    result = client.queue[:limit]
                    del client.queue[:limit]
                    return result
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(timeout=remaining)

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._condition:
            return [client.registration.to_dict() for client in self._clients.values()]

    def _flush_pending_locked(self, client: _BrowserClient) -> None:
        self._expire_pending_locked()
        remaining = []
        for expires_at, envelope in self._pending:
            target = envelope.get("payload", {}).get("target", {})
            if self._target_matches(client.registration, target):
                client.queue.append(envelope)
            else:
                remaining.append((expires_at, envelope))
        self._pending = remaining

    def _expire_pending_locked(self) -> None:
        now = time.monotonic()
        self._pending = [(expires_at, envelope) for expires_at, envelope in self._pending if expires_at >= now]
