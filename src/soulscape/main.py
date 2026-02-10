"""Unified Overlay Application for Soulscape.

Manages a single transparent full-screen window and renders multiple souls within it.
"""

from __future__ import annotations

import logging
import multiprocessing
import random
import sys
import time
import winreg
from pathlib import Path
from typing import Any

import pyglet
from dotenv import load_dotenv
from pyglet.window import key

from soulscape.constants import SOUL_HEIGHT, SOUL_WIDTH
from soulscape.core import MessageBoard, Soul
from soulscape.system.input_router import InputRouter
from soulscape.system.logger import log, setup_logging
from soulscape.system.persistence import (
    load_settings,
    load_souls,
    save_settings,
    save_souls,
)
from soulscape.system.tray import TrayController
from soulscape.system.window_manager import get_window_manager
from soulscape.ui.graphics.scene_renderer import SceneRenderer
from soulscape.ui.gui.gui_service import GuiCommand, run_gui_service
from soulscape.utils.helpers import safe_run_async

# Globals - now encapsulated in SoulscapeApp, but kept for type hinting if needed
# active_souls: list[Soul] = []
# Explicitly load dotenv
env_path = Path.cwd() / ".env"
load_dotenv(dotenv_path=env_path, override=True)


# Set up logging using the project's utility
setup_logging(level=logging.DEBUG)
# Suppress noisy library logs even in debug mode
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("pyglet").setLevel(logging.INFO)


