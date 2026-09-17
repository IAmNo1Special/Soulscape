from fastapi.testclient import TestClient

from ..managers import manager


def test_websocket_connect(client: TestClient):
    with client.websocket_connect("/ws/user1") as websocket:
        data = websocket.receive_json()
        assert data["type"] == "connected"
        assert data["v"] == 1
        assert "online_owners" in data


def test_websocket_messages_carry_protocol_version(client: TestClient):
    with client.websocket_connect("/ws/user1") as ws1:
        ws1.receive_json()
        assert ws1.receive_json()["type"] == "snapshot"
        with client.websocket_connect("/ws/user2") as ws2:
            ws2.receive_json()
            assert ws2.receive_json()["type"] == "snapshot"
            data = ws1.receive_json()
            assert data["type"] == "owner_online"
            assert data["v"] == 1
            assert data["owner_id"] == "user2"


def test_websocket_broadcast(client: TestClient):
    with client.websocket_connect("/ws/user1") as ws1:
        # Establish first connection
        ws1.receive_json()
        assert ws1.receive_json()["type"] == "snapshot"

        with client.websocket_connect("/ws/user2") as ws2:
            # Second user connects
            ws2.receive_json()
            assert ws2.receive_json()["type"] == "snapshot"

            # First user should receive notification
            data = ws1.receive_json()
            assert data["type"] == "owner_online"
            assert data["owner_id"] == "user2"


def test_websocket_disconnect(client: TestClient):
    with client.websocket_connect("/ws/user1") as ws1:
        ws1.receive_json()

    # After block exit, user1 disconnects
    assert "user1" not in manager.active_connections


def test_websocket_ping_pong(client: TestClient):
    with client.websocket_connect("/ws/user1") as ws:
        ws.receive_json()
        assert ws.receive_json()["type"] == "snapshot"
        ws.send_text("ping")
        data = ws.receive_text()
        assert data == "pong"
