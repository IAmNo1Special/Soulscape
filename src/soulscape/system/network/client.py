"""Lightweight async network client for the Soulscape Hub."""

from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import quote

import httpx

from soulscape.system.logger import log
from soulscape.utils.security import redact_secret


class NetworkClient:
    """Async client wrapper for Soulscape backend services."""

    def __init__(
        self, base_url: Optional[str] = None, secret_key: Optional[str] = None
    ):
        self.base_url = base_url or os.getenv(
            "HUB_URL", "http://localhost:8000"
        )
        # Security: Warn if sending secrets over unencrypted HTTP (to non-localhost)
        if (
            self.base_url.startswith("http://")
            and "localhost" not in self.base_url
            and "127.0.0.1" not in self.base_url
        ):
            log.warning(
                f"SECURITY WARNING: Hub URL '{self.base_url}' uses unencrypted HTTP. "
                "Secrets will be transmitted in plain text! Please use HTTPS for production."
            )

        self.secret_key = secret_key or os.getenv("HUB_SECRET_KEY", "")
        self.headers = (
            {"X-Hub-Secret": self.secret_key} if self.secret_key else {}
        )
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazily initializes and returns the shared httpx.AsyncClient."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, headers=self.headers
            )
        return self._client

    async def close(self) -> None:
        """Closes the shared httpx.AsyncClient."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _handle_error_response(
        self, response: httpx.Response, method: str, endpoint: str
    ) -> None:
        """Logs detailed error information for failed requests."""
        try:
            # Avoid leaking potentially large or binary response bodies
            resp_text = response.text
            if len(resp_text) > 500:
                resp_text = resp_text[:500] + "... [truncated]"

            detail = "No detail"
            try:
                detail = response.json().get("detail", "No detail")
            except (httpx.JSONDecodeError, ValueError):
                detail = resp_text

            log.error(
                f"Hub {method} error ({response.status_code}) at {endpoint}: {detail}"
            )

        except Exception as e:
            err_msg = redact_secret(str(e), self.secret_key)
            log.error(
                f"Hub {method} error ({response.status_code}) at {endpoint}: {err_msg}"
            )

    async def _get(self, endpoint: str, token: Optional[str] = None) -> Any:
        try:
            headers = {"X-Hub-Secret": token} if token else self.headers
            client = await self._get_client()
            response = await client.get(endpoint, headers=headers)
            if response.status_code >= 400:
                self._handle_error_response(response, "GET", endpoint)
                return None
            return response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.debug(f"Network GET: Hub unreachable at {endpoint} ({e})")
            return None
        except Exception as e:
            log.error(f"Network GET error at {endpoint}: {e}")
            return None

    async def _post(
        self, endpoint: str, data: dict[str, Any], token: Optional[str] = None
    ) -> Any:
        try:
            headers = {"X-Hub-Secret": token} if token else self.headers
            client = await self._get_client()
            response = await client.post(endpoint, json=data, headers=headers)
            if response.status_code >= 400:
                self._handle_error_response(response, "POST", endpoint)
                return None
            return response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.debug(f"Network POST: Hub unreachable at {endpoint} ({e})")
            return None
        except Exception as e:
            log.error(f"Network POST error at {endpoint}: {e}")
            return None

    async def _delete(
        self,
        endpoint: str,
        data: Optional[dict[str, Any]] = None,
        token: Optional[str] = None,
    ) -> Any:
        try:
            headers = {"X-Hub-Secret": token} if token else self.headers
            client = await self._get_client()
            # Some APIs expect DELETE data in the body
            response = await client.request(
                "DELETE", endpoint, json=data, headers=headers
            )
            if response.status_code >= 400:
                self._handle_error_response(response, "DELETE", endpoint)
                return None
            return response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.debug(f"Network DELETE: Hub unreachable at {endpoint} ({e})")
            return None
        except Exception as e:
            log.error(f"Network DELETE error at {endpoint}: {e}")
            return None

    # --- Specific Endpoints (Placeholders for now) ---
    async def get_marketplace(self) -> Any:
        return await self._get("/marketplace")

    async def post_listing(
        self, listing_data: dict[str, Any], token: Optional[str] = None
    ) -> Any:
        return await self._post("/marketplace/list", listing_data, token=token)

    async def buy_item(
        self,
        listing_id: str,
        buyer_data: dict[str, Any],
        token: Optional[str] = None,
    ) -> Any:
        return await self._post(
            f"/marketplace/buy/{quote(listing_id)}", buyer_data, token=token
        )

    async def get_messages(self) -> Any:
        return await self._get("/social")

    async def post_message(
        self, message_data: dict[str, Any], token: Optional[str] = None
    ) -> Any:
        return await self._post("/social/post", message_data, token=token)

    async def post_reply(
        self, reply_data: dict[str, Any], token: Optional[str] = None
    ) -> Any:
        return await self._post("/social/reply", reply_data, token=token)

    async def edit_message(
        self,
        message_id: str,
        edit_data: dict[str, Any],
        token: Optional[str] = None,
    ) -> Any:
        return await self._post(
            f"/social/edit/{quote(message_id)}", edit_data, token=token
        )

    async def delete_message(
        self, message_id: str, author_id: str, token: Optional[str] = None
    ) -> Any:
        """Deletes a message from the board. Passes author_id in body for REST consistency."""
        return await self._delete(
            f"/social/delete/{quote(message_id)}",
            data={"author_id": author_id},
            token=token,
        )

    async def delete_listing(
        self, listing_id: str, token: Optional[str] = None
    ) -> Any:
        return await self._delete(
            f"/marketplace/delete/{quote(listing_id)}", token=token
        )

    async def update_funds(self, amount: float) -> Any:
        return await self._post("/marketplace/funds", {"amount": amount})

    async def get_souls(self) -> Any:
        return await self._get("/souls")

    async def get_souls_by_owner(self, owner_id: str) -> Any:
        return await self._get(f"/souls?owner_id={quote(owner_id)}")

    async def post_souls(
        self,
        souls_data: list[dict[str, Any]],
        owner_id: str,
        token: Optional[str] = None,
    ) -> Any:
        payload = {"owner_id": owner_id, "souls": souls_data}
        return await self._post("/souls", payload, token=token)
