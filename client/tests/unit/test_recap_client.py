"""Headless tests for the recap Hub client (issue #33)."""

from __future__ import annotations

import unittest

from client.system.recap_client import RecapClient


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class TestRecapClient(unittest.TestCase):
    def test_list_recaps(self):
        seen = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            seen["url"] = url
            seen["params"] = params
            return _Resp(200, {"recaps": [{"day": "2026-09-16", "lines": []}]})

        client = RecapClient(
            resolve_hub_url=lambda: "http://hub:9785",
            resolve_hub_secret=lambda: "s",
            http_get=fake_get,
        )
        recaps = client.list_recaps("s1")
        self.assertEqual(recaps, [{"day": "2026-09-16", "lines": []}])
        self.assertEqual(seen["url"], "http://hub:9785/souls/s1/recaps")
        self.assertIsNone(client.last_error)

    def test_offline_returns_empty(self):
        client = RecapClient(resolve_hub_url=lambda: "")
        self.assertEqual(client.list_recaps("s1"), [])
        self.assertIsNotNone(client.last_error)

    def test_http_error_returns_empty(self):
        client = RecapClient(
            resolve_hub_url=lambda: "http://hub:9785",
            resolve_hub_secret=lambda: "s",
            http_get=lambda *a, **k: _Resp(500),
        )
        self.assertEqual(client.list_recaps("s1"), [])
        self.assertIsNotNone(client.last_error)


if __name__ == "__main__":
    unittest.main()
