import asyncio
import queue
import threading
from typing import Any, Dict, List, Optional

from soulscape.system.logger import log
from soulscape.system.network.client import NetworkClient
from soulscape.system.network.presence import PresenceManager


class NetworkService:
    """Manages asynchronous network communication via thread-safe queues.

    This service decouples the high-frequency game loop (Main Thread) from
    the potentially high-latency network I/O (AsyncIO Loop).
    """

    def __init__(self, owner_id: str):
        self.owner_id = owner_id

        # Upstream: Client -> Hub
        # Downstream: Hub -> Client
        self._upstream_queue: asyncio.Queue = asyncio.Queue()
        self._downstream_queue: queue.Queue = queue.Queue()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running: bool = False

        # Sub-services
        self.client = NetworkClient()
        self.presence_manager = PresenceManager(
            owner_id=self.owner_id,
            on_owner_online=self._on_owner_online,
            on_owner_offline=self._on_owner_offline,
            on_soul_updated=self._on_soul_updated,
            on_connect=self._on_connect,
        )

    def start(self) -> None:
        """Starts the background network thread and event loop."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info(f"NetworkService started for owner: {self.owner_id}")

    def stop(self) -> None:
        """Stops the network service."""
        self._running = False
        if self._loop:
            # Schedule the disconnect coroutine safely
            asyncio.run_coroutine_threadsafe(
                self.presence_manager.disconnect(), self._loop
            )
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread:
            self._thread.join(timeout=1.0)
        log.info("NetworkService stopped.")

    def enqueue_update(self, data: Dict[str, Any]) -> None:
        """Non-blocking push of data to the upstream queue (called from Main Thread)."""
        if self._loop and self._running:
            # We use run_coroutine_threadsafe to interact with the async queue from main thread
            asyncio.run_coroutine_threadsafe(
                self._upstream_queue.put(data), self._loop
            )

    def get_events(self) -> List[Dict[str, Any]]:
        """Non-blocking pop of all available events from downstream queue (called from Main Thread)."""
        events = []
        try:
            while True:
                event = self._downstream_queue.get_nowait()
                events.append(event)
        except queue.Empty:
            pass
        return events

    # --- Async Callbacks (Run in Background Loop) ---
    # These push events to the thread-safe queue for the Main Thread to consume

    async def _on_connect(self, online_owners: List[str]) -> None:
        self._downstream_queue.put(
            {"type": "connected", "online_owners": online_owners}
        )

    async def _on_owner_online(self, owner_id: str) -> None:
        self._downstream_queue.put(
            {"type": "owner_online", "owner_id": owner_id}
        )

    def _on_owner_offline(self, owner_id: str) -> None:
        self._downstream_queue.put(
            {"type": "owner_offline", "owner_id": owner_id}
        )

    async def _on_soul_updated(self, souls: List[Dict], owner_id: str) -> None:
        self._downstream_queue.put(
            {"type": "soul_updated", "souls": souls, "owner_id": owner_id}
        )

    # --- Internal Loop Logic ---

    def _run_loop(self) -> None:
        """The entry point for the background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # Start the Presence Manager
        self._loop.create_task(self.presence_manager.connect())

        # Start the Upstream Processor
        self._loop.create_task(self._process_upstream())

        try:
            self._loop.run_forever()
        except Exception as e:
            log.error(f"NetworkService loop crashed: {e}")
        finally:
            self._loop.close()

    async def _process_upstream(self) -> None:
        """Consumes updates from the upstream queue and sends them to the Hub."""
        log.info("NetworkService: Upstream worker started.")
        while self._running:
            try:
                # Get data from async queue
                data = await self._upstream_queue.get()

                # Check message type or just assume soul update?
                # For now we assume typical soul update structure or handle generic types
                # Using PresenceManager.send_update for souls

                if "souls" in data:
                    await self.presence_manager.send_update(data["souls"])

                self._upstream_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"NetworkService: Upstream error: {e}")
                await asyncio.sleep(1.0)  # Backoff
                break
