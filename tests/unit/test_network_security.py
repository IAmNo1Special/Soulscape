import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from soulscape.system.network.client import NetworkClient
from soulscape.system.network.presence import PresenceManager


@pytest.fixture
def mock_env():
    # Use patch.dict to safely modify os.environ
    with patch.dict(
        os.environ,
        {"HUB_URL": "http://refined-hub", "HUB_SECRET_KEY": "refined_secret"},
    ):
        yield


@pytest.mark.asyncio
async def test_network_client_refined_config(mock_env):
    client = NetworkClient()
    assert client.base_url == "http://refined-hub"
    assert client.headers == {"X-Hub-Secret": "refined_secret"}


@pytest.mark.asyncio
async def test_network_client_detail_error_log(mock_env):
    client = NetworkClient()

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"detail": "Specific error detail"}
        mock_get.return_value = mock_response

        # Use patch to capture log calls
        with patch(
            "soulscape.system.network.client.log.error"
        ) as mock_log_error:
            result = await client._get("/test")
            assert result is None
            mock_log_error.assert_called_with(
                "Hub GET error (400) at /test: Specific error detail"
            )


@pytest.mark.asyncio
async def test_presence_manager_refined_url(mock_env):
    # Dummy callbacks
    async def on_online(oid):
        pass

    def on_offline(oid):
        pass

    async def on_update(souls, oid):
        pass

    pm = PresenceManager(
        owner_id="test_owner",
        on_owner_online=on_online,
        on_owner_offline=on_online,
        on_soul_updated=on_update,
    )

    assert pm._ws_url == "ws://refined-hub/ws/test_owner?token=refined_secret"
