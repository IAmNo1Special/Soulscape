"""Hub client for the ambient recap dashboard view (issue #33).

Mirrors MailbagClient: injected URL/secret resolvers and injectable
HTTP functions keep it headless-testable.
"""

from __future__ import annotations

from typing import Any

import httpx


class RecapClient:
    def __init__(
        self,
        resolve_hub_url=None,
        resolve_hub_secret=None,
        http_get=None,
        timeout: float = 15.0,
    ) -> None:
        self._resolve_hub_url = resolve_hub_url
        self._resolve_hub_secret = resolve_hub_secret
        self._http_get = http_get or httpx.get
        self._timeout = timeout
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        """Human-readable reason for the most recent failed call."""
        return self._last_error

    def _base(self) -> tuple[str, dict] | None:
        try:
            hub_url = (self._resolve_hub_url() or "").rstrip("/")
        except Exception:
            hub_url = ""
        if not hub_url:
            self._last_error = "No Hub URL configured."
            return None
        try:
            secret = (self._resolve_hub_secret() or "").strip()
        except Exception:
            secret = ""
        headers = {"X-Hub-Secret": secret} if secret else {}
        return hub_url, headers

    def list_recaps(self, soul_id: str, limit: int = 30) -> list[dict[str, Any]]:
        """Recaps for a soul, newest first ([] when offline)."""
        base = self._base()
        if base is None:
            return []
        hub_url, headers = base
        try:
            resp = self._http_get(
                f"{hub_url}/souls/{soul_id}/recaps",
                params={"limit": limit},
                headers=headers,
                timeout=self._timeout,
            )
        except Exception as exc:
            self._last_error = f"Hub unreachable: {exc}"
            return []
        if resp.status_code == 200:
            self._last_error = None
            try:
                items = resp.json().get("recaps", [])
                return list(items) if isinstance(items, list) else []
            except Exception:
                return []
        self._last_error = f"Hub answered {resp.status_code}."
        return []
