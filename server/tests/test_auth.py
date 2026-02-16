import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect

from ..main import app

# Ensure environment is loaded
load_dotenv()
KEY = os.getenv("HUB_SECRET_KEY", "soulscape-secret-123")

client = TestClient(app)


def test_no_auth_fails():
    response = client.get("/marketplace")
    assert response.status_code == 403
    assert response.json() == {"detail": "Could not validate credentials"}


def test_invalid_auth_fails():
    response = client.get("/marketplace", headers={"X-Hub-Secret": "wrong-key"})
    assert response.status_code == 403
    assert response.json() == {"detail": "Could not validate credentials"}


def test_valid_auth_succeeds():
    response = client.get("/marketplace", headers={"X-Hub-Secret": KEY})
    assert response.status_code == 200


# Check that we can't connect without auth
def test_websocket_no_auth_fails():
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/test_owner") as _:
            pass
    assert exc.value.code == 1008


def test_websocket_valid_auth_succeeds():
    # Token via query param
    with client.websocket_connect(f"/ws/test_owner?token={KEY}") as websocket:
        data = websocket.receive_json()
        assert data["type"] == "connected"
