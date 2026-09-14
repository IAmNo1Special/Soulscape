import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger("soulscape_hub")


class ConnectionManager:
    """Manages active WebSocket connections for presence tracking."""

    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, owner_id: str, websocket: WebSocket):
        await websocket.accept()
        if owner_id in self.active_connections:
            try:
                await self.active_connections[owner_id].close()
            except Exception as e:
                logger.warning(
                    f"Failed to close existing websocket for {owner_id}: {e}"
                )
        self.active_connections[owner_id] = websocket
        logger.info(
            f"Owner connected: {owner_id} (Total: {len(self.active_connections)})"
        )

    def disconnect(self, owner_id: str):
        self.active_connections.pop(owner_id, None)
        logger.info(
            f"Owner disconnected: {owner_id} (Total: {len(self.active_connections)})"
        )

    def get_online_owners(self) -> list[str]:
        return list(self.active_connections.keys())

    async def broadcast(self, message: dict, exclude: str | None = None):
        async def _send(owner_id, ws):
            if owner_id == exclude:
                return
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=5)
            except Exception:
                self.active_connections.pop(owner_id, None)

        tasks = [_send(oid, ws) for oid, ws in list(self.active_connections.items())]
        await asyncio.gather(*tasks, return_exceptions=True)


manager = ConnectionManager()
