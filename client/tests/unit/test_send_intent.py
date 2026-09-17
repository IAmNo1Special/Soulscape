import asyncio
import hashlib
import hmac
import json
import os
from unittest.mock import patch

import pytest

from client.system.network.presence import (
    PresenceManager,
    _intent_canonical,
)


@pytest.fixture
def mock_env():
    with patch.dict(
        os.environ,
        {"HUB_URL": "http://localhost", "HUB_SECRET_KEY": "test-secret-123"},
    ):
        yield


def _make_pm():
    async def _noop(*args, **kwargs):
        pass

    def _sync_noop(*args, **kwargs):
        pass

    return PresenceManager(
        owner_id="tamer1",
        on_owner_online=_noop,
        on_owner_offline=_sync_noop,
        on_soul_updated=_noop,
    )


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)


@pytest.mark.asyncio
async def test_send_intent_signs_and_acks(mock_env):
    pm = _make_pm()
    pm._ws = FakeWs()
    pm._session_id = "sess-1"
    pm._hmac_key = "key-abc"

    async def _answer():
        await asyncio.sleep(0)
        nonce = json.loads(pm._ws.sent[0])["nonce"]
        pm._resolve_ack(
            nonce,
            {
                "type": "intent_ack",
                "nonce": nonce,
                "intent_id": "int-1",
                "status": "accepted",
            },
        )

    task = asyncio.ensure_future(_answer())
    ack = await pm.send_intent("move_to", "soul-1", x=10.0, y=20.0)
    await task

    assert ack["status"] == "accepted"
    assert ack["intent_id"] == "int-1"
    frame = json.loads(pm._ws.sent[0])
    assert frame["type"] == "intent"
    assert frame["kind"] == "move_to"
    assert frame["soul_id"] == "soul-1"
    assert frame["x"] == 10.0 and frame["y"] == 20.0
    assert frame["session_id"] == "sess-1"
    assert frame["nonce"]
    expected = hmac.new(
        b"key-abc", _intent_canonical(frame).encode(), hashlib.sha256
    ).hexdigest()
    assert frame["signature"] == expected


@pytest.mark.asyncio
async def test_send_intent_dropped_without_session(mock_env):
    pm = _make_pm()
    pm._ws = FakeWs()
    assert await pm.send_intent("move_to", "soul-1", x=1.0, y=2.0) is None
    assert pm._ws.sent == []


@pytest.mark.asyncio
async def test_send_intent_timeout_returns_none(mock_env):
    pm = _make_pm()
    pm._ws = FakeWs()
    pm._session_id = "sess-1"
    pm._hmac_key = "key-abc"
    with patch("client.system.network.presence.INTENT_ACK_TIMEOUT", 0.01):
        assert await pm.send_intent("move_to", "soul-1", x=1.0, y=2.0) is None
    assert pm._pending_acks == {}


@pytest.mark.asyncio
async def test_connected_frame_stores_signing_session(mock_env):
    pm = _make_pm()

    async def _noop(*args, **kwargs):
        pass

    messages = [
        json.dumps(
            {
                "v": 1,
                "type": "connected",
                "session_id": "sess-9",
                "hmac_key": "hk-9",
                "online_owners": [],
            }
        ),
    ]

    async def _gen():
        for message in messages:
            yield message

    await pm._listen(_gen())
    assert pm._session_id == "sess-9"
    assert pm._hmac_key == "hk-9"


@pytest.mark.asyncio
async def test_error_frame_resolves_pending_ack(mock_env):
    pm = _make_pm()
    pm._ws = FakeWs()
    pm._session_id = "sess-1"
    pm._hmac_key = "key-abc"

    async def _answer():
        await asyncio.sleep(0)
        nonce = json.loads(pm._ws.sent[0])["nonce"]
        pm._resolve_ack(
            nonce, {"type": "error", "code": "CUSTODY_DENIED", "nonce": nonce}
        )

    task = asyncio.ensure_future(_answer())
    ack = await pm.send_intent("move_to", "soul-9", x=1.0, y=2.0)
    await task
    assert ack["type"] == "error"
    assert ack["code"] == "CUSTODY_DENIED"
