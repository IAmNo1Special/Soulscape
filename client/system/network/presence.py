"""WebSocket-based presence manager for real-time online/offline detection."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable, Coroutine
from typing import Any

import websockets

log = logging.getLogger("soulscape")


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
        on_connect: (
            Callable[[list[str]], Coroutine[Any, Any, None]] | None
        ) = None,
    ):
        self.owner_id = owner_id
        self.on_owner_online = on_owner_online
        self.on_owner_offline = on_owner_offline
        self.on_soul_updated = on_soul_updated
        self.on_connect = on_connect
        self._ws: websockets.ClientConnection | None = None
        self._running = False

        # Build WS URL from Hub URL (http -> ws)
        hub_url = os.getenv("HUB_URL", "http://localhost:8000")
        # Case-insensitive replacement of HTTP scheme
        hub_url_lower = hub_url.lower()
        if hub_url_lower.startswith("https://"):
            ws_url = (
                "wss://" + hub_url[8:]
            )  # Remove "https://" and add "wss://"
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
                    f"WebSocket connection lost: {e}. "
                    f"Reconnecting in {backoff}s..."
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
                msg_type = message.get("type")

                if msg_type == "connected":
                    # Initial connection — load all currently online owners
                    online_owners = message.get("online_owners", [])
                    log.info(
                        f"Hub reports {len(online_owners)} online owner(s)."
                    )
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

            except Exception as e:
                log.error(f"Error processing WS message: {e}")

    async def send_update(self, souls: list[dict[str, Any]]) -> None:
        """Broadcasts local soul state updates to the Hub."""
        if self._ws:
            try:
                await self._ws.send(
                    json.dumps({"type": "soul_update", "souls": souls})
                )
            except Exception as e:
                log.error(f"Error sending soul update: {e}")

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
