"""Mailbag Hub client (issue #32).

Thin, testable HTTP wrapper over the Hub mailbag endpoints:

- ``GET /mailbag/count`` -> pending count for the tray badge
- ``GET /mailbag/pending`` -> pending questions for the answer surface
- ``POST /mailbag/{question_id}/answer`` -> post the tamer's answer

The tamer's Hub URL and secret are resolved lazily at call time (they
can change between sessions); ``http_get``/``http_post`` are injectable
so unit tests can run headless without touching the network.

Every method degrades to an empty/error result when the Hub is
unreachable or unconfigured -- the tray badge reads 0 and the answer
surface shows an empty list rather than crashing the overlay.
"""

from __future__ import annotations

import time

import httpx


class MailbagClient:
    def __init__(
        self,
        resolve_hub_url=None,
        resolve_hub_secret=None,
        http_get=None,
        http_post=None,
        timeout: float = 15.0,
    ) -> None:
        self._resolve_hub_url = resolve_hub_url
        self._resolve_hub_secret = resolve_hub_secret
        self._http_get = http_get or httpx.get
        self._http_post = http_post or httpx.post
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
        secret = ""
        try:
            secret = self._resolve_hub_secret() or ""
        except Exception:
            secret = ""
        headers = {"X-Hub-Secret": secret} if secret else {}
        return hub_url, headers

    def count(self) -> int:
        """Pending-question count for the tray badge (0 when offline)."""
        base = self._base()
        if base is None:
            return 0
        hub_url, headers = base
        try:
            resp = self._http_get(
                f"{hub_url}/mailbag/count",
                headers=headers,
                timeout=self._timeout,
            )
        except Exception as exc:
            self._last_error = f"Hub unreachable: {exc}"
            return 0
        if resp.status_code == 200:
            self._last_error = None
            try:
                return int(resp.json().get("pending", 0))
            except Exception:
                return 0
        self._last_error = f"Hub answered {resp.status_code}."
        return 0

    def pending(self) -> list[dict]:
        """Pending questions for the answer surface ([] when offline)."""
        base = self._base()
        if base is None:
            return []
        hub_url, headers = base
        try:
            resp = self._http_get(
                f"{hub_url}/mailbag/pending",
                headers=headers,
                timeout=self._timeout,
            )
        except Exception as exc:
            self._last_error = f"Hub unreachable: {exc}"
            return []
        if resp.status_code == 200:
            self._last_error = None
            try:
                items = resp.json().get("questions", [])
                return list(items) if isinstance(items, list) else []
            except Exception:
                return []
        self._last_error = f"Hub answered {resp.status_code}."
        return []

    def answer(self, question_id: str, text: str) -> dict:
        """Post the tamer's answer for a question.

        Returns a dict with ``status`` ("answered" | "error") and,
        on success, the Hub's record (question_id, loyalty_delta, ...).
        """
        base = self._base()
        if base is None:
            return {"status": "error", "message": self._last_error}
        hub_url, headers = base
        started = time.time()
        try:
            resp = self._http_post(
                f"{hub_url}/mailbag/{question_id}/answer",
                json={"answer": text},
                headers=headers,
                timeout=self._timeout,
            )
        except Exception as exc:
            self._last_error = f"Hub unreachable: {exc}"
            return {"status": "error", "message": self._last_error}
        elapsed_ms = int((time.time() - started) * 1000)
        if resp.status_code == 200:
            self._last_error = None
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            return {"status": "answered", "client_ms": elapsed_ms, **payload}
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        message = (
            detail
            if isinstance(detail, str)
            else f"Hub answered {resp.status_code}."
        )
        self._last_error = message
        return {"status": "error", "message": message}
