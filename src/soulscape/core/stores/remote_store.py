"""Remote implementation of the DataStore using the NetworkClient."""

from __future__ import annotations

from typing import Any

from soulscape.system.network.client import NetworkClient

from .base import DataStore


class RemoteStore(DataStore):
    """Network-backed storage implementation using the Soulscape Hub."""

    def __init__(self, client: NetworkClient | None = None):
        self.client = client or NetworkClient()

    # --- Marketplace ---
    async def load_marketplace(self) -> Any:
        return await self.client.get_marketplace()

    async def save_marketplace(self, data: dict[str, Any]) -> bool:
        # RemoteStore handles individual 'list' and 'buy' actions through tools.
        # This global save is a placeholder for heavy synchronization if needed.
        return True

    # --- Message Board ---
    async def load_messageboard(self) -> Any:
        return await self.client.get_messages()

    async def save_messageboard(self, data: dict[str, Any]) -> bool:
        # The data passed here usually comes from local state.
        # For Hub, we prefer individual 'post' calls.
        return True

    # --- Hub-specific Granular Operations ---
    async def add_listing(self, listing_data: dict[str, Any]) -> bool:
        res = await self.client.post_listing(listing_data)
        return res is not None and res.get("status") == "success"

    async def delete_listing(self, listing_id: str) -> bool:
        # Pure cancellation (different from 'buy')
        res = await self.client.delete_listing(listing_id)
        return res is not None and res.get("status") == "success"

    async def add_post(self, post_data: dict[str, Any]) -> bool:
        res = await self.client.post_message(post_data)
        return res is not None and res.get("status") == "success"

    async def add_reply(self, reply_data: dict[str, Any]) -> bool:
        res = await self.client.post_reply(reply_data)
        return res is not None and res.get("status") == "success"

    async def edit_post(
        self, message_id: str, content: str, author_id: str
    ) -> bool:
        res = await self.client.edit_message(
            message_id, {"content": content, "author_id": author_id}
        )
        return res is not None and res.get("status") == "success"

    async def delete_post(self, message_id: str, author_id: str) -> bool:
        res = await self.client.delete_message(message_id, author_id)
        return res is not None and res.get("status") == "success"

    async def buy_listing(
        self, listing_id: str, buyer_data: dict[str, Any]
    ) -> bool:
        res = await self.client.buy_item(listing_id, buyer_data)
        return res is not None and res.get("status") == "success"

    async def update_funds(self, amount: float) -> bool:
        res = await self.client.update_funds(amount)
        return res is not None and res.get("status") == "success"
