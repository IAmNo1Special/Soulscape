"""Tests for the Agent Bridge Hub client (issue #36).

Headless: BridgeClient takes an injectable http_get, so no network is
touched.
"""

from __future__ import annotations

import unittest

from client.system.bridge_client import BridgeClient


class FakeResponse:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _client(http_get, hub_url="http://hub:9785", secret="secret"):
    return BridgeClient(
        resolve_hub_url=lambda: hub_url,
        resolve_hub_secret=lambda: secret,
        http_get=http_get,
    )


class TestRecentEvents(unittest.TestCase):
    def test_returns_items_newest_first_with_display_summary(self):
        payload = [
            {
                "event_id": "evt_2",
                "soul_id": "soul-a",
                "source_id": "ci",
                "kind": "test_failed",
                "summary": "<untrusted>3 specs red</untrusted>",
                "ref": "run-2",
                "created_at": 200.0,
            },
            {
                "event_id": "evt_1",
                "soul_id": "soul-a",
                "source_id": "ci",
                "kind": "note",
                "summary": "<untrusted>plain note</untrusted>",
                "ref": None,
                "created_at": 100.0,
            },
        ]
        calls = []

        def http_get(url, params=None, headers=None, timeout=None):
            calls.append((url, params))
            return FakeResponse(200, payload)

        client = _client(http_get)
        items = client.recent_events(limit=3)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["event_id"], "evt_2")
        self.assertEqual(items[0]["display_summary"], "3 specs red")
        self.assertEqual(items[1]["display_summary"], "plain note")
        self.assertNotIn("<untrusted>", items[0]["display_summary"])
        self.assertEqual(calls[0][0], "http://hub:9785/bridge/activity")
        self.assertEqual(calls[0][1], {"limit": 3})
        self.assertIsNone(client.last_error)

    def test_empty_when_unconfigured(self):
        client = BridgeClient(
            resolve_hub_url=lambda: "",
            resolve_hub_secret=lambda: "s",
            http_get=lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("no network")
            ),
        )
        self.assertEqual(client.recent_events(), [])
        self.assertIsNotNone(client.last_error)

    def test_empty_when_unreachable(self):
        def boom(*args, **kwargs):
            raise ConnectionError("down")

        client = _client(boom)
        self.assertEqual(client.recent_events(), [])
        self.assertIn("unreachable", client.last_error)

    def test_empty_on_http_error(self):
        client = _client(lambda *a, **k: FakeResponse(403, {}))
        self.assertEqual(client.recent_events(), [])
        self.assertIn("403", client.last_error)

    def test_empty_on_malformed_payload(self):
        client = _client(lambda *a, **k: FakeResponse(200, {"nope": 1}))
        self.assertEqual(client.recent_events(), [])


if __name__ == "__main__":
    unittest.main()
