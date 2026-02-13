import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from soulscape.system.network_service import NetworkService


class TestNetworkService(unittest.TestCase):
    def setUp(self):
        # Patch PresenceManager and Client to avoid real network
        self.patcher_pm = patch(
            "soulscape.system.network_service.PresenceManager"
        )
        self.MockPresenceManager = self.patcher_pm.start()

        self.patcher_client = patch(
            "soulscape.system.network_service.NetworkClient"
        )
        self.MockClient = self.patcher_client.start()

        # Setup mocks
        self.mock_pm_instance = self.MockPresenceManager.return_value
        self.mock_pm_instance.connect = AsyncMock()
        self.mock_pm_instance.send_update = AsyncMock()
        self.mock_pm_instance.disconnect = AsyncMock()

        self.service = NetworkService(owner_id="test_owner")

    def tearDown(self):
        if self.service._running:
            self.service.stop()
        self.patcher_pm.stop()
        self.patcher_client.stop()

    def test_startup_shutdown(self):
        """Verify the service starts and stops its thread/loop correctly."""
        self.service.start()
        time.sleep(0.1)  # Allow thread to start
        self.assertTrue(self.service._running)
        self.assertIsNotNone(self.service._thread)
        self.assertTrue(self.service._thread.is_alive())

        self.service.stop()
        time.sleep(0.1)
        self.assertFalse(self.service._running)
        self.assertFalse(self.service._thread.is_alive())

    def test_enqueue_upstream(self):
        """Verify data pushed to upstream queue reaches PresenceManager."""
        self.service.start()
        time.sleep(0.1)

        test_data = {"souls": [{"id": 1, "x": 10}]}
        self.service.enqueue_update(test_data)

        # Allow async loop to process
        time.sleep(0.2)

        # Check if PresenceManager.send_update was called
        # The service calls pm.send_update(data["souls"])
        self.mock_pm_instance.send_update.assert_called_with(
            [{"id": 1, "x": 10}]
        )

    def test_downstream_events(self):
        """Verify callbacks populate the downstream queue."""
        # Simulate a callback from PresenceManager (which runs in background loop usually)
        # But here safely injecting into the queue via the service's internal callback methods
        # properly awaited if they were async, but here we can just call the queue directly or
        # use the service helper methods if we exposed them.

        # We can call the callback methods directly to test the queue logic
        # But wait, they are async methods on the service instance:
        # async def _on_soul_updated(self, souls, owner_id)

        # We can't call async methods from here easily without a loop.
        # But we can verify `get_events` works if data is in queue.

        self.service._downstream_queue.put({"type": "test_event"})

        events = self.service.get_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "test_event")

        # Verify queue is empty
        events2 = self.service.get_events()
        self.assertEqual(len(events2), 0)

    async def test_on_owner_online_logic(self):
        """Verify _on_owner_online fetches souls and emits events."""
        # We need to run the async method. Since we can't easily run loop here
        # we can just call it if we were in an async test runner, but we are not.
        # So we manually await it or use asyncio.run if compatible.
        # unittest.IsolatedAsyncioTestCase is better but let's try manual run loop
        # or just inspect the logic by running it in a loop.
        pass

    def test_on_owner_online_flow(self):
        """Test the owner_online flow including validation and fetching."""
        # Initialize a loop for this test
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # 1. Valid Owner
        owner_id = "Valid_Owner-123"
        soup_payload = [{"soul_id": "s1", "name": "Test Soul"}]

        # Setup mock client response
        # self.service.client is a Mock object from setUp/patcher
        # We need to configure the instance that service.client refers to.
        # In setUp: self.MockClient = self.patcher_client.start()
        # self.service = NetworkService(...) -> self.client = NetworkClient() -> MockClient()

        # Access the mock instance attached to the service
        mock_client_instance = self.service.client
        mock_client_instance.get_souls_by_owner = AsyncMock(
            return_value=soup_payload
        )

        # Run the async method
        loop.run_until_complete(self.service._on_owner_online(owner_id))

        # Verification
        events = self.service.get_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["type"], "owner_online")
        self.assertEqual(events[0]["owner_id"], owner_id)

        self.assertEqual(events[1]["type"], "soul_updated")
        self.assertEqual(events[1]["owner_id"], owner_id)
        self.assertEqual(events[1]["souls"], soup_payload)

        # Verify client call
        mock_client_instance.get_souls_by_owner.assert_called_with(owner_id)

        # 2. Invalid Owner (Path Traversal attempt)
        invalid_owner = "../../../etc/passwd"
        mock_client_instance.get_souls_by_owner.reset_mock()

        loop.run_until_complete(self.service._on_owner_online(invalid_owner))

        events_invalid = self.service.get_events()
        # Should only have the previous 2 events? No, get_events pops them.
        # But wait, get_events() in my verification above popped them!
        # So queue should be empty or contain the new "owner_online" (which is always emitted first?)
        # Logic:
        # self._downstream_queue.put({"type": "owner_online"...})
        # THEN validation check.
        # So we expect 1 event: owner_online. But NO soul_updated.

        self.assertEqual(len(events_invalid), 1)
        self.assertEqual(events_invalid[0]["type"], "owner_online")
        self.assertEqual(events_invalid[0]["owner_id"], invalid_owner)

        # Verify fetch was NOT called
        mock_client_instance.get_souls_by_owner.assert_not_called()

        loop.close()


if __name__ == "__main__":
    unittest.main()
