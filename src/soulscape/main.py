# overlay_app.py
"""
Unified Overlay Application for Soulscape.
Manages a single transparent full-screen window and renders multiple souls within it.
"""

import multiprocessing
import os
import random
import sys
import winreg

import pyglet
from dotenv import load_dotenv
from pyglet.window import key

from soulscape.constants import SOUL_HEIGHT, SOUL_WIDTH
from soulscape.core.soul import Soul
from soulscape.system.input_router import InputRouter
from soulscape.system.logger import log
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

# Globals
active_souls = []
window_manager = None
overlay_window = None
tray_controller = None
scene_renderer = None
input_router = None
_saved_settings = load_settings()
_global_opacity = _saved_settings.get("opacity", 80)
_next_soul_id = 1

gui_command_queue = None
gui_result_queue = None


def persist_souls_state():
    """Saves current souls to disk."""
    data = [soul.to_dict() for soul in active_souls]
    save_souls(data)


def create_soul(name=None, position=None, load_saved=False):
    """Creates a new soul and adds it to the overlay."""
    global _next_soul_id

    if name is None:
        name = f"Soul {_next_soul_id}"
        _next_soul_id += 1

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

    if load_saved and position is not None:
        # Use loaded position
        pass
    elif position is None:
        # Center of screen by default/random
        display = pyglet.display.get_display().get_default_screen()
        position = (
            display.width // 2 - SOUL_WIDTH // 2,
            display.height // 2 - SOUL_HEIGHT // 2,
        )

    soul = Soul(
        window_instance=overlay_window,
        orb_color_rgb=orb_color,
        aura_color_rgb=aura_color,
        name=name,
        on_right_click=handle_soul_right_click,
        on_move_end=persist_souls_state,
        initial_position=position,
        soul_registry=active_souls,
    )

    active_souls.append(soul)
    log.debug(f"Spawned new soul: {name}")
    return soul


def handle_soul_right_click(soul, screen_x, screen_y):
    """Callback for soul right-click events."""
    log.debug(f"Right-click on soul {soul.name} at {screen_x}, {screen_y}")

    if gui_command_queue:
        gui_command_queue.put(
            {
                "type": GuiCommand.SHOW_CONTEXT_MENU,
                "soul_id": id(soul),
                "name": soul.name,
                "x": screen_x,
                "y": screen_y,
            }
        )


def show_soul_settings(soul):
    """Shows settings dialog for a soul."""

    gui_command_queue.put(
        {
            "type": GuiCommand.SHOW_SOUL_SETTINGS,
            "soul_id": id(soul),
            "name": soul.name,
            "orb_color": soul.orb_color_rgb,
            "aura_color": soul.aura_color_rgb,
            "stats": soul.stats.to_dict(),
        }
    )


def _check_startup_registry():
    """Check Windows registry to see if Soulscape is set to run on startup."""
    global _run_on_startup
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
            _run_on_startup = True
        except FileNotFoundError:
            _run_on_startup = False
        winreg.CloseKey(key)
    except Exception:
        _run_on_startup = False

    return _run_on_startup


def _set_windows_startup(enabled):
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
                # PyInstaller or similar frozen executable
                cmd = f'"{sys.executable}"'
            else:
                # Running as script
                # Use python.exe (or pythonw.exe if we could detect it, but sys.executable is safer fallback)
                exe_path = sys.executable
                script_path = os.path.abspath(sys.argv[0])
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


# Initialize startup state
_check_startup_registry()


def show_global_settings():
    """Shows global settings dialog."""
    # Use GUI service instead of direct multiprocessing
    global gui_command_queue, _global_opacity

    if gui_command_queue:
        run_on_startup = _check_startup_registry()
        # current_hotkey = load_settings().get("spawn_hotkey", "ctrl+shift+s") # GUI doesn't support editing this yet
        gui_command_queue.put(
            {
                "type": GuiCommand.SHOW_GLOBAL_SETTINGS,
                "current_opacity": _global_opacity,
                "run_on_startup": run_on_startup,
                # "spawn_hotkey": current_hotkey,
            }
        )


