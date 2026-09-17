"""WebSocket-based presence manager for real-time online/offline detection."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from collections.abc import Callable, Coroutine
from typing import Any

import websockets
from shared import protocol

log = logging.getLogger("soulscape")

INTENT_ACK_TIMEOUT = 10.0


def _intent_canonical(message: dict[str, Any]) -> str:
    body = {k: v for k, v in message.items() if k not in ("type", "signature")}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


class PresenceManager:
    """Manages a persistent WebSocket connection to the Hub for presence.

    Connects to the Hub's WS endpoint, listens for owner_online/owner_offline
    events, and calls the provided callbacks to add/remove remote souls.
    Automatically reconnects with exponential backoff on connection loss.
    """

    def __init__(
        self,
        owner_id: str,
        on_owner_online: Callable[[str], Coroutine[Any, Any, None]],
        on_owner_offline: Callable[[str], None],
        on_soul_updated: Callable[[list[dict], str], Coroutine[Any, Any, None]],
        on_connect: (Callable[[list[str]], Coroutine[Any, Any, None]] | None) = None,
        on_viewport_frame: (Callable[[dict], Coroutine[Any, Any, None]] | None) = None,
    ):
        self.owner_id = owner_id
        self.on_owner_online = on_owner_online
        self.on_owner_offline = on_owner_offline
        self.on_soul_updated = on_soul_updated
        self.on_connect = on_connect
        self.on_viewport_frame = on_viewport_frame
        self._ws: websockets.ClientConnection | None = None
        self._running = False
        self._session_id: str | None = None
        self._hmac_key: str | None = None
        self._pending_acks: dict[str, asyncio.Future] = {}

        # Build WS URL from Hub URL (http -> ws)
        hub_url = os.getenv("HUB_URL", "http://localhost:9785")
        # Case-insensitive replacement of HTTP scheme
        hub_url_lower = hub_url.lower()
        if hub_url_lower.startswith("https://"):
            ws_url = "wss://" + hub_url[8:]  # Remove "https://" and add "wss://"
        elif hub_url_lower.startswith("http://"):
            ws_url = "ws://" + hub_url[7:]  # Remove "http://" and add "ws://"
        else:
            ws_url = f"ws://{hub_url}"  # Default to ws if no scheme

        self.secret_key = os.getenv("HUB_SECRET_KEY", "")
        self._ws_url = ws_url + f"/ws/{owner_id}"

    async def connect(self) -> None:
        """Start the WebSocket connection loop with auto-reconnect."""
        self._running = True
        backoff = 1

        # Prepare headers if secret is present
        headers = {}
        if self.secret_key:
            headers["X-Hub-Secret"] = self.secret_key

        auth_failures = 0
        max_auth_failures = 5
        while self._running:
            try:
                log.info(f"Connecting to Hub WebSocket: {self._ws_url}")
                async with websockets.connect(
                    self._ws_url,
                    additional_headers=headers if headers else None,
                ) as ws:
                    self._ws = ws
                    backoff = 1  # Reset backoff on successful connect
                    auth_failures = 0  # Reset auth failures on success
                    log.info("WebSocket connected to Hub.")
                    await self._listen(ws)
            except websockets.exceptions.ConnectionClosed as e:
                if e.code == 1008:
                    auth_failures += 1
                    log.error(
                        f"Hub WebSocket auth failure ({auth_failures}/{max_auth_failures}). Check HUB_SECRET_KEY."
                    )
                    if auth_failures >= max_auth_failures:
                        log.error("Too many auth failures. Giving up.")
                        self._running = False
                        break

                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                else:
                    log.warning(f"WebSocket connection closed: {e}")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
            except (
                ConnectionError,
                OSError,
                websockets.exceptions.WebSocketException,
            ) as e:
                if not self._running:
                    break
                log.warning(
                    f"WebSocket connection lost: {e}. Reconnecting in {backoff}s..."
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)  # Cap at 30 seconds
            except Exception as e:
                if not self._running:
                    break
                log.error(f"Unexpected WebSocket error: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _listen(self, ws: websockets.ClientConnection) -> None:
        """Listen for presence events from the Hub."""
        async for raw_message in ws:
            try:
                message = json.loads(raw_message)
                env = protocol.parse_envelope(message)
                if env.v != protocol.PROTOCOL_VERSION:
                    log.warning(f"Unsupported protocol version: {env.v}")
                    continue
                msg_type = env.type

                if msg_type == "connected":
                    self._session_id = message.get("session_id")
                    self._hmac_key = message.get("hmac_key")
                    # Initial connection — load all currently online owners
                    online_owners = message.get("online_owners", [])
                    log.info(f"Hub reports {len(online_owners)} online owner(s).")
                    if self.on_connect:
                        await self.on_connect(online_owners)

                    for oid in online_owners:
                        await self.on_owner_online(oid)

                elif msg_type == "owner_online":
                    oid = message.get("owner_id")
                    log.info(f"Owner came online: {oid}")
                    await self.on_owner_online(oid)

                elif msg_type == "owner_offline":
                    oid = message.get("owner_id")
                    log.info(f"Owner went offline: {oid}")
                    self.on_owner_offline(oid)

                elif msg_type == "soul_updated":
                    oid = message.get("owner_id")
                    souls = message.get("souls", [])
                    await self.on_soul_updated(souls, oid)

                elif msg_type in (
                    protocol.MessageType.SNAPSHOT.value,
                    protocol.MessageType.DELTA.value,
                ):
                    if self.on_viewport_frame is not None:
                        await self.on_viewport_frame(message)

                elif msg_type == protocol.MessageType.INTENT_ACK.value:
                    self._resolve_ack(message.get("nonce"), message)

                elif msg_type == protocol.MessageType.ERROR.value:
                    self._resolve_ack(message.get("nonce"), message)

            except Exception as e:
                log.error(f"Error processing WS message: {e}")

    def _resolve_ack(self, nonce: Any, message: dict[str, Any]) -> None:
        if not nonce:
            return
        future = self._pending_acks.pop(nonce, None)
        if future is not None and not future.done():
            future.set_result(message)

    async def send_intent(
        self, kind: str, soul_id: str, **fields: Any
    ) -> dict[str, Any] | None:
        if self._ws is None or self._session_id is None or self._hmac_key is None:
            log.warning("send_intent dropped: no signed session")
            return None
        nonce = secrets.token_urlsafe(16)
        body: dict[str, Any] = {
            "kind": kind,
            "nonce": nonce,
            "session_id": self._session_id,
            "soul_id": soul_id,
            **fields,
        }
        payload = protocol.envelope(protocol.MessageType.INTENT, **body)
        payload["signature"] = hmac.new(
            self._hmac_key.encode(),
            _intent_canonical(payload).encode(),
            hashlib.sha256,
        ).hexdigest()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_acks[nonce] = future
        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=INTENT_ACK_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.warning(f"Intent ack timeout for nonce {nonce}")
            return None
        finally:
            self._pending_acks.pop(nonce, None)

    async def disconnect(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        log.info("WebSocket disconnected from Hub.")
