import asyncio
import concurrent.futures
import re
import sys
import threading
from typing import Any, Dict, List, Optional

from ..core.commands import (
    OwnerPresenceCommand,
    PresenceReconcileCommand,
    StateUpdateCommand,
    ViewportFrameCommand,
)
from .command_queue import CommandQueue
from .logger import log
from .network.client import NetworkClient
from .network.presence import PresenceManager
from .presence import (
    PresencePipeline,
    PresenceRedactor,
    build_sampler,
    get_app_category_opt_in,
)


class NetworkService:
    """Manages asynchronous network communication via thread-safe queues.

    This service decouples the high-frequency game loop (Main Thread) from
    the potentially high-latency network I/O (AsyncIO Loop).
    """

    def __init__(self, owner_id: str, viewport_consumer: Any = None):
        self.owner_id = owner_id
        self.viewport_consumer = viewport_consumer

        # Downstream: Hub -> Client (Commands)
        self.command_queue: CommandQueue = CommandQueue()

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
            on_viewport_frame=self._on_viewport_frame,
        )
        # #28: tamer presence uplink (started in start(); online-only).
        self._presence_pipeline: Optional[PresencePipeline] = None

    def _is_valid_owner(self, owner_id: str) -> bool:
        """Validates that an owner_id is safe and well-formed."""
        if not owner_id or not isinstance(owner_id, str):
            return False
        return bool(re.match(r"^[a-zA-Z0-9_-]+$", owner_id))

    def start(self) -> None:
        """Starts the background network thread and event loop."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._start_presence_pipeline()
        log.info(f"NetworkService started for owner: {self.owner_id}")

    def _start_presence_pipeline(self) -> None:
        """Start the #28 tamer-presence uplink (online mode only).

        This service only runs in online mode (SoulscapeApp starts it
        exclusively on the online path), so presence is never sampled or
        shipped offline. The real sampler is Windows-only; other
        platforms log and skip rather than fabricate signals.
        """
        try:
            sampler = build_sampler()
        except Exception as exc:
            log.warning(f"Presence pipeline disabled: {exc}")
            return
        if sys.platform != "win32":
            log.info(
                "Presence pipeline idle: no platform sampler "
                "(Windows-only); nothing sampled or shipped."
            )
            return
        redactor = PresenceRedactor(
            sampler, app_category_opt_in=get_app_category_opt_in()
        )
        self._presence_pipeline = PresencePipeline(
            redactor,
            send=self.send_intent,
            get_soul_id=lambda: self.owner_id,
        )
        self._presence_pipeline.start()
        log.info("Tamer presence pipeline started (change + 60s heartbeat).")

    def stop(self) -> None:
        """Stops the network service."""
        self._running = False
        if self._presence_pipeline is not None:
            try:
                self._presence_pipeline.stop()
            except Exception:
                pass
            self._presence_pipeline = None
        if self._loop:
            # We schedule the formal shutdown coroutine in the loop
            asyncio.run_coroutine_threadsafe(self._shutdown_async(), self._loop)

        if self._thread:
            # Wait for the thread to actually finish
            self._thread.join(timeout=2.0)
        log.info("NetworkService stopped.")

    async def _shutdown_async(self) -> None:
        """Coroutine to perform formal shutdown operations in the loop."""
        try:
            # 1. Disconnect presence (WebSocket)
            await self.presence_manager.disconnect()
            # 2. Close HTTP client
            await self.client.close()
        except Exception as e:
            log.error(f"Error during network shutdown: {e}")
        finally:
            # 2. Stop the loop once everything is closed
            if self._loop:
                self._loop.stop()

    def send_intent(
        self, kind: str, soul_id: str, **fields: Any
    ) -> concurrent.futures.Future | None:
        if not self._running or not self._loop:
            log.warning("send_intent dropped: network service not running")
            return None
        return asyncio.run_coroutine_threadsafe(
            self.presence_manager.send_intent(kind, soul_id, **fields),
            self._loop,
        )

    # --- Async Callbacks (Run in Background Loop) ---
    # These push events to the thread-safe queue for the Main Thread to consume

    async def _on_connect(self, online_owners: List[str]) -> None:
        # Reconcile all remote souls at once instead of individual online events
        valid_owners = [o for o in online_owners if self._is_valid_owner(o)]
        self.command_queue.put(PresenceReconcileCommand(online_owners=valid_owners))

    async def _on_owner_online(self, owner_id: str) -> None:
        if not self._is_valid_owner(owner_id):
            log.warning(f"Invalid owner_id received in online event: {owner_id}")
            return

        self.command_queue.put(OwnerPresenceCommand(owner_id=owner_id, action="online"))

        try:
            # Fetch remote souls directly here
            souls_data = await self.client.get_souls_by_owner(owner_id)
            if souls_data:
                self.command_queue.put(
                    StateUpdateCommand(souls_data=souls_data, owner_id=owner_id)
                )
        except Exception as e:
            log.error(f"Error fetching remote souls for {owner_id}: {e}")

    def _on_owner_offline(self, owner_id: str) -> None:
        if not self._is_valid_owner(owner_id):
            return
        self.command_queue.put(
            OwnerPresenceCommand(owner_id=owner_id, action="offline")
        )

    async def _on_soul_updated(self, souls: List[Dict], owner_id: str) -> None:
        if not self._is_valid_owner(owner_id):
            return
        self.command_queue.put(StateUpdateCommand(souls_data=souls, owner_id=owner_id))

    async def _on_viewport_frame(self, frame: Dict[str, Any]) -> None:
        if self.viewport_consumer is not None:
            self.command_queue.put(
                ViewportFrameCommand(consumer=self.viewport_consumer, frame=frame)
            )

    # --- Internal Loop Logic ---

    def _run_loop(self) -> None:
        """The entry point for the background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # Start the Presence Manager
        self._loop.create_task(self.presence_manager.connect())

        try:
            self._loop.run_forever()
        except Exception as e:
            log.error(f"NetworkService loop crashed: {e}")
        finally:
            self._loop.close()
