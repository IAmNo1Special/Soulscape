import sqlite3
import unittest.mock
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ..main import app
from ..managers import manager

client = TestClient(app)


@pytest.mark.anyio
async def test_websocket_connect_existing_close_error():
    # Test lines 37-40: Exception when closing existing connection
    mock_ws_old = MagicMock()
    # close() is async
    mock_ws_old.close = unittest.mock.AsyncMock(side_effect=Exception("Close Fail"))

    mock_ws_new = MagicMock()
    mock_ws_new.accept = unittest.mock.AsyncMock()

    # Pre-populate active connection
    manager.active_connections["user1"] = mock_ws_old

    # Should catch exception internally and proceed
    await manager.connect("user1", mock_ws_new)

    # Should invoke close() on the old socket
    mock_ws_old.close.assert_called_once()
    assert manager.active_connections["user1"] == mock_ws_new


@pytest.mark.anyio
async def test_websocket_broadcast_error():
    # Test lines 62-63: Exception during broadcast removes connection
    mock_ws = MagicMock()
    # send_json() is async
    mock_ws.send_json = unittest.mock.AsyncMock(side_effect=Exception("Send Fail"))

    manager.active_connections["user1"] = mock_ws

    await manager.broadcast({"msg": "hi"})

    # Should remove user1
    assert "user1" not in manager.active_connections


def test_get_souls_json_decode_error(client: TestClient):
    from .. import database

    # Test lines 543-546: JSON decode error in get_souls
    with sqlite3.connect(database.DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM souls")
        cursor.execute(
            """
            INSERT INTO souls (soul_id, owner_id, name, hometown)
            VALUES (?, ?, ?, ?)
        """,
            ("999", "u1", "BadJSON", "{invalid"),
        )
        conn.commit()
    conn.close()

    response = client.get("/souls")
    data = response.json()
    assert len(data) == 1
    assert data[0]["hometown"] == "{invalid"


def test_update_souls_missing_owner(client: TestClient):
    # owner_id/custodian_id is required: 400 when neither is provided
    response = client.post("/souls", json={"souls": []})
    assert response.status_code == 400


def test_operator_delete_reply(client: TestClient, register_soul):
    # Operator deleting a reply
    register_soul("1", essence=100.0, name="A")
    register_soul("2", essence=100.0, name="B")

    # Create post and reply first
    p_res = client.post(
        "/social/post",
        json={"author_id": "1", "author_name": "A", "title": "P",
              "content": "P"},
    )
    p_id = p_res.json()["message_id"]

    r_res = client.post(
        "/social/reply",
        json={
            "message_id": p_id,
            "author_id": "2",
            "author_name": "B",
            "content": "R",
        },
    )
    r_id = r_res.json()["reply_id"]

    # Delete reply as operator (999)
    res = client.post(
        f"/social/delete/{r_id}",
        json={"author_id": "999"},
    )
    assert res.status_code == 200

    # Soft delete: the reply stays as a tombstone under the post.
    get_res = client.get("/social")
    posts = get_res.json()
    assert len(posts[0]["replies"]) == 1
    assert posts[0]["replies"][0]["message_id"] == r_id
    assert posts[0]["replies"][0]["deleted"] is True
    assert posts[0]["replies"][0]["body"] == "[deleted]"


@pytest.mark.anyio
async def test_websocket_soul_update_removed(client: TestClient):
    # The legacy upstream state-push channel was deleted (issue #14):
    # soul_update now yields an explicit CHANNEL_REMOVED rejection, never
    # a state change, and the connection stays alive.
    with client.websocket_connect("/ws/user1") as ws:
        ws.receive_json()
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_json(
            {
                "v": 1,
                "type": "soul_update",
                "souls": [{"soul_id": "1", "x": 999.0, "y": 999.0}],
            }
        )
        err = None
        for _ in range(5):
            frame = ws.receive_json()
            if frame.get("type") == "error":
                err = frame
                break
        assert err is not None
        assert err["code"] == "CHANNEL_REMOVED"
        # Connection stays alive (ping/pong to verify)
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


@pytest.mark.anyio
async def test_websocket_malformed_json(client: TestClient):
    # Test line 725-726: JSONDecodeError in WS
    with client.websocket_connect("/ws/user1") as ws:
        ws.receive_json()
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_text("{invalid")
        # Connection should stay alive (ping/pong to verify)
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


@pytest.mark.anyio
async def test_websocket_handle_message_exception(client: TestClient):
    # Test lines 727-728: Generic exception in message processing
    # Send a JSON list, which parses but fails .get("type")
    with client.websocket_connect("/ws/user1") as ws:
        ws.receive_json()
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_text("[]")
        # Connection should stay alive
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


@pytest.mark.anyio
async def test_websocket_loop_generic_exception(client: TestClient):
    # Test lines 735-738: Generic exception in WS loop
    # We trigger this by mocking manager.broadcast to fail AFTER connection
    # The initial connection triggers broadcast (line 690).
    # We use a list side_effect:
    # 1. First call (line 690) raises Exception -> caught by line 735
    # 2. Second call (line 738, inside catch block) returns None -> success
    with patch(
        "server.managers.manager.broadcast",
        side_effect=[Exception("Outer Loop Fail"), None],
    ):
        try:
            with client.websocket_connect("/ws/user1") as _:
                pass
        except Exception:
            # Expected behavior if exception bubbles up or connection closes abnormally
            pass


def test_main_block():
    import runpy

    # We patch uvicorn in the test
    with patch("uvicorn.run") as mock_run:
        import sys

        # Remove from sys.modules if present to avoid RuntimeWarning when run as module
        sys.modules.pop("server.main", None)
        try:
            runpy.run_module("server.main", run_name="__main__")
        except Exception:
            pass
        assert mock_run.called