class SoulscapeApp:
    """Main application class for Soulscape."""

    def __init__(self) -> None:
        self.active_souls: list[Soul] = []
        self.window_manager: Any = None
        self.overlay_window: Any = None
        self.tray_controller: Any = None
        self.scene_renderer: Any = None
        self.input_router: Any = None

        # Load settings
        self.saved_settings: dict[str, Any] = load_settings()
        self.global_opacity: int = self.saved_settings.get("opacity", 80)
        self.global_aura_visible: bool = self.saved_settings.get(
            "aura_visible", True
        )
        self.run_on_startup: bool = self._check_startup_registry()
        self.instance_id: str = self.saved_settings.get(
            "instance_id", "unknown"
        )

        self.gui_command_queue: Any = None
        self.gui_result_queue: Any = None
        self.gui_process: Any = None

        self.next_soul_id: int = 1
        self.last_save_time: float = time.time()

    def run(self) -> None:
        """Starts the application."""
        log.debug("Starting Overlay Application...")

        # Initialize Persistent GUI Service
        self.gui_command_queue = multiprocessing.Queue()
        self.gui_result_queue = multiprocessing.Queue()

        self.gui_process = multiprocessing.Process(
            target=run_gui_service,
            args=(self.gui_command_queue, self.gui_result_queue),
            daemon=True,
        )
        self.gui_process.start()
        log.debug("GUI Service started.")

        # 1. Setup Window Manager (Creates Overlay Window)
        self.window_manager = get_window_manager()
        self.overlay_window = self.window_manager.window

        # Apply initial opacity
        self.window_manager.set_opacity(self.global_opacity)

        # 2b. Initialize Scene Renderer and Input Router
        self.scene_renderer = SceneRenderer()
        self.input_router = InputRouter()

        # Apply initial aura visibility
        self.scene_renderer.aura_visible = self.global_aura_visible

        # 3. Setup Input Handling
        self._setup_window_events()

        # 4. Load Content
        self.load_initial_souls()

        # 5. Tray Icon
        self._setup_tray()

        # 6. Run Loop
        log.debug("Entering main loop.")

        # Schedule GUI result polling
        pyglet.clock.schedule_interval(lambda dt: self.check_gui_results(), 0.1)

        # Schedule Soul Updates (Centralized Loop)
        pyglet.clock.schedule_interval(self.update_souls, 1 / 60.0)

        pyglet.app.run()

    def _setup_window_events(self) -> None:
        """Sets up Pyglet window event handlers."""

        @self.overlay_window.event
        def on_draw() -> None:
            """Pyglet event handler for drawing the window content."""
            self.overlay_window.clear()
            self.scene_renderer.render(
                self.active_souls, self.overlay_window.height
            )

        @self.overlay_window.event
        def on_mouse_press(
            x: int, y: int, button: int, modifiers: int
        ) -> bool | None:
            """Pyglet event handler for mouse press events."""
            # Use InputRouter to find target soul
            target_soul = self.input_router.get_soul_at(
                self.active_souls, x, y, self.overlay_window.height
            )

            if target_soul:
                # Dispatch to target
                target_soul.on_mouse_press(
                    x, y, button, modifiers, self.overlay_window.height
                )
                # Store as dragged soul if left click
                if button == pyglet.window.mouse.LEFT:
                    self.input_router.dragged_soul = target_soul
            else:
                # Clicked on empty space
                pass

        @self.overlay_window.event
        def on_mouse_drag(
            x: int, y: int, dx: int, dy: int, buttons: int, modifiers: int
        ) -> None:
            """Pyglet event handler for mouse drag events."""
            if self.input_router.dragged_soul:
                self.input_router.dragged_soul.on_mouse_drag(
                    x, y, dx, dy, buttons, modifiers, self.overlay_window.height
                )

        @self.overlay_window.event
        def on_mouse_release(
            x: int, y: int, button: int, modifiers: int
        ) -> None:
            """Pyglet event handler for mouse release events."""
            if self.input_router.dragged_soul:
                self.input_router.dragged_soul.on_mouse_release(
                    x, y, button, modifiers, self.overlay_window.height
                )
                self.input_router.dragged_soul = None

        @self.overlay_window.event
        def on_key_press(symbol: int, modifiers: int) -> int | None:
            if symbol == key.ESCAPE:
                self.quit_app()
            elif symbol == key.A:
                self.toggle_auras()

    def _setup_tray(self) -> None:
        """Initializes and starts the system tray icon."""

        def on_tray_spawn() -> None:
            # Schedule on main thread
            pyglet.clock.schedule_once(lambda dt: self.create_soul(), 0)

        def on_tray_toggle_auras() -> None:
            self.toggle_auras()

        def on_tray_settings() -> None:
            self.show_global_settings()

        def on_tray_message_board() -> None:
            if self.gui_command_queue:
                self.gui_command_queue.put(
                    {
                        "type": GuiCommand.SHOW_MESSAGE_BOARD,
                    }
                )

        def on_tray_exit() -> None:
            pyglet.clock.schedule_once(lambda dt: self.quit_app(), 0)

        self.tray_controller = TrayController(
            on_add_soul=on_tray_spawn,
            on_toggle_auras=on_tray_toggle_auras,
            on_settings=on_tray_settings,
            on_message_board=on_tray_message_board,
            on_exit=on_tray_exit,
        )
        self.tray_controller.start()

    def toggle_auras(self) -> None:
        """Toggles aura visibility for all souls and persists the setting."""
        self.scene_renderer.aura_visible = not self.scene_renderer.aura_visible
        self.global_aura_visible = self.scene_renderer.aura_visible
        for soul in self.active_souls:
            soul.aura_visible = self.scene_renderer.aura_visible
        log.info(f"Global Aura Visibility: {self.scene_renderer.aura_visible}")
        self.persist_souls_state()

    def persist_souls_state(self) -> None:
        """Saves current souls to disk."""
        data = [soul.to_dict() for soul in self.active_souls]
        save_souls(data)

        # Also save global settings that affect souls (like aura visibility)
        current_settings = load_settings()
        current_settings["aura_visible"] = self.global_aura_visible
        save_settings(current_settings)

    def update_souls(self, dt: float) -> None:
        """Updates all active souls."""
        for soul in self.active_souls:
            soul.update(dt)

        # Periodic Save (every 60 seconds)
        if time.time() - self.last_save_time > 60:
            log.debug("Performing periodic souls state persistence.")
            self.persist_souls_state()
            self.last_save_time = time.time()

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
            on_move_end=self.persist_souls_state,
            on_state_change=self.persist_souls_state,
            initial_position=position,
            soul_registry=self.active_souls,
            screen_width=sw,
            screen_height=sh,
            local_instance_id=self.instance_id,
        )
        soul.aura_visible = self.global_aura_visible
        self.active_souls.append(soul)
        log.debug(f"Spawned new soul: {name}")
        self.persist_souls_state()
        return soul

    def handle_soul_right_click(
        self, soul: Soul, screen_x: int, screen_y: int
    ) -> None:
        """Callback for soul right-click events."""
        log.debug(
            f"Right-click on soul {soul.biology.name} at {screen_x}, {screen_y}"
        )

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
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ
            )
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

    def show_global_settings(self) -> None:
        """Shows global settings dialog."""
        if self.gui_command_queue:
            run_on_startup = self._check_startup_registry()
            self.gui_command_queue.put(
                {
                    "type": GuiCommand.SHOW_GLOBAL_SETTINGS,
                    "current_opacity": self.global_opacity,
                    "run_on_startup": run_on_startup,
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
                    on_move_end=self.persist_souls_state,
                    on_state_change=self.persist_souls_state,
                    soul_registry=self.active_souls,
                    screen_width=sw,
                    screen_height=sh,
                    local_instance_id=self.instance_id,
                )
                self.active_souls.append(soul)
        else:
            # Create default soul
            self.create_soul(name="Genesis Soul")

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

                    # Apply settings
                    self.global_opacity = opacity
                    self.window_manager.set_opacity(opacity)

                    # Persist
                    current_settings = load_settings()
                    current_settings["opacity"] = opacity
                    save_settings(current_settings)

                    # Startup reg
                    self._set_windows_startup(startup)
                    log.debug(
                        "Applied global settings: opacity=%d, startup=%s",
                        opacity,
                        startup,
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
                                target_soul.aura_visible = (
                                    not target_soul.aura_visible
                                )
                                self.persist_souls_state()
                            elif action == "DISMISS":
                                self.active_souls.remove(target_soul)
                                target_soul.cleanup()
                                log.debug(
                                    f"Dismissed soul: {target_soul.biology.name}"
                                )
                                self.persist_souls_state()

                elif cmd_type == GuiCommand.CREATE_SOCIAL_POST:
                    data = msg.get("data")
                    if data:
                        safe_run_async(
                            MessageBoard().create_post(
                                data["author_id"],
                                data["author_name"],
                                data["title"],
                                data["content"],
                            )
                        )
                        log.info(
                            f"Created operator post via GUI: {data['title']}"
                        )

                elif cmd_type == GuiCommand.CREATE_SOCIAL_REPLY:
                    data = msg.get("data")
                    if data:
                        safe_run_async(
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
                        success = safe_run_async(
                            MessageBoard().delete_message(
                                data["requester_id"], data["message_id"]
                            )
                        )
                        if success:
                            log.info(
                                f"Deleted social message: {data['message_id']}"
                            )
                        else:
                            log.warning(
                                f"Failed to delete social message: {data['message_id']}"
                            )

                elif cmd_type == GuiCommand.EDIT_SOCIAL_MESSAGE:
                    data = msg.get("data")
                    if data:
                        success = safe_run_async(
                            MessageBoard().edit_message(
                                data["requester_id"],
                                data["message_id"],
                                data["content"],
                            )
                        )
                        if success:
                            log.info(
                                f"Edited social message: {data['message_id']}"
                            )
                        else:
                            log.warning(
                                f"Failed to edit social message: {data['message_id']}"
                            )

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

                            log.debug(
                                f"Updated soul {target_soul.biology.name}"
                            )
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

        except multiprocessing.queues.Empty:
            pass
        except Exception as e:
            log.error(f"Error processing GUI results: {e}")

    def quit_app(self) -> None:
        """Cleans up resources and exits the application."""
        log.debug("Shutting down...")
        self.persist_souls_state()
        if self.tray_controller:
            self.tray_controller.stop()
        if self.overlay_window:
            self.overlay_window.close()
        pyglet.app.exit()
        sys.exit(0)


if __name__ == "__main__":
    app = SoulscapeApp()
    app.run()
