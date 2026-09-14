import os
from unittest.mock import MagicMock, patch

import pytest

from client.core import Operator
from client.system.network.client import NetworkClient
from client.system.network.presence import PresenceManager


@pytest.fixture
def mock_env():
    with patch.dict(
        os.environ,
        {"HUB_URL": "http://localhost", "HUB_SECRET_KEY": "test-secret-123"},
    ):
        yield


@pytest.mark.asyncio
async def test_network_client_auth_headers(mock_env):
    client = NetworkClient()
    assert client.headers == {"X-Hub-Secret": "test-secret-123"}


@pytest.mark.asyncio
async def test_network_client_403_handling(mock_env):
    client = NetworkClient()

    mock_response = MagicMock()
    mock_response.status_code = 403

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        res = await client._get("/test")
        assert res is None


@pytest.mark.asyncio
async def test_presence_manager_token_param(mock_env):
    # Dummy callbacks
    async def mock_async_cb(*args, **kwargs):
        pass

    def mock_cb(*args, **kwargs):
        pass

    pm = PresenceManager(
        owner_id="test_owner",
        on_owner_online=mock_async_cb,
        on_owner_offline=mock_cb,
        on_soul_updated=mock_async_cb,
    )
    # PresenceManager uses HUB_URL from env if not passed
    assert "token=test-secret-123" not in pm._ws_url
    assert pm.secret_key == "test-secret-123"
    assert "ws://localhost/ws/test_owner" in pm._ws_url


@pytest.mark.asyncio
async def test_operator_post_mock(mock_env):
    """Verifies that the NetworkClient correctly prepares the Operator post payload."""
    client = NetworkClient()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "status": "success",
        "message_id": "123",
        "cost": 0.0,
    }

    with patch(
        "httpx.AsyncClient.post", return_value=mock_response
    ) as mock_post:
        payload = {
            "author_id": Operator.ID,
            "author_name": Operator.NAME,
            "title": "Broadcast",
            "content": "Hello World",
        }
        res = await client.post_message(payload)
        assert res["status"] == "success"
        assert res["cost"] == 0.0

        # Verify headers were sent
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert kwargs["json"] == payload
