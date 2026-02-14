import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from soulscape.core.interactions.marketplace import Marketplace
from soulscape.core.interactions.social import MessageBoard
from soulscape.core.soul.soul import Soul
from soulscape.core.stores.remote_store import RemoteStore
from soulscape.system.network.client import NetworkClient


class TestHubV3Integration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Mock dependencies
        self.mock_network_client = AsyncMock(spec=NetworkClient)
        self.mock_store = AsyncMock(spec=RemoteStore)
        # We need to manually link the mock_store to use the mock_client if tested directly,
        # but here we test interactions -> store -> client flow.

        # Reset Singletons for isolation
        MessageBoard._instance = None
        Marketplace._instance = None

        # Initialize Singletons with mock store
        self.message_board = MessageBoard(store=self.mock_store)
        self.marketplace = Marketplace(store=self.mock_store)

        # Setup mock soul
        self.soul_id = "soul-123"
        self.secret = "my-secret-token-v3"
        self.soul_data = {
            "soul_id": self.soul_id,
            "secret": self.secret,
            "biology": {
                "name": "Test Soul",
                "gender": "Non-binary",
                "species": "Spirit",
            },
            # Minimal required fields for Soul init if needed,
            # though we might not instantiate Soul fully if just testing logic usage
        }

    def test_soul_secret_generation(self):
        """Test that a Soul generates a secret if none is provided."""
        # We need to mock dependencies of Soul to instantiate it easily
        # Soul init is complex, let's try to mock the constructor or just check logic
        # based on previous file view.
        # Actually, let's instantiate a Soul with minimal valid data if possible.
        # Soul init requires task_scheduler often, let's mock it.
        mock_scheduler = MagicMock()

        # Case 1: No secret provided
        soul = Soul(
            orb_color_rgb=(0.5, 0.5, 0.5),
            aura_color_rgb=(1.0, 0.5, 0.0),
            task_scheduler=mock_scheduler,
        )
        self.assertTrue(hasattr(soul, "secret"))
        self.assertIsNotNone(soul.secret)
        self.assertTrue(len(soul.secret) > 0)

        # Case 2: Secret provided
        soul2 = Soul(
            orb_color_rgb=(0.5, 0.5, 0.5),
            aura_color_rgb=(1.0, 0.5, 0.0),
            task_scheduler=mock_scheduler,
            secret="preset-secret",
        )
        self.assertEqual(soul2.secret, "preset-secret")

        # Case 3: Persistence
        data = soul2.to_dict()
        self.assertIn("secret", data)
        self.assertEqual(data["secret"], "preset-secret")

        # Case 4: Loading
        soul3 = Soul.from_dict(data, task_scheduler=mock_scheduler)
        self.assertEqual(soul3.secret, "preset-secret")

        # Case 5: Security (No Secret)
        public_data = soul2.to_dict(include_secret=False)
        self.assertNotIn("secret", public_data)

    async def test_network_client_token_header(self):
        """Test that NetworkClient uses the token header when provided."""
        client = NetworkClient(
            base_url="http://test.com", secret_key="global-key"
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_instance = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = (
                mock_client_instance
            )
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json = MagicMock(return_value={"status": "success"})
            mock_client_instance.post.return_value = mock_response

            # Action with token
            await client.post_message({"content": "hello"}, token="soul-secret")

            # Verify call args
            # We expect headers to include X-Hub-Secret: soul-secret
            # and NOT global-key
            call_kwargs = mock_client_cls.call_args[1]
            headers = call_kwargs["headers"]
            self.assertEqual(headers["X-Hub-Secret"], "soul-secret")

            # Action without token
            await client.post_message({"content": "hello"})
            call_kwargs_default = mock_client_cls.call_args_list[1][
                1
            ]  # Second call
            headers_default = call_kwargs_default["headers"]
            self.assertEqual(headers_default["X-Hub-Secret"], "global-key")

    async def test_social_interaction_flow(self):
        """Test that MessageBoard.create_post passes the token correctly."""
        # We assume the caller (SoulAgent or similar) extracts the secret from the Soul
        # and passes it to create_post.

        await self.message_board.create_post(
            author_id="soul-123",
            author_name="Test Soul",
            title="Hello",
            content="World",
            token="soul-secret",
        )

        # Verify store called with token
        self.mock_store.add_post.assert_awaited_once()
        args, kwargs = self.mock_store.add_post.call_args
        self.assertEqual(kwargs.get("token"), "soul-secret")

    async def test_marketplace_interaction_flow(self):
        """Test that Marketplace.add_listing passes the token correctly."""
        mock_item = MagicMock()
        mock_item.to_dict.return_value = {"name": "Test Item"}

        await self.marketplace.add_listing(
            seller_id=123,
            seller_name="Seller Soul",
            item=mock_item,
            price=10.0,
            token="seller-secret",
        )

        # Verify store called with token
        self.mock_store.add_listing.assert_awaited_once()
        args, kwargs = self.mock_store.add_listing.call_args
        self.assertEqual(kwargs.get("token"), "seller-secret")


if __name__ == "__main__":
    unittest.main()
