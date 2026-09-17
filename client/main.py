"""Unified Overlay Application for Soulscape.

Manages a single transparent full-screen window and renders multiple souls within it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import random
import sys
import threading
import time
import winreg
from multiprocessing import Process, Queue, queues
from pathlib import Path
from typing import Any, Coroutine

import httpx
import pyglet
from dotenv import load_dotenv
from pyglet.window import key

from .constants import SOUL_HEIGHT, SOUL_WIDTH
from .core import MessageBoard, Soul
from .core.soul import physics as soul_physics
from .core.commands import ViewportFrameCommand
from .system.input_router import InputRouter
from .system.location import whereabouts_label
from .system.logger import log, setup_logging
from .system.network.viewport_client import (
    ViewportConsumer,
    ViewportMapper,
    is_statue,
    viewport_mode_enabled,
)
from .core.interactions.pet_gestures import (
    CARRY_BEGIN,
    CARRY_END,
    CARRY_MOVE,
    CHIRP,
    PET,
    PetGestureDetector,
    gesture_intent,
)
from .core.interactions.pet_card import build_info_card, presence_status
from .system.network_service import NetworkService
from .system.noise import NoisePolicy
from .system.bubble_config import load_bubble_config, save_bubble_config
from .system.persistence import (
    get_client_mode,
    load_settings,
    load_souls,
    resolve_hub_url,
    save_settings,
)
from .system.tray import TrayController
from .system.window_manager import get_cursor_pos, get_window_manager
from .system.dirty_tracker import DirtyTracker
from .system.dpi import declare_per_monitor_v2_dpi_awareness
from .system.fullscreen import foreground_is_exclusive_fullscreen
from .ui.graphics.scene_renderer import SceneRenderer
from .ui.graphics.visual_reflexes import VisualReflexController
from .ui.bubbles import (
    BUBBLE_KINDS,
    KIND_MAILBAG,
    KIND_MORNING_NOTE,
    Bubble,
    BubbleManager,
)
from .system.mailbag_client import MailbagClient
from .system.recap_client import RecapClient
from .ui.gui.gui_service import GuiCommand, run_gui_service

# Explicitly load dotenv
env_path = Path.cwd() / ".env"
load_dotenv(dotenv_path=env_path, override=True)


# Set up logging using the project's utility
setup_logging(level=logging.DEBUG)
# Suppress noisy library logs even in debug mode
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("pyglet").setLevel(logging.WARNING)


class SoulscapeApp:
    """Main application class for Soulscape."""

    def __init__(self) -> None:
        self.active_souls: list[Soul] = []
        self.window_manager: Any = None
        self.overlay_window: Any = None
        self.tray_controller: Any = None
        self.scene_renderer: Any = None
        self.input_router: Any = None
        # Issue #29: water-cooler reflexes (unlock greeting, long-idle
        # nap, input-burst reaction + typing-dip). Local-only.
        self.reflex_controller: VisualReflexController | None = None

        # Issue #30: speech bubbles + noise policy. Config survives
        # restarts via soulscape_bubbles.toml; the policy object is the
        # live gate every show_bubble call passes through.
        self.bubble_config = load_bubble_config()
        self.noise_policy = NoisePolicy(self.bubble_config.noise)
        self.bubble_manager = BubbleManager(
            policy=self.noise_policy,
            durations=self.bubble_config.display.durations,
            max_visible_per_soul=self.bubble_config.display.max_visible_per_soul,
            queue_depth=self.bubble_config.display.queue_depth,
        )
        # Issue #32: tapping a mailbag bubble opens the answer surface.
        self.bubble_manager.set_tap_handler(self._on_bubble_tap)
        # Issue #32: Hub client for the mailbag badge + answer surface.
        self.mailbag_client = MailbagClient(
            resolve_hub_url=resolve_hub_url,
            resolve_hub_secret=lambda: os.getenv("HUB_SECRET_KEY", ""),
        )
        self._mailbag_count_cache: tuple[float, int] = (0.0, 0)
        # Issue #33: Hub client for the morning-recap dashboard view.
        self.recap_client = RecapClient(
            resolve_hub_url=resolve_hub_url,
            resolve_hub_secret=lambda: os.getenv("HUB_SECRET_KEY", ""),
        )
        self._recaps_cache: tuple[float, dict] = (0.0, {})
        # Pause toggle (tray): freezes local sim + reflex visuals.
        self.sim_paused: bool = False

        # Load settings
        self.saved_settings: dict[str, Any] = load_settings()
        self.global_opacity: int = self.saved_settings.get("opacity", 80)
        self.global_aura_visible: bool = self.saved_settings.get("aura_visible", True)
        self.run_on_startup: bool = self._check_startup_registry()
        self.instance_id: str = self.saved_settings.get("instance_id", "unknown")
        self.next_soul_id: int = 1

        self.gui_command_queue: Any = None
        self.gui_result_queue: Any = None
        self.gui_process: Any = None

        self.last_save_time: float = time.time()
        self.last_topmost_time: float = time.time()
        self._is_saving_souls: bool = False  # Lock for background saves

        # Dirty-driven render loop: scene only redraws when flagged dirty.
        self.dirty_tracker = DirtyTracker()
        self._overlay_parked: bool = False
        self._last_render_snapshot: list | None = None

        # Unified Network Service
        self.viewport_mode: bool = viewport_mode_enabled()
        self.viewport_consumer = ViewportConsumer() if self.viewport_mode else None
        self.viewport_mapper = ViewportMapper() if self.viewport_mode else None
        # Pet grammar (issue #31): gesture detector + press/card state.
        # Viewport input is affection/attention only -- no move_to.
        self._pet_gestures = PetGestureDetector() if self.viewport_mode else None
        self._pet_press_soul_id: str | None = None
        self._info_card_soul_id: str | None = None
        self._info_card_xy: tuple[float, float] | None = None
        self.network_service = NetworkService(
            owner_id=self.instance_id, viewport_consumer=self.viewport_consumer
        )

        # Persistent Background Event Loop for non-UI tasks (saves, HTTP)
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._loop_thread.start()
        self._running: bool = False

    def _run_event_loop(self) -> None:
        """Runs the background asyncio event loop."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coro(self, coro: Coroutine) -> asyncio.Future:
        """Thread-safe submission of coroutines to the background loop."""
        if not self._loop.is_running():
            log.warning("Attempted to run coro but loop is not running.")
            f = asyncio.Future()
            f.cancel()
            return f
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def run(self) -> None:
        """Starts the application."""
        self._running = True
        log.info("Starting Application...")

        # Initialize Persistent GUI Service
        self.gui_command_queue = Queue()
        self.gui_result_queue = Queue()

        self.gui_process: Process = Process(
            target=run_gui_service,
            args=(self.gui_command_queue, self.gui_result_queue),
            daemon=True,
        )
        self.gui_process.start()
        log.info("GUI Service started.")

        # 1. Setup Window Manager (Creates Overlay Window)
        if sys.platform == "win32":
            declare_per_monitor_v2_dpi_awareness()
        self.window_manager = get_window_manager()
        self.overlay_window = self.window_manager.window

        # Apply initial opacity
        self.window_manager.set_opacity(self.global_opacity)

        # 2b. Initialize Scene Renderer and Input Router
        self.scene_renderer = SceneRenderer()
        self.input_router = InputRouter()
        self.reflex_controller = VisualReflexController()

        # Apply initial aura visibility
        self.scene_renderer.aura_visible = self.global_aura_visible

        # 3. Setup Input Handling
        self._setup_window_events()
        self._gate_draw_on_dirty()

        # 4. Load Content
        if self.viewport_mode:
            log.info("Viewport mode: souls render from the Hub stream.")
        else:
            self.load_initial_souls()

        # 5. Start Network Service
        if self.viewport_mode:
            hub_url = resolve_hub_url()
            if not hub_url:
                raise RuntimeError(
                    "Soulscape is in Online (Hub) mode but no Hub URL is "
                    "configured. Set HUB_URL or the Hub URL in Settings. "
                    "Refusing to silently fall back to Offline."
                )
            self.network_service.start()
            # Force initial secure sync of loaded souls (HTTP)
            self.initial_hub_sync()

        self.window_manager.show_window()
        self._setup_tray()
        log.debug("Entering main loop.")

        pyglet.clock.schedule_interval(lambda dt: self.check_gui_results(), 0.1)

        pyglet.clock.schedule_interval(self.update_souls, 1 / 60.0)

        if sys.platform == "win32":
            pyglet.clock.schedule_interval(self._overlay_housekeeping, 1.0 / 12.0)

        try:
            pyglet.app.run()
        finally:
            # Robust final cleanup
            self.quit_app()

    def _setup_window_events(self) -> None:
        """Sets up Pyglet window event handlers."""

        @self.overlay_window.event
        def on_draw() -> None:
            self.overlay_window.clear()
            self.scene_renderer.render(self.active_souls, self.overlay_window.height)
            positions = self._soul_screen_positions()
            jobs = self.bubble_manager.layout(positions)
            if jobs:
                self.scene_renderer.render_bubbles(jobs)
            if self.viewport_mode and self.viewport_consumer is not None:
                self._draw_viewport_pet_overlays()

        @self.overlay_window.event
        def on_key_press(symbol, modifiers) -> None:
            if symbol == key.A:
                self.toggle_auras()
            elif symbol == key.ESCAPE:
                self.quit_app()

        # Register input router handlers
        self.overlay_window.push_handlers(self.input_router)

        @self.overlay_window.event
        def on_mouse_press(x: int, y: int, button: int, modifiers: int) -> bool | None:
            """Pyglet event handler for mouse press events."""
            # Issue #32: bubble taps win over soul clicks -- a mailbag
            # bubble opens the answer surface and swallows the click.
            if button == pyglet.window.mouse.LEFT:
                tapped = self.bubble_manager.tap_at(
                    x, y, self._soul_screen_positions()
                )
                if tapped is not None:
                    return True
            # Use InputRouter to find target soul
            target_soul = self.input_router.get_soul_at(
                self.active_souls, x, y, self.overlay_window.height
            )

            if target_soul:
                if self.viewport_mode:
                    # Pet grammar (issue #31): left press starts gesture
                    # tracking; right press toggles the info card. No
                    # direct commands -- move_to is gone from viewport.
                    soul_id = target_soul.biology.soul_id
                    if button == pyglet.window.mouse.LEFT:
                        self._pet_press_soul_id = soul_id
                        if self._pet_gestures is not None:
                            self._pet_gestures.press(x, y)
                    elif button == pyglet.window.mouse.RIGHT:
                        if self._info_card_soul_id == soul_id:
                            self._info_card_soul_id = None
                            self._info_card_xy = None
                        else:
                            self._info_card_soul_id = soul_id
                            self._info_card_xy = (x, y)
                        self.dirty_tracker.mark_dirty()
                else:
                    # Only allow interaction if we own this soul
                    if target_soul.owner_id != self.instance_id:
                        return None

                    # Dispatch to target
                    target_soul.on_mouse_press(
                        x, y, button, modifiers, self.overlay_window.height
                    )
                    # Store as dragged soul if left click
                    if button == pyglet.window.mouse.LEFT:
                        self.input_router.dragged_soul = target_soul
            else:
                # Clicked on empty space
                if self.viewport_mode:
                    # Dismiss the info card; legacy menu handling below.
                    if self._info_card_soul_id is not None:
                        self._info_card_soul_id = None
                        self._info_card_xy = None
                        self.dirty_tracker.mark_dirty()
                # If we have an "on_click_empty" handler in router, use it
                if self.input_router.on_click_empty:
                    self.input_router.on_click_empty(x, y)

        @self.overlay_window.event
        def on_mouse_drag(
            x: int, y: int, dx: int, dy: int, buttons: int, modifiers: int
        ) -> None:
            """Pyglet event handler for mouse drag events."""
            if self.viewport_mode:
                # Pet grammar (issue #31): drags past the start threshold
                # become a carry; the detector rate-limits streamed moves.
                if (
                    self._pet_gestures is not None
                    and self._pet_press_soul_id is not None
                ):
                    for event in self._pet_gestures.drag(x, y):
                        self._handle_pet_gesture(event, self._pet_press_soul_id)
                return
            if self.input_router.dragged_soul:
                self.input_router.dragged_soul.on_mouse_drag(
                    x, y, dx, dy, buttons, modifiers, self.overlay_window.height
                )

        @self.overlay_window.event
        def on_mouse_release(x: int, y: int, button: int, modifiers: int) -> None:
            """Pyglet event handler for mouse release events."""
            if self.viewport_mode:
                # Pet grammar (issue #31): release resolves the gesture --
                # chirp, pet, or carry end. move_to is gone from viewport.
                if button == pyglet.window.mouse.LEFT:
                    soul_id = self._pet_press_soul_id
                    self._pet_press_soul_id = None
                    if soul_id is not None and self._pet_gestures is not None:
                        for event in self._pet_gestures.release(x, y):
                            self._handle_pet_gesture(event, soul_id)
                return
            dragged = self.input_router.dragged_soul
            if dragged is None:
                return
            dragged.on_mouse_release(
                x, y, button, modifiers, self.overlay_window.height
            )
            self.input_router.dragged_soul = None

    def _draw_viewport_pet_overlays(self) -> None:
        """Hover nameplate + right-click info card (issue #31)."""
        consumer = self.viewport_consumer
        if consumer is None:
            return
        hovered = self.input_router.hovered_soul
        if hovered is not None and hovered in self.active_souls:
            soul_id = hovered.biology.soul_id
            ident = consumer.soul_identity(soul_id)
            plate = f"{ident['name']} -- {ident['species']} . Lvl {ident['level']}"
            self.scene_renderer.render_nameplate(
                hovered.x + hovered.width / 2,
                self.overlay_window.height - hovered.draw_y + 10,
                plate,
            )
        if self._info_card_soul_id is not None and self._info_card_xy is not None:
            lines = self._info_card_lines(self._info_card_soul_id)
            x, y = self._info_card_xy
            self.scene_renderer.render_info_card(
                x,
                y,
                lines,
                self.overlay_window.width,
                self.overlay_window.height,
            )

    def _gate_draw_on_dirty(self) -> None:
        """Wrap the Pyglet window draw so GL work only happens when dirty.

        Pyglet dispatches on_draw and flips buffers on every loop tick; gating
        draw() itself is what makes a static scene truly zero-flip idle.
        """
        original_draw = self.overlay_window.draw

        def gated_draw(dt: float) -> None:
            if self.dirty_tracker.consume():
                original_draw(dt)

        self.overlay_window.draw = gated_draw  # type: ignore[method-assign]

    def _overlay_housekeeping(self, dt: float) -> None:
        """Windows overlay policy tick: click-through, parking, DPI."""
        try:
            self._poll_click_through()
            self._poll_fullscreen_parking()
            if self.window_manager.check_dpi_changed():
                self.dirty_tracker.mark_dirty()
        except Exception as e:
            log.debug(f"Overlay housekeeping tick failed: {e}")

    def _poll_click_through(self) -> None:
        """Toggle WS_EX_TRANSPARENT based on cursor-over-Soul hit-testing."""
        pos = get_cursor_pos()
        if pos is None or self.input_router is None:
            return
        soul = self.input_router.poll_soul_under_cursor(
            self.active_souls, pos, self.overlay_window.height
        )
        self.window_manager.update_click_through(soul)
        # Hover state for the viewport nameplate (issue #31): mark the
        # scene dirty on change so the nameplate appears/disappears
        # without waiting for unrelated redraws.
        if self.input_router.hovered_soul is not soul:
            self.input_router.hovered_soul = soul
            self.dirty_tracker.mark_dirty()

    def _poll_fullscreen_parking(self) -> None:
        """Hide the overlay while an exclusive-fullscreen app is foreground."""
        hwnd = getattr(self.window_manager, "hwnd", None)
        fullscreen = foreground_is_exclusive_fullscreen(own_hwnd=hwnd)
        if fullscreen and not self._overlay_parked:
            self.overlay_window.set_visible(False)
            self._overlay_parked = True
            log.info("Exclusive fullscreen detected; overlay parked.")
        elif not fullscreen and self._overlay_parked:
            self._overlay_parked = False
            self.window_manager.show_window()
            self.dirty_tracker.mark_dirty()
            log.info("Fullscreen ended; overlay restored.")

    def handle_empty_click(self, x: int, y: int) -> None:
        """Handle click on empty space."""
        # Close any open menus
        if self.gui_command_queue:
            self.gui_command_queue.put({"type": GuiCommand.HIDE_CONTEXT_MENU})

    def _setup_tray(self) -> None:
        """Initializes the system tray icon."""

        # Define callbacks that schedule on the main thread for safety
        def on_tray_spawn() -> None:
            # Schedule on main thread
            pyglet.clock.schedule_once(lambda dt: self.create_soul(), 0)

        def on_tray_toggle_auras() -> None:
            # Toggle global setting (atomic enough) but update souls safely
            # Schedule on main thread to avoid concurrent modification of active_souls
            pyglet.clock.schedule_once(lambda dt: self.toggle_auras(), 0)

        def on_tray_settings() -> None:
            # This puts into a queue so it's thread-safe, but consistency is good
            pyglet.clock.schedule_once(lambda dt: self.show_global_settings(), 0)

        def on_tray_message_board() -> None:
            if self.gui_command_queue:
                self.gui_command_queue.put(
                    {
                        "type": GuiCommand.SHOW_MESSAGE_BOARD,
                    }
                )

        def on_tray_open_mailbag() -> None:
            # Issue #32: tray Mailbag item opens the answer surface.
            self._open_mailbag_tab()

        def on_tray_exit() -> None:
            pyglet.clock.schedule_once(lambda dt: self.quit_app(), 0)

        def get_tray_souls() -> list[dict]:
            infos: list[dict] = []
            positions: dict[str, tuple[float, float]] = {}
            bounds: tuple[float, float] | None = None
            if self.viewport_consumer is not None:
                positions = self.viewport_consumer.rendered_positions()
                region = self.viewport_consumer.region
                if region is not None:
                    bounds = (region[2], region[3])
            for soul in self.active_souls:
                sid = soul.biology.soul_id
                state = (
                    self.viewport_consumer.soul_state(sid)
                    if self.viewport_consumer is not None
                    else None
                )
                essence = (
                    self.viewport_consumer.soul_essence(sid)
                    if self.viewport_consumer is not None
                    else None
                )
                wpos = positions.get(sid)
                if wpos is not None:
                    location = whereabouts_label(state, wpos[0], wpos[1], bounds)
                else:
                    location = whereabouts_label(state, None, None, bounds)
                infos.append(
                    {
                        "soul_id": sid,
                        "name": soul.biology.name,
                        "essence": essence,
                        "location": location,
                        "state": state,
                    }
                )
            return infos

        def on_pause_toggle() -> None:
            self.sim_paused = not self.sim_paused
            log.info(f"Simulation paused: {self.sim_paused}")

        def on_work_mode_toggle() -> None:
            self.bubble_config.noise.work_mode = not self.bubble_config.noise.work_mode
            self.noise_policy.update_settings(self.bubble_config.noise)
            save_bubble_config(self.bubble_config)
            log.info(f"Work mode: {self.bubble_config.noise.work_mode}")

        def notify_bubble(soul_id: str, text: str, kind: str) -> None:
            if kind not in BUBBLE_KINDS:
                kind = "system"
            pyglet.clock.schedule_once(
                lambda dt: self.bubble_manager.show_bubble(
                    soul_id, text, kind=kind, solicited=True
                ),
                0,
            )

        self.tray_controller = TrayController(
            on_add_soul=on_tray_spawn,
            on_toggle_auras=on_tray_toggle_auras,
            on_settings=on_tray_settings,
            on_message_board=on_tray_message_board,
            on_exit=on_tray_exit,
            get_souls=get_tray_souls,
            get_hub_status=lambda: "online" if self.viewport_mode else "local",
            get_mailbag_count=self._cached_mailbag_count,
            is_paused=lambda: self.sim_paused,
            on_pause_toggle=on_pause_toggle,
            is_work_mode=lambda: self.bubble_config.noise.work_mode,
            on_work_mode_toggle=on_work_mode_toggle,
            on_open_market=None,
            on_open_mailbag=on_tray_open_mailbag,
            on_request_quip=self._request_quip,
            notify_bubble=notify_bubble,
            get_recaps=self._cached_recaps,
            on_open_recap=lambda soul_id, day: self._open_recap_view(
                soul_id, day
            ),
        )
        # Start the tray controller (it handles its own thread)
        self.tray_controller.start()

    def _request_quip(self, soul_id: str) -> dict:
        """Hit the Hub quip endpoint for a soul (issue #30 tray Quips menu).

        Returns the response dict; the tray surfaces rejections (budget /
        essence) as a notification + system bubble. Success needs no local
        action -- the quip arrives as a solicited bubble viewport op.
        """
        hub_url = ""
        try:
            hub_url = (resolve_hub_url() or "").rstrip("/")
        except Exception:
            hub_url = ""
        if not hub_url:
            return {
                "status": "error",
                "reason": "no_hub",
                "message": "No Hub URL configured; quips need online mode.",
            }
        secret = os.getenv("HUB_SECRET_KEY", "")
        headers = {"X-Hub-Secret": secret} if secret else {}
        try:
            resp = httpx.post(
                f"{hub_url}/souls/{soul_id}/quip",
                json={},
                headers=headers,
                timeout=30.0,
            )
        except Exception as exc:
            return {
                "status": "error",
                "reason": "hub_unreachable",
                "message": f"Hub unreachable: {exc}",
            }
        if resp.status_code == 200:
            return {"status": "success", **resp.json()}
        try:
            detail = resp.json().get("detail", {})
        except Exception:
            detail = {}
        if isinstance(detail, dict):
            return {
                "status": "rejected",
                "reason": detail.get("reason", f"http_{resp.status_code}"),
                "message": detail.get(
                    "message", f"Quip rejected ({resp.status_code})."
                ),
            }
        return {
            "status": "rejected",
            "reason": f"http_{resp.status_code}",
            "message": str(detail),
        }

    def show_add_soul_dialog(self) -> None:
        """Shows dialog to add a new soul."""
        if self.gui_command_queue:
            self.gui_command_queue.put({"type": GuiCommand.SHOW_ADD_SOUL})

    async def async_persist_souls_state(self) -> None:
        """Async version of persist_souls_state."""
        if self._is_saving_souls:
            return

        self._is_saving_souls = True
        try:
            from .system.persistence import async_save_souls

            # Only send locally-owned souls to the Hub
            owned_souls = [
                soul for soul in self.active_souls if soul.owner_id == self.instance_id
            ]
            # 1. Persistence Data (Includes Secrets - Secure Disk)
            persistence_data = [
                soul.to_dict(include_secret=True) for soul in owned_souls
            ]

            await async_save_souls(persistence_data, owner_id=self.instance_id)

            # Also save global settings
            current_settings = load_settings()
            current_settings["aura_visible"] = self.global_aura_visible
            save_settings(current_settings)
        finally:
            self._is_saving_souls = False

    def persist_souls_state(self) -> None:
        """Saves current state of all locally-owned souls to disk."""
        if self._is_saving_souls:
            return

        # Fire and forget via background loop
        self._run_coro(self.async_persist_souls_state())

    def initial_hub_sync(self) -> None:
        """Triggers a secure HTTP sync of souls to the Hub."""
        self._run_coro(self.async_initial_hub_sync())

    async def async_initial_hub_sync(self) -> None:
        """Securely registers owned souls with the Hub via HTTP."""
        owned_souls = [
            soul for soul in self.active_souls if soul.owner_id == self.instance_id
        ]
        if not owned_souls:
            return

        # DATA SPLITTING: secrets included for secure HTTP registration
        secure_data = [soul.to_dict(include_secret=True) for soul in owned_souls]

        try:
            log.info("Performing initial secure Hub sync via HTTP...")
            # We use the bulk post_souls endpoint
            await self.network_service.client.post_souls(
                souls_data=secure_data,
                owner_id=self.instance_id,
            )
        except Exception as e:
            log.error(f"Initial Hub sync failed: {e}")

    # --- Sync Event Handlers (Called from Game Loop) ---

    def _handle_network_events(self) -> None:
        """Processes commands from the NetworkService command queue."""
        # Use first active soul for registry-based commands, or 'self' for app-level commands
        context = self.active_souls[0] if self.active_souls else self

        while not self.network_service.command_queue.empty():
            command = self.network_service.command_queue.get()
            if not command:
                continue

            if self.viewport_mode and not isinstance(command, ViewportFrameCommand):
                continue

            try:
                # Commands like PresenceReconcile can handle the App context
                command.execute(context)
            except Exception as e:
                log.error(
                    f"Error executing network command {type(command).__name__}: {e}"
                )
                continue

            # Issue #30: server-driven bubble ops ride the viewport;
            # drain them into the BubbleManager (noise-gated like local).
            if isinstance(command, ViewportFrameCommand) and command.consumer is not None:
                for bop in command.consumer.drain_bubbles():
                    kind = bop.get("kind") or "speech"
                    if kind not in BUBBLE_KINDS:
                        kind = "speech"
                    payload = bop.get("payload")
                    self.bubble_manager.show_bubble(
                        bop["soul_id"],
                        bop.get("text", ""),
                        kind=kind,
                        solicited=bop.get("solicited", False),
                        payload=payload if isinstance(payload, dict) else None,
                    )

    def _soul_screen_positions(self) -> dict[str, tuple[float, float]]:
        """Orb-center screen positions, shared by bubble layout and taps."""
        height = self.overlay_window.height
        return {
            soul.biology.soul_id: (
                soul.x + soul.width / 2,
                height - soul.draw_y,
            )
            for soul in self.active_souls
        }

    def _on_bubble_tap(self, bubble: Bubble) -> None:
        """BubbleManager tap callback (issue #32).

        Tapping a mailbag bubble opens the mailbag answer surface; other
        kinds have no tap action.
        """
        if bubble.kind == KIND_MAILBAG:
            self._open_mailbag_tab()
        elif bubble.kind == KIND_MORNING_NOTE:
            # Issue #33: tapping the morning note opens that soul's
            # recap view (latest day).
            self._open_recap_view(bubble.soul_id)

    def _open_mailbag_tab(self) -> None:
        """Ask the GUI process to show the message board's Mailbag tab."""
        if self.gui_command_queue:
            self.gui_command_queue.put(
                {
                    "type": GuiCommand.SHOW_MESSAGE_BOARD,
                    "tab": "mailbag",
                }
            )

    def _cached_mailbag_count(self) -> int:
        """Pending-question count for the tray badge, cached 30s so menu
        rebuilds on the tray thread never block on network I/O."""
        ts, val = self._mailbag_count_cache
        if time.time() - ts < 30:
            return val
        val = self.mailbag_client.count()
        self._mailbag_count_cache = (time.time(), val)
        return val

    def _cached_recaps(self) -> dict:
        """Recaps per soul for the tray dashboard, cached 5 min so menu
        rebuilds on the tray thread never block on network I/O."""
        ts, val = self._recaps_cache
        if time.time() - ts < 300:
            return val
        out: dict = {}
        for soul in self.active_souls:
            soul_id = getattr(soul.biology, "soul_id", None)
            if not soul_id:
                continue
            recaps = self.recap_client.list_recaps(soul_id)
            if recaps:
                out[soul_id] = recaps
        self._recaps_cache = (time.time(), out)
        return out

    def _open_recap_view(self, soul_id: str, day: str | None = None) -> None:
        """Show a soul's recap lines for a day (issue #33 dashboard view).

        The tray submenu is the browse surface; the full lines arrive
        as a tray notification (the existing transient surface). Runs
        off the tray thread: cache first, background fetch on miss.
        """
        def _show(recaps: list) -> None:
            recap = None
            if day is not None:
                recap = next((r for r in recaps if r.get("day") == day), None)
            if recap is None and recaps:
                recap = recaps[0]
            if recap is None:
                self.tray_controller._notify("No recaps yet.", "Morning recap")
                return
            lines = [
                str(line.get("text", ""))
                for line in recap.get("lines", [])
                if line.get("text")
            ]
            body = "\n".join(lines[:6]) or "quiet night — nothing new"
            self.tray_controller._notify(body, f"Morning recap — {recap['day']}")

        cached = (self._recaps_cache[1] or {}).get(soul_id)
        if cached:
            _show(cached)
            return
        threading.Thread(
            target=lambda: _show(self.recap_client.list_recaps(soul_id)),
            daemon=True,
        ).start()

    def _on_connect_sync(self, online_owners: list[str]) -> None:
        """Reconciles local state with Hub truth."""
        online_set = set(online_owners)
        # Prune stale
        stale_souls = [
            soul
            for soul in self.active_souls
            if soul.owner_id != self.instance_id and soul.owner_id not in online_set
        ]
        if stale_souls:
            log.info(f"Pruning {len(stale_souls)} stale souls.")
            for soul in stale_souls:
                soul.cleanup()
                if soul in self.active_souls:
                    self.active_souls.remove(soul)
            self.dirty_tracker.mark_dirty()

    def _on_owner_offline_sync(self, owner_id: str) -> None:
        """Handle owner going offline."""
        active_len = len(self.active_souls)
        self.active_souls[:] = [s for s in self.active_souls if s.owner_id != owner_id]
        if len(self.active_souls) < active_len:
            log.info(f"Removed souls for offline owner: {owner_id}")
            self.dirty_tracker.mark_dirty()

    def _on_soul_updated_sync(self, souls_data: list[dict], owner_id: str) -> None:
        """Handle soul updates."""
        # SECURITY: Prevent spoofing of local souls by remote/Hub
        if owner_id == self.instance_id:
            log.warning(
                "Received soul update for SELF from network. Ignoring to prevent spoofing."
            )
            return

        existing_map = {s.biology.soul_id: s for s in self.active_souls}
        display = pyglet.display.get_display().get_default_screen()

        for soul_data in souls_data:
            sid = soul_data.get("soul_id")
            if sid in existing_map:
                existing_map[sid].update_from_dict(soul_data)
            else:
                # Create new
                soul = Soul.from_dict(
                    data=soul_data,
                    on_right_click=self.handle_soul_right_click,
                    on_move_end=self._on_soul_move_end,
                    on_state_change=self.persist_souls_state,
                    on_async_state_change=self.async_persist_souls_state,
                    soul_registry=self.active_souls,
                    screen_width=display.width,
                    screen_height=display.height,
                    local_instance_id=self.instance_id,
                    task_scheduler=self._run_coro,
                )
                self.active_souls.append(soul)
                log.info(f"New remote soul: {soul.biology.name}")
                self.dirty_tracker.mark_dirty()

    def update_souls(self, dt: float) -> None:
        """Updates all active souls."""
        # 1. Process Network Events (Downstream)
        self._handle_network_events()

        if self.viewport_mode:
            self._update_viewport_souls(dt)
            if not self.sim_paused:
                self._update_visual_reflexes()
            self._poll_topmost()
            # Pet grammar (issue #31): a hold becomes a pet as soon as
            # the threshold passes, without waiting for release.
            if (
                self._pet_gestures is not None
                and self._pet_press_soul_id is not None
            ):
                for event in self._pet_gestures.poll():
                    self._handle_pet_gesture(event, self._pet_press_soul_id)
            return

        # 2. Update Simulation
        if not self.sim_paused:
            soul_physics.begin_separation_frame()
            for soul in self.active_souls:
                soul.update(dt)
            self._update_visual_reflexes()

        # ... (periodic save/topmost unchanged) ...
        if time.time() - self.last_save_time > 30:
            log.debug("Performing periodic souls state persistence.")
            self.persist_souls_state()
            self.last_save_time = time.time()

        if time.time() - self.last_topmost_time > 5:
            self.window_manager.set_always_on_top()
            self.last_topmost_time = time.time()

        snapshot = [
            (soul.biology.soul_id, round(soul.x, 3), round(soul.y, 3))
            for soul in self.active_souls
        ]
        if snapshot != self._last_render_snapshot:
            self._last_render_snapshot = snapshot
            self.dirty_tracker.mark_dirty()

    def _poll_topmost(self) -> None:
        """Re-asserts the overlay always-on-top window flag."""
        if time.time() - self.last_topmost_time > 5:
            self.window_manager.set_always_on_top()
            self.last_topmost_time = time.time()

    def _update_visual_reflexes(self) -> None:
        """Applies water-cooler reflex overlays to souls (issue #29).

        Local-only: the controller consumes #28's coarse input-activity
        signal and presence events; nothing leaves the machine.
        """
        if self.reflex_controller is None:
            return
        soul_ids = [soul.biology.soul_id for soul in self.active_souls]
        overlays = self.reflex_controller.frame(soul_ids)
        by_id = {soul.biology.soul_id: soul for soul in self.active_souls}
        for sid, overlay in overlays.items():
            soul = by_id.get(sid)
            if soul is None:
                continue
            soul.reflex_kind = overlay.reflex
            soul.reflex_t = overlay.reflex_t
            soul.typing_dip = overlay.typing_dip

    def _update_viewport_souls(self, dt: float) -> None:
        """Positions Hub-driven souls from the viewport interpolator.

        Viewport mode only: no local simulation, no upstream broadcast, no
        disk persistence — the Hub stream is the single source of truth.
        """
        consumer = self.viewport_consumer
        mapper = self.viewport_mapper
        if consumer is None or mapper is None:
            return

        region = consumer.region
        if region is not None:
            mapper.set_region(*region)

        display = pyglet.display.get_display().get_default_screen()
        positions = consumer.rendered_positions()

        known = set(positions)
        existing = {soul.biology.soul_id: soul for soul in self.active_souls}

        for sid in known - set(existing):
            wx, wy = positions[sid]
            soul = Soul.from_dict(
                data={
                    "soul_id": sid,
                    "name": f"Hub Soul {sid[:8]}",
                    "position": [wx, wy],
                    "owner_id": "hub",
                },
                on_right_click=self.handle_soul_right_click,
                on_move_end=self._on_soul_move_end,
                on_state_change=self.persist_souls_state,
                on_async_state_change=self.async_persist_souls_state,
                soul_registry=self.active_souls,
                screen_width=display.width,
                screen_height=display.height,
                local_instance_id=self.instance_id,
                task_scheduler=self._run_coro,
            )
            self.active_souls.append(soul)
            self.dirty_tracker.mark_dirty()

        for sid in set(existing) - known:
            soul = existing[sid]
            soul.cleanup()
            self.active_souls.remove(soul)
            self.dirty_tracker.mark_dirty()

        souls_by_id = {soul.biology.soul_id: soul for soul in self.active_souls}
        states = consumer.soul_states()
        for sid, (wx, wy) in positions.items():
            soul = souls_by_id.get(sid)
            if soul is None:
                continue
            sx, sy = mapper.world_to_screen(wx, wy, display.width, display.height)
            soul.x, soul.y = sx, sy
            soul.draw_y = sy
            # Issue #21: collapsed souls render as statues -- desaturated
            # stone colors and a frozen plasma pulse.
            soul.statue = is_statue(states.get(sid))
            # Issue #22: dormant (unfunded) souls render as statues too,
            # amber-tinted to distinguish them from collapsed statues.
            soul.dormant_statue = consumer.is_dormant(sid)
            # Issue #29: stale Hub presence renders the "offline" statue
            # variant (desaturated, frozen).
            soul.offline_stale = consumer.is_stale(sid)
            # Issue #30 (step 0 of #29): authoritative biology from the
            # Hub stream drives the shader uniforms online instead of
            # healthy local defaults.
            bio = consumer.soul_biology(sid)
            soul.biology.satiety = bio["satiety"]
            soul.biology.hydration = bio["hydration"]
            soul.biology.stats.max_hp = max(1, int(round(bio["max_hp"])))
            soul.biology.current_health = max(
                0,
                min(soul.biology.stats.max_hp, int(round(bio["hp"]))),
            )
            # Issue #30: work mode dims visuals.
            soul.work_dim = self.bubble_config.noise.work_mode
            if not soul.statue and not soul.dormant_statue and not self.sim_paused:
                soul.visual_tick(dt)

        snapshot = [
            (soul.biology.soul_id, round(soul.x, 3), round(soul.y, 3))
            for soul in self.active_souls
        ]
        if snapshot != self._last_render_snapshot:
            self._last_render_snapshot = snapshot
            self.dirty_tracker.mark_dirty()

    def create_soul(
        self,
        name: str | None = None,
        position: tuple[int, int] | None = None,
        load_saved: bool = False,
    ) -> Soul:
        """Creates a new soul and adds it to the overlay."""
        if name is None:
            name = f"Soul {self.next_soul_id}"
            self.next_soul_id += 1

        # Default colors (random if not loaded)
        orb_color = (
            random.random(),
            random.random(),
            random.random(),
        )
        aura_color = (
            random.random(),
            random.random(),
            random.random(),
        )

        if not (load_saved and position is not None):
            if position is None:
                # Center of screen by default/random
                display = pyglet.display.get_display().get_default_screen()
                position = (
                    display.width // 2 - SOUL_WIDTH // 2,
                    display.height // 2 - SOUL_HEIGHT // 2,
                )

        # Get screen dimensions for decoupling
        display = pyglet.display.get_display().get_default_screen()
        sw, sh = display.width, display.height

        soul = Soul(
            name=name,
            orb_color_rgb=orb_color,
            aura_color_rgb=aura_color,
            on_right_click=self.handle_soul_right_click,
            on_move_end=self._on_soul_move_end,
            on_state_change=self.persist_souls_state,
            on_async_state_change=self.async_persist_souls_state,
            initial_position=position,
            soul_registry=self.active_souls,
            screen_width=sw,
            screen_height=sh,
            local_instance_id=self.instance_id,
            task_scheduler=self._run_coro,
        )
        soul.aura_visible = self.global_aura_visible
        self.active_souls.append(soul)
        log.debug(f"Spawned new soul: {name}")
        self.dirty_tracker.mark_dirty()
        self.persist_souls_state()
        return soul

    def _handle_pet_gesture(self, event, soul_id: str) -> None:
        """Map a pet-grammar gesture to a signed intent (issue #31)."""
        kind = event.kind
        if kind == CHIRP:
            # Light local pulse: immediate feedback while the signed
            # chirp intent round-trips; the Hub's solicited "!" bubble
            # arrives after adjudication.
            self.bubble_manager.show_bubble(
                soul_id, "!", kind="system", solicited=True
            )
            intent_kind, payload = gesture_intent(event, soul_id)
            self.network_service.send_intent(intent_kind, soul_id, **payload)
        elif kind == PET:
            intent_kind, payload = gesture_intent(event, soul_id)
            self.network_service.send_intent(intent_kind, soul_id, **payload)
        elif kind in (CARRY_BEGIN, CARRY_MOVE, CARRY_END):
            if self.viewport_mapper is None:
                return
            display = pyglet.display.get_display().get_default_screen()
            wx, wy = self.viewport_mapper.screen_to_world(
                float(event.x), float(event.y), display.width, display.height
            )
            intent_kind, payload = gesture_intent(event, soul_id, (wx, wy))
            self.network_service.send_intent(intent_kind, soul_id, **payload)

    def _info_card_lines(self, soul_id: str) -> list[str]:
        """Build the right-click card lines from viewport state."""
        consumer = self.viewport_consumer
        if consumer is None:
            return [soul_id[:8], "presence: offline"]
        identity = consumer.soul_identity(soul_id)
        biology = consumer.soul_biology(soul_id)
        essence = consumer.wallets().get(soul_id)
        tracked = soul_id in consumer.soul_ids()
        state = consumer.soul_state(soul_id)
        # Whereabouts from the Hub's authoritative world position, not
        # the rendered sprite's screen pixels.
        wpos = consumer.rendered_positions().get(soul_id)
        bounds = None
        region = consumer.region
        if region is not None:
            bounds = (region[2], region[3])
        x = y = None
        if wpos is not None:
            x, y = wpos
        where = whereabouts_label(state, x, y, bounds)
        status = presence_status(tracked, consumer.is_stale(soul_id))
        return build_info_card(soul_id, identity, biology, essence, where, status)

    def _on_soul_move_end(self, soul: Soul, x: float, y: float) -> None:
        self.persist_souls_state()

    def handle_soul_right_click(self, soul: Soul, screen_x: int, screen_y: int) -> None:
        """Callback for soul right-click events."""
        log.debug(f"Right-click on soul {soul.biology.name} at {screen_x}, {screen_y}")

        if self.gui_command_queue:
            self.gui_command_queue.put(
                {
                    "type": GuiCommand.SHOW_CONTEXT_MENU,
                    "soul_id": id(soul),
                    "name": soul.biology.name,
                    "x": screen_x,
                    "y": screen_y,
                }
            )

    def show_soul_settings(self, soul: Soul) -> None:
        """Shows settings dialog for a soul."""
        self.gui_command_queue.put(
            {
                "type": GuiCommand.SHOW_SOUL_SETTINGS,
                "soul_id": id(soul),
                "name": soul.biology.name,
                "orb_color": soul.orb_color_rgb,
                "aura_color": soul.aura_color_rgb,
                "stats": soul.biology.stats.to_dict(),
            }
        )

    def _check_startup_registry(self) -> bool:
        """Check Windows registry to see if Soulscape is set to run on startup."""
        if sys.platform != "win32":
            return False

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "Soulscape"

        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
            try:
                winreg.QueryValueEx(key, app_name)
                is_startup = True
            except FileNotFoundError:
                is_startup = False
            winreg.CloseKey(key)
        except Exception:
            is_startup = False

        return is_startup

    def _set_windows_startup(self, enabled: bool) -> None:
        """Add or remove Soulscape from Windows startup registry."""
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "Soulscape"

        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
            )
            if enabled:
                # Determine command based on execution context
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    exe_path = sys.executable
                    script_path = Path(sys.argv[0]).resolve()
                    cmd = f'"{exe_path}" "{script_path}"'

                winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
                log.debug(f"Added Soulscape to Windows startup: {cmd}")
            else:
                try:
                    winreg.DeleteValue(key, app_name)
                    log.debug("Removed Soulscape from Windows startup.")
                except FileNotFoundError:
                    pass  # Key doesn't exist, nothing to delete
            winreg.CloseKey(key)
        except Exception as e:
            log.error(f"Error modifying Windows startup: {e}")

    def toggle_auras(self) -> None:
        """Toggles visibility of all auras."""
        self.global_aura_visible = not self.global_aura_visible
        self.scene_renderer.aura_visible = self.global_aura_visible

        # Persist
        current_settings = load_settings()
        current_settings["aura_visible"] = self.global_aura_visible
        save_settings(current_settings)

        # Update active souls
        for soul in self.active_souls:
            soul.aura_visible = self.global_aura_visible

        self.dirty_tracker.mark_dirty()
        log.info(f"Global aura visibility set to: {self.global_aura_visible}")

    def show_global_settings(self) -> None:
        """Shows global settings dialog."""
        if self.gui_command_queue:
            run_on_startup = self._check_startup_registry()
            current_hub_url = self.saved_settings.get(
                "hub_url", "http://localhost:9785"
            )
            self.gui_command_queue.put(
                {
                    "type": GuiCommand.SHOW_GLOBAL_SETTINGS,
                    "current_opacity": self.global_opacity,
                    "run_on_startup": run_on_startup,
                    "current_hub_url": current_hub_url,
                    "current_mode": get_client_mode(),
                }
            )

    def load_initial_souls(self) -> None:
        """Loads souls from disk or creates default if none exist."""
        saved_souls = load_souls()
        if saved_souls:
            # Get screen dimensions for decoupling
            display = pyglet.display.get_display().get_default_screen()
            sw, sh = display.width, display.height

            for soul_data in saved_souls:
                soul = Soul.from_dict(
                    data=soul_data,
                    on_right_click=self.handle_soul_right_click,
                    on_move_end=self._on_soul_move_end,
                    on_state_change=self.persist_souls_state,
                    on_async_state_change=self.async_persist_souls_state,
                    soul_registry=self.active_souls,
                    screen_width=sw,
                    screen_height=sh,
                    local_instance_id=self.instance_id,
                    task_scheduler=self._run_coro,
                )
                self.active_souls.append(soul)

        # Ensure this instance has at least one locally-owned soul
        has_own_soul = any(
            soul.owner_id == self.instance_id for soul in self.active_souls
        )
        if not has_own_soul:
            self.create_soul()

    def check_gui_results(self) -> None:
        """Polls the GUI result queue for messages and processes them."""
        if not self.gui_result_queue:
            return

        try:
            while True:
                # Non-blocking get
                msg = self.gui_result_queue.get_nowait()
                cmd_type = msg.get("type")

                if cmd_type == GuiCommand.SHOW_GLOBAL_SETTINGS:
                    data = msg.get("data")
                    opacity = data.get("opacity")
                    startup = data.get("startup")
                    hub_url = data.get("hub_url")
                    mode = data.get("mode", "offline")

                    # Apply settings
                    self.global_opacity = opacity
                    self.window_manager.set_opacity(opacity)
                    self.dirty_tracker.mark_dirty()

                    # Persist
                    current_settings = load_settings()
                    current_settings["opacity"] = opacity
                    current_settings["hub_url"] = hub_url
                    if mode in ("offline", "online"):
                        current_settings["mode"] = mode
                    save_settings(current_settings)
                    self.saved_settings = current_settings

                    # Startup reg
                    self._set_windows_startup(startup)
                    log.debug(
                        "Applied global settings: opacity=%d, startup=%s, "
                        "hub_url=%s, mode=%s",
                        opacity,
                        startup,
                        hub_url,
                        mode,
                    )

                elif cmd_type == GuiCommand.SHOW_CONTEXT_MENU:
                    action = msg.get("action")
                    soul_id = msg.get("soul_id")

                    if action and soul_id:
                        # Find soul
                        target_soul = next(
                            (s for s in self.active_souls if id(s) == soul_id),
                            None,
                        )
                        if target_soul:
                            if action == "EDIT":
                                self.show_soul_settings(target_soul)
                            elif action == "TOGGLE_AURA":
                                target_soul.aura_visible = not target_soul.aura_visible
                                self.dirty_tracker.mark_dirty()
                                self.persist_souls_state()
                            elif action == "DISMISS":
                                self.active_souls.remove(target_soul)
                                target_soul.cleanup()
                                log.debug(f"Dismissed soul: {target_soul.biology.name}")
                                self.dirty_tracker.mark_dirty()
                                self.persist_souls_state()

                elif cmd_type == GuiCommand.CREATE_SOCIAL_POST:
                    data = msg.get("data")
                    if data:
                        self._run_coro(
                            MessageBoard().create_post(
                                data["author_id"],
                                data["author_name"],
                                data["title"],
                                data["content"],
                            )
                        )
                        log.info(f"Created operator post via GUI: {data['title']}")

                elif cmd_type == GuiCommand.CREATE_SOCIAL_REPLY:
                    data = msg.get("data")
                    if data:
                        self._run_coro(
                            MessageBoard().create_reply(
                                data["author_id"],
                                data["author_name"],
                                data["parent_id"],
                                data["content"],
                            )
                        )
                        log.info(
                            f"Created operator reply via GUI: {data['content'][:20]}"
                        )

                elif cmd_type == GuiCommand.DELETE_SOCIAL_MESSAGE:
                    data = msg.get("data")
                    if data:
                        self._run_coro(
                            MessageBoard().delete_message(
                                data["requester_id"], data["message_id"]
                            )
                        )
                        log.info(
                            f"Dispatched deletion for message: {data['message_id']}"
                        )

                elif cmd_type == GuiCommand.EDIT_SOCIAL_MESSAGE:
                    data = msg.get("data")
                    if data:
                        self._run_coro(
                            MessageBoard().edit_message(
                                data["requester_id"],
                                data["message_id"],
                                data["content"],
                            )
                        )
                        log.info(f"Dispatched edit for message: {data['message_id']}")

                elif cmd_type == GuiCommand.SHOW_SOUL_SETTINGS:
                    data = msg.get("data")
                    soul_id = msg.get("soul_id")
                    if data and soul_id:
                        target_soul = next(
                            (s for s in self.active_souls if id(s) == soul_id),
                            None,
                        )
                        if target_soul:
                            target_soul.biology.name = data.get("name")
                            target_soul.orb_color_rgb = data.get("orb_color")
                            target_soul.aura_color_rgb = data.get("aura_color")

                            # Update renderers
                            target_soul.orb_renderer.base_color_rgb = (
                                target_soul.orb_color_rgb
                            )
                            target_soul.aura_renderer.base_color_rgb = (
                                target_soul.aura_color_rgb
                            )

                            log.debug(f"Updated soul {target_soul.biology.name}")
                            self.dirty_tracker.mark_dirty()
                            self.persist_souls_state()

                elif cmd_type == GuiCommand.SHOW_ADD_SOUL:
                    data = msg.get("data")
                    if data:
                        # Create new soul with these params
                        soul = self.create_soul(name=data.get("name"))
                        soul.orb_color_rgb = data.get("orb_color")
                        soul.aura_color_rgb = data.get("aura_color")
                        # Update renderers immediately
                        soul.orb_renderer.base_color_rgb = soul.orb_color_rgb
                        soul.aura_renderer.base_color_rgb = soul.aura_color_rgb
                        self.persist_souls_state()

        except queues.Empty:
            pass
        except Exception as e:
            log.error(f"Error processing GUI results: {e}")

    def quit_app(self) -> None:
        """Cleans up resources and exits the application."""
        if not self._running:
            return  # Avoid double shutdown

        log.info("Shutting down Soulscape...")
        self._running = False

        # 1. Save final state
        self.persist_souls_state()

        # 2. Stop Network Service (Graceful WebSocket closure)
        self.network_service.stop()

        # 3. Stop all Souls
        for soul in list(self.active_souls):
            soul.stop()

        # 4. Stop background persistence loop
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._shutdown_async(), self._loop
                ).result(timeout=5)
            except concurrent.futures.TimeoutError:
                log.warning("Background loop shutdown timed out.")
            except Exception as e:
                log.warning(f"Background loop shutdown failed unexpectedly: {e}")
            if self._loop_thread and threading.current_thread() != self._loop_thread:
                self._loop_thread.join(timeout=2.0)

        # 5. Stop GUI services
        if self.tray_controller:
            self.tray_controller.stop()
        if self.gui_process and self.gui_process.is_alive():
            # Graceful shutdown attempt
            if self.gui_command_queue:
                self.gui_command_queue.put({"type": GuiCommand.EXIT})

            self.gui_process.join(timeout=2.0)

            # Force kill if still alive
            if self.gui_process.is_alive():
                log.warning("GUI process did not exit gracefully, terminating.")
                self.gui_process.terminate()
                self.gui_process.join(timeout=1.0)
        if self.overlay_window:
            self.overlay_window.close()

        log.info("Cleanup complete. Exiting.")
        pyglet.app.exit()

    async def _shutdown_async(self) -> None:
        """Coroutine to perform formal shutdown operations in the background loop."""
        try:
            # Cancel all tasks (like pending persistence)
            tasks = [
                t
                for t in asyncio.all_tasks(self._loop)
                if t is not asyncio.current_task()
            ]
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if self._loop:
                self._loop.stop()


def main() -> None:
    """Entry point for the application."""
    app = SoulscapeApp()
    app.run()


if __name__ == "__main__":
    main()
