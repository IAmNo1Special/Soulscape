"""Agent Bridge Hub client (issue #36).

Thin, testable HTTP wrapper over the Hub bridge endpoints:

- ``GET /bridge/activity`` -> recent bridge events for the tamer,
  newest first (the feed behind the info-card activity log and the
  tray tooltip)

The tamer's Hub URL and secret are resolved lazily at call time (they
can change between sessions); ``http_get`` is injectable so unit tests
can run headless without touching the network.

Degrades to an empty list when the Hub is unreachable or
unconfigured -- the info card simply shows no recent tool activity
rather than crashing the overlay.
"""

from __future__ import annotations

import httpx


class BridgeClient:
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
            secret = self._resolve_hub_secret()
        except Exception:
            secret = ""
        headers = {"X-Hub-Secret": secret} if secret else {}
        return hub_url, headers

    def recent_events(self, limit: int = 5) -> list[dict]:
        """Recent bridge events for the tamer, newest first.

        Returns [] when the Hub is unreachable, unconfigured, or the
        tamer has no events. Summaries arrive <untrusted>-wrapped from
        the Hub; the ``display_summary`` field carries the unwrapped
        text for tamer-facing surfaces.
        """
        base = self._base()
        if base is None:
            return []
        hub_url, headers = base
        try:
            resp = self._http_get(
                f"{hub_url}/bridge/activity",
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
                items = resp.json()
                items = list(items) if isinstance(items, list) else []
            except Exception:
                return []
            for item in items:
                if isinstance(item, dict):
                    summary = str(item.get("summary", ""))
                    item["display_summary"] = (
                        summary.replace("<untrusted>", "").replace(
                            "</untrusted>", ""
                        )
                    )
            return items
        self._last_error = f"Hub answered {resp.status_code}."
        return []
