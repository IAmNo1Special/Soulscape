import logging

from fastapi import WebSocket

logger = logging.getLogger("soulscape_hub")


class ConnectionManager:
    """Manages active WebSocket connections for presence tracking."""

    def __init__(self):
        # {owner_id: WebSocket}
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, owner_id: str, websocket: WebSocket):
        await websocket.accept()
        # Close existing connection for this owner if any
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
        """Send a message to all connected clients, optionally excluding one."""
        for owner_id, ws in list(self.active_connections.items()):
            if owner_id == exclude:
                continue
            try:
                await ws.send_json(message)
            except Exception:
                self.active_connections.pop(owner_id, None)


manager = ConnectionManager()