def load_initial_souls():
    """Loads souls from disk."""
    saved_souls = load_souls()
    if saved_souls:
        for soul_data in saved_souls:
            soul = Soul.from_dict(
                data=soul_data,
                window_instance=overlay_window,
                on_right_click=handle_soul_right_click,
                on_move_end=persist_souls_state,
                soul_registry=active_souls,
            )
            active_souls.append(soul)
    else:
        # Create default soul
        create_soul(name="Genesis Soul")


def main():
    global overlay_window, window_manager, tray_controller, gui_command_queue, gui_result_queue, scene_renderer, input_router

    # Load environment variables
    load_dotenv()

    log.debug("Starting Overlay Application...")

    # Initialize Persistent GUI Service
    global gui_process
    gui_command_queue = multiprocessing.Queue()
    gui_result_queue = multiprocessing.Queue()

    gui_process = multiprocessing.Process(
        target=run_gui_service,
        args=(gui_command_queue, gui_result_queue),
        daemon=True,
    )
    gui_process.start()
    log.debug("GUI Service started.")

    # 1. Setup Window Manager (Creates Overlay Window)
    window_manager = get_window_manager()
    overlay_window = window_manager.window

    # Apply initial opacity
    window_manager.set_opacity(_global_opacity)

    # 2b. Initialize Scene Renderer and Input Router
    scene_renderer = SceneRenderer()
    input_router = InputRouter()

    # 3. Setup Input Handling
    @overlay_window.event
    def on_draw():
        overlay_window.clear()
        scene_renderer.render(active_souls, overlay_window.height)

    @overlay_window.event
    def on_mouse_press(x, y, button, modifiers):
        # Use InputRouter to find target soul
        target_soul = input_router.get_soul_at(
            active_souls, x, y, overlay_window.height
        )

        if target_soul:
            # Dispatch to target
            target_soul.on_mouse_press(x, y, button, modifiers)
            # Store as dragged soul if left click
            if button == pyglet.window.mouse.LEFT:
                input_router.dragged_soul = target_soul
        else:
            # Clicked on empty space
            pass

    @overlay_window.event
    def on_mouse_drag(x, y, dx, dy, buttons, modifiers):
        if input_router.dragged_soul:
            input_router.dragged_soul.on_mouse_drag(
                x, y, dx, dy, buttons, modifiers
            )

    @overlay_window.event
    def on_mouse_release(x, y, button, modifiers):
        if input_router.dragged_soul:
            input_router.dragged_soul.on_mouse_release(x, y, button, modifiers)
            input_router.dragged_soul = None

        # Also let specific soul handle release if it wasn't the dragged one (rare but possible)
        # For now, just clearing dragged_soul is enough for our logic.

    @overlay_window.event
    def on_key_press(symbol, modifiers):
        if symbol == key.ESCAPE:
            quit_app()
        # Soul specific keys? 'A' for Aura?
        # Broadcast to all?
        for soul in active_souls:
            soul.on_key_press(symbol, modifiers)

    # 4. Load Content
    load_initial_souls()

    # 5. Tray Icon
    def on_tray_spawn():
        # Schedule on main thread
        pyglet.clock.schedule_once(lambda dt: create_soul(), 0)

    def on_tray_toggle_auras():
        for soul in active_souls:
            soul.aura_visible = not soul.aura_visible

    def on_tray_settings():
        show_global_settings()

    def on_tray_message_board():
        global gui_command_queue
        if gui_command_queue:
            gui_command_queue.put(
                {
                    "type": GuiCommand.SHOW_MESSAGE_BOARD,
                }
            )

    def on_tray_exit():
        # This runs in tray thread usually, need to signal main thread
        # Or tray controller calls quit_app
        pyglet.clock.schedule_once(lambda dt: quit_app(), 0)

    tray_controller = TrayController(
        on_add_soul=on_tray_spawn,
        on_toggle_auras=on_tray_toggle_auras,
        on_settings=on_tray_settings,
        on_message_board=on_tray_message_board,
        on_exit=on_tray_exit,
    )
    tray_controller.start()

    # 6. Run Loop
    log.debug("Entering main loop.")

    # Schedule GUI result polling
    pyglet.clock.schedule_interval(lambda dt: check_gui_results(), 0.1)

    pyglet.app.run()


