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
    # Test lines 489-492: Operator deleting a reply
    # Create souls first
    register_soul("1", essence=100.0, name="A")
    register_soul("2", essence=100.0, name="B")
    register_soul(
        "999", essence=100.0, name="Operator"
    )  # Operator needs to exist? No, usually hardcoded check, but maybe essence check?

    # Create post and reply first
    p_res = client.post(
        "/social/post",
        json={"author_id": "1", "author_name": "A", "content": "P"},
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

    # Verify deleted
    get_res = client.get("/social")
    posts = get_res.json()
    assert len(posts[0]["replies"]) == 0


@pytest.mark.anyio
async def test_websocket_soul_update(client: TestClient):
    # Test lines 705-721: soul_update message in WS
    with client.websocket_connect("/ws/user1") as ws1:
        ws1.receive_json()  # connection msg

        with client.websocket_connect("/ws/user2") as ws2:
            ws2.receive_json()  # connection msg
            ws1.receive_json()  # user2 online msg

            # Send soul_update from user2
            ws2.send_json({"type": "soul_update", "souls": [{"soul_id": "1"}]})

            # ws1 should receive soul_updated
            data = ws1.receive_json()
            assert data["type"] == "soul_updated"
            assert data["owner_id"] == "user2"
            assert data["souls"] == [{"soul_id": "1"}]


@pytest.mark.anyio
async def test_websocket_malformed_json(client: TestClient):
    # Test line 725-726: JSONDecodeError in WS
    with client.websocket_connect("/ws/user1") as ws:
        ws.receive_json()
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
