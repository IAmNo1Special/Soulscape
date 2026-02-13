"""Lightweight async network client for the Soulscape Hub."""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from soulscape.system.logger import log


class NetworkClient:
    """Async client wrapper for Soulscape backend services."""

    def __init__(
        self, base_url: Optional[str] = None, api_key: Optional[str] = None
    ):
        self.base_url = base_url or os.getenv(
            "SOULSCAPE_HUB_URL", "http://localhost:8000"
        )
        self.api_key = api_key or os.getenv("SOULSCAPE_HUB_KEY", "")
        self.headers = {"X-API-Key": self.api_key} if self.api_key else {}

    async def _get(self, endpoint: str) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, headers=self.headers
            ) as client:
                response = await client.get(endpoint)
                response.raise_for_status()
                return response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.debug(f"Network GET: Hub unreachable at {endpoint} ({e})")
            return None
        except Exception as e:
            log.error(f"Network GET error at {endpoint}: {e}")
            return None

    async def _post(self, endpoint: str, data: dict[str, Any]) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, headers=self.headers
            ) as client:
                response = await client.post(endpoint, json=data)
                response.raise_for_status()
                return response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.debug(f"Network POST: Hub unreachable at {endpoint} ({e})")
            return None
        except Exception as e:
            log.error(f"Network POST error at {endpoint}: {e}")
            return None

    # --- Specific Endpoints (Placeholders for now) ---
    async def get_marketplace(self) -> Any:
        return await self._get("/marketplace")

    async def post_listing(self, listing_data: dict[str, Any]) -> Any:
        return await self._post("/marketplace/list", listing_data)

    async def buy_item(
        self, listing_id: str, buyer_data: dict[str, Any]
    ) -> Any:
        return await self._post(f"/marketplace/buy/{listing_id}", buyer_data)

    async def get_messages(self) -> Any:
        return await self._get("/social")

    async def post_message(self, message_data: dict[str, Any]) -> Any:
        return await self._post("/social/post", message_data)

    async def post_reply(self, reply_data: dict[str, Any]) -> Any:
        return await self._post("/social/reply", reply_data)

    async def edit_message(
        self, message_id: str, edit_data: dict[str, Any]
    ) -> Any:
        return await self._post(f"/social/edit/{message_id}", edit_data)

    async def delete_message(self, message_id: str, author_id: int) -> Any:
        # Pydantic expect author_id in some way? Or query param?
        # Hub expects it in delete_message(message_id, author_id)
        # We'll pass it as query param or extra data if we want
        return await self._post(
            f"/social/delete/{message_id}?author_id={author_id}", {}
        )

    async def delete_listing(self, listing_id: str) -> Any:
        return await self._post(f"/marketplace/delete/{listing_id}", {})

    async def update_funds(self, amount: float) -> Any:
        return await self._post("/marketplace/funds", {"amount": amount})

    async def get_souls(self) -> Any:
        return await self._get("/souls")

    async def get_souls_by_owner(self, owner_id: str) -> Any:
        return await self._get(f"/souls?owner_id={owner_id}")

    async def post_souls(
        self, souls_data: list[dict[str, Any]], owner_id: str
    ) -> Any:
        payload = {"owner_id": owner_id, "souls": souls_data}
        return await self._post("/souls", payload)
