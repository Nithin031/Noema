"""Dependency-free WebSocket transport for the local browser bridge.

The normal WSGI API exposes a long-poll fallback.  This small server keeps the
 preferred delivery path available without adding a runtime dependency to the
 local activity intelligence service.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import struct
from typing import Any, Callable, Dict, Mapping, Optional

from .bridge import BrowserBridge


class BrowserWebSocketServer:
    """Serve browser registration, events, and targeted interventions."""

    def __init__(
        self,
        bridge: BrowserBridge,
        on_action: Optional[Callable[[str, Mapping[str, Any]], Any]] = None,
    ):
        self.bridge = bridge
        self.on_action = on_action

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readuntil(b"\r\n\r\n")
        headers = {}
        for line in raw.decode("latin-1").split("\r\n")[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().casefold()] = value.strip()
        key = headers.get("sec-websocket-key")
        if not key:
            raise ValueError("missing Sec-WebSocket-Key")
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Accept: {}\r\n\r\n"
            ).format(accept).encode("latin-1")
        )
        await writer.drain()

    async def _read_frame(self, reader: asyncio.StreamReader) -> Optional[str]:
        header = await reader.readexactly(2)
        first, second = header
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
        if length > 2_000_000:
            raise ValueError("browser WebSocket frame is too large")
        mask = await reader.readexactly(4) if masked else None
        payload = bytearray(await reader.readexactly(length))
        if mask:
            for index in range(length):
                payload[index] ^= mask[index % 4]
        if opcode == 0x8:
            return None
        if opcode == 0x9:
            return "__PING__"
        if opcode != 0x1:
            return ""
        return payload.decode("utf-8")

    async def _write_frame(self, writer: asyncio.StreamWriter, value: str) -> None:
        payload = value.encode("utf-8")
        length = len(payload)
        if length < 126:
            header = bytes([0x81, length])
        elif length < 65536:
            header = bytes([0x81, 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x81, 127]) + struct.pack("!Q", length)
        writer.write(header + payload)
        await writer.drain()

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        instance_id = None
        try:
            await self._handshake(reader, writer)
            first = await asyncio.wait_for(self._read_frame(reader), timeout=10.0)
            registration = json.loads(first or "{}")
            if registration.get("type") != "register":
                raise ValueError("first browser WebSocket message must register the extension")
            instance_id = str(registration.get("extension_instance_id") or "")
            self.bridge.register(
                registration.get("browser"),
                registration.get("device_id"),
                instance_id,
            )
            await self._write_frame(writer, json.dumps({"type": "registered", "extension_instance_id": instance_id}))
            loop = asyncio.get_running_loop()
            while True:
                read_task = asyncio.create_task(self._read_frame(reader))
                poll_task = asyncio.ensure_future(
                    loop.run_in_executor(None, self.bridge.poll, instance_id, 20.0, 10)
                )
                done, pending = await asyncio.wait(
                    (read_task, poll_task), return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                completed = done.pop()
                value = await completed
                if isinstance(value, list):
                    for message in value:
                        await self._write_frame(writer, json.dumps(message, ensure_ascii=False))
                    continue
                if value is None:
                    break
                if value == "__PING__":
                    await self._write_frame(writer, json.dumps({"type": "pong"}))
                    continue
                if not value:
                    continue
                message = json.loads(value)
                message_type = message.get("type")
                if message_type == "event":
                    self.bridge.report_event(instance_id, message.get("event") or message)
                elif message_type == "action" and self.on_action:
                    self.on_action(instance_id, message)
        except (asyncio.IncompleteReadError, ConnectionError, OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
        finally:
            if instance_id:
                self.bridge.unregister(instance_id)
            writer.close()
            try:
                await writer.wait_closed()
            except (AttributeError, ConnectionError, OSError):
                pass

    async def serve(self, host: str = "127.0.0.1", port: int = 8766) -> None:
        server = await asyncio.start_server(self._connection, host, port)
        async with server:
            await server.serve_forever()

def serve_websocket(bridge: BrowserBridge, host: str = "127.0.0.1", port: int = 8766) -> None:
    asyncio.run(BrowserWebSocketServer(bridge).serve(host, port))
