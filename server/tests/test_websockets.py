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


def test_ws_intent_sim_down_returns_unavailable_envelope(
    client: TestClient, db_conn, monkeypatch
):
    """Issue #37: sim unreachable -> loud SIM_UNAVAILABLE error envelope.

    No hang, no silent intent loss: the client gets a clear retryable
    error with the nonce echoed.
    """
    from .. import intents as intents_module
    from ..routers import websockets as ws_router
    from ..sim_gateway import SimUnreachable

    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, position, velocity, essence) "
        "VALUES ('wsdown1', 'op1', '[0,0]', '[0,0]', 100.0)"
    )
    db_conn.commit()

    class DeadGateway:
        def submit_intent(self, *args, **kwargs):
            raise SimUnreachable("sim down")

        def tick_status(self):
            raise SimUnreachable("sim down")

    monkeypatch.setattr(ws_router, "gateway_for", lambda *a, **k: DeadGateway())

    with client.websocket_connect("/ws/op1") as ws:
        connected = ws.receive_json()
        assert connected["type"] == "connected"
        assert ws.receive_json()["type"] == "snapshot"
        frame = {
            "v": 1,
            "type": "intent",
            "kind": "move_to",
            "nonce": "nonce-ws-down",
            "session_id": connected["session_id"],
            "soul_id": "wsdown1",
            "x": 10.0,
            "y": 10.0,
        }
        frame["signature"] = intents_module.sign_intent(frame, connected["hmac_key"])
        ws.send_json(frame)
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "SIM_UNAVAILABLE"
        assert err["nonce"] == "nonce-ws-down"