def check_gui_results():
    """Polls the GUI result queue for messages."""
    global _global_opacity

    if not gui_result_queue:
        return

    try:
        while True:
            # Non-blocking get
            msg = gui_result_queue.get_nowait()
            cmd_type = msg.get("type")

            if cmd_type == GuiCommand.SHOW_GLOBAL_SETTINGS:
                data = msg.get("data")
                opacity = data.get("opacity")
                startup = data.get("startup")

                # Apply settings
                _global_opacity = opacity
                window_manager.set_opacity(opacity)

                # Persist
                current_settings = load_settings()
                current_settings["opacity"] = opacity
                save_settings(current_settings)

                # Startup reg
                _set_windows_startup(startup)
                log.debug(
                    f"Applied global settings: opacity={opacity}, startup={startup}"
                )

            elif cmd_type == GuiCommand.SHOW_CONTEXT_MENU:
                action = msg.get("action")
                soul_id = msg.get("soul_id")

                if action and soul_id:
                    # Find soul
                    target_soul = next(
                        (s for s in active_souls if id(s) == soul_id), None
                    )
                    if target_soul:
                        if action == "EDIT":
                            show_soul_settings(target_soul)
                        elif action == "TOGGLE_AURA":
                            target_soul.aura_visible = (
                                not target_soul.aura_visible
                            )
                        elif action == "DISMISS":
                            active_souls.remove(target_soul)
                            target_soul.cleanup()
                            log.debug(f"Dismissed soul: {target_soul.name}")
                            persist_souls_state()

            elif cmd_type == GuiCommand.SHOW_SOUL_SETTINGS:
                data = msg.get("data")
                soul_id = msg.get("soul_id")
                if data and soul_id:
                    target_soul = next(
                        (s for s in active_souls if id(s) == soul_id), None
                    )
                    if target_soul:
                        target_soul.name = data.get("name")
                        target_soul.orb_color_rgb = data.get("orb_color")
                        target_soul.aura_color_rgb = data.get("aura_color")

                        # Update renderers
                        target_soul.orb_renderer.base_color_rgb = (
                            target_soul.orb_color_rgb
                        )
                        target_soul.aura_renderer.base_color_rgb = (
                            target_soul.aura_color_rgb
                        )

                        log.debug(f"Updated soul {target_soul.name}")
                        persist_souls_state()

            elif cmd_type == GuiCommand.SHOW_ADD_SOUL:
                data = msg.get("data")
                if data:
                    # Create new soul with these params
                    soul = create_soul(name=data.get("name"))
                    soul.orb_color_rgb = data.get("orb_color")
                    soul.aura_color_rgb = data.get("aura_color")
                    # Update renderers immediately
                    soul.orb_renderer.base_color_rgb = soul.orb_color_rgb
                    soul.aura_renderer.base_color_rgb = soul.aura_color_rgb
                    persist_souls_state()

    except multiprocessing.queues.Empty:
        pass
    except Exception as e:
        log.error(f"Error processing GUI results: {e}")


def quit_app():
    log.debug("Shutting down...")
    persist_souls_state()
    if tray_controller:
        tray_controller.stop()
    if overlay_window:
        overlay_window.close()
    pyglet.app.exit()
    sys.exit(0)


if __name__ == "__main__":
    main()
