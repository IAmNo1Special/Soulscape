# launcher.py
"""Soulscape launcher with system tray integration."""

import random
import sys
import threading

import pyglet

from constants import WINDOW_HEIGHT, WINDOW_WIDTH
from logger import log
from persistence import load_settings, load_souls, save_settings, save_souls
from settings_gui import GlobalSettingsDialog, SoulContextMenu, SoulSettingsDialog
from soul import SoulApp
from tray import TrayController
from window_manager import get_window_manager

# Global state
active_souls = []
active_windows = []
tray_controller = None
_next_window_id = 1
window_manager = get_window_manager()  # Platform-specific window manager

# Global settings state - load from settings.json
_saved_settings = load_settings()
_global_opacity = _saved_settings["opacity"]  # Percentage (15-100)
_run_on_startup = False


def _check_startup_registry():
    """Check Windows registry to see if Soulscape is set to run on startup."""
    global _run_on_startup
    if sys.platform != "win32":
        return False

    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "Soulscape"

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, app_name)
            _run_on_startup = True
        except FileNotFoundError:
            _run_on_startup = False
        winreg.CloseKey(key)
    except Exception:
        _run_on_startup = False

    return _run_on_startup


# Initialize startup state from registry
_check_startup_registry()


def persist_souls_state():
    """Save the current state of all active souls to disk."""
    if active_souls:
        # Use Pyglet clock to ensure thread safety if called from thread
        def _save(dt):
            try:
                soul_states = [soul.to_dict() for soul in active_souls]
                save_souls(soul_states)
            except Exception as e:
                log.error(f"Error persisting soul states: {e}")

        pyglet.clock.schedule_once(_save, 0)


def create_new_soul_window(
    orb_color_rgb=(0.56, 0.93, 0.56),
    aura_color_rgb=(1.0, 0.5, 0.0),
    name=None,
    position=None,
):
    """
    Create a new transparent soul window.

    Args:
        orb_color_rgb: Orb color as RGB tuple (0.0-1.0)
        aura_color_rgb: Aura color as RGB tuple (0.0-1.0)
        name: Soul name (auto-generated if None)
        position: Optional (x, y) tuple for initial position
    """
    global _next_window_id
    window_id = _next_window_id
    _next_window_id += 1

    new_window = None
    try:
        config = pyglet.gl.Config(
            double_buffer=True,
            depth_size=0,
            alpha_size=8,
            samples=4,
        )

        new_window = pyglet.window.Window(
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            caption=f"SoulWindow_{window_id}",
            style=pyglet.window.Window.WINDOW_STYLE_OVERLAY,
            config=config,
            vsync=True,
            resizable=False,
            visible=True,
            fullscreen=False,
        )

        if position:
            new_window.set_location(int(position[0]), int(position[1]))
        else:
            screen_width = new_window.screen.width
            screen_height = new_window.screen.height
            pos_x = random.randint(0, max(0, screen_width - WINDOW_WIDTH))
            pos_y = random.randint(0, max(0, screen_height - WINDOW_HEIGHT))
            new_window.set_location(pos_x, pos_y)

        if sys.platform == "win32":
            window_manager.setup_window(new_window, window_id)
            # Apply saved global opacity
            if _global_opacity != 100:
                window_manager.set_opacity(new_window, window_id, _global_opacity)

    except Exception as e:
        log.error(f"Error creating Pyglet window for soul {window_id}: {e}")
        if new_window:
            new_window.close()
        return None

    soul_name = name if name is not None else f"Soul {window_id}"
    soul_app = SoulApp(
        new_window,
        orb_color_rgb,
        aura_color_rgb,
        soul_name,
        on_right_click=handle_soul_right_click,
        on_move_end=persist_souls_state,
    )
    soul_app.window_id = window_id  # Store window_id for later use
    active_souls.append(soul_app)
    active_windows.append(new_window)
    log.info(f"Spawned new soul: {soul_name}")
    return soul_app


def handle_soul_right_click(soul_app, screen_x, screen_y):
    """Handle right-click on a soul window."""
    log.debug(f"Launcher: Handling right click for {soul_app.name}")

    def on_edit():
        def on_apply(name, orb, aura):
            # Schedule update on Pyglet main thread to avoid GL context issues
            def apply_changes(dt):
                soul_app.name = name
                soul_app.orb_color_rgb = orb
                soul_app.aura_color_rgb = aura
                soul_app.orb_renderer.base_color_rgb = orb
                soul_app.aura_renderer.base_color_rgb = aura
                # Update physics label if it exists
                if hasattr(soul_app.window_physics, "name_label"):
                    soul_app.window_physics.name_label.text = name

            pyglet.clock.schedule_once(apply_changes, 0)

        # Run dialog in separate thread
        dialog_thread = threading.Thread(
            target=lambda: SoulSettingsDialog(
                name=soul_app.name,
                orb_color=soul_app.orb_color_rgb,
                aura_color=soul_app.aura_color_rgb,
                on_apply=on_apply,
            ).show(),
            daemon=True,
        )
        dialog_thread.start()

    def on_toggle_aura():
        soul_app.aura_visible = not soul_app.aura_visible

    def on_dismiss():
        log.info(f"Dismissing soul: {soul_app.name}")
        if soul_app in active_souls:
            active_souls.remove(soul_app)
        if soul_app.window in active_windows:
            active_windows.remove(soul_app.window)

        # Schedule cleanup on main thread
        pyglet.clock.schedule_once(lambda dt: _cleanup_soul_instance(soul_app), 0)

    # Spawn menu in thread
    menu_thread = threading.Thread(
        target=lambda: SoulContextMenu(
            soul_app.name, on_edit, on_toggle_aura, on_dismiss
        ).show(screen_x, screen_y),
        daemon=True,
    )
    menu_thread.start()


def _cleanup_soul_instance(soul_app):
    """Helper to cleanup a single soul instance on main thread."""
    try:
        soul_app.cleanup()
        soul_app.window.close()
    except Exception as e:
        log.error(f"Error cleaning up soul {soul_app.name}: {e}")


def toggle_all_auras():
    """Toggle aura visibility for all active souls."""
    for soul in active_souls:
        soul.aura_visible = not soul.aura_visible
    state = "visible" if active_souls and active_souls[0].aura_visible else "hidden"
    log.info(f"All auras now {state}")


def on_add_soul_from_tray():
    """Handle 'Add Soul' from system tray menu."""

    # Run dialog in main thread via pyglet.clock
    def show_dialog(dt):
        def on_apply(name, orb_color, aura_color):
            # Schedule soul creation on main thread
            pyglet.clock.schedule_once(
                lambda dt: create_new_soul_window(orb_color, aura_color, name), 0
            )

        # Create dialog in separate thread to avoid blocking Pyglet
        dialog_thread = threading.Thread(
            target=lambda: SoulSettingsDialog(on_apply=on_apply).show(), daemon=True
        )
        dialog_thread.start()

    pyglet.clock.schedule_once(show_dialog, 0)


def on_settings_from_tray():
    """Handle 'Settings...' from system tray menu."""
    log.info(
        f"Opening settings with: opacity={_global_opacity}, run_on_startup={_run_on_startup}"
    )

    def on_apply(opacity, run_on_startup):
        # Schedule settings update on main thread
        pyglet.clock.schedule_once(
            lambda dt: apply_global_settings(opacity, run_on_startup),
            0,
        )

    def run_dialog():
        GlobalSettingsDialog(
            current_opacity=_global_opacity,
            run_on_startup=_run_on_startup,
            on_apply=on_apply,
        ).show()

    # Show dialog in separate thread
    dialog_thread = threading.Thread(target=run_dialog, daemon=True)
    dialog_thread.start()


def apply_global_settings(opacity, run_on_startup):
    """Apply global settings to all active souls."""
    global _global_opacity, _run_on_startup

    _global_opacity = opacity
    _run_on_startup = run_on_startup

    # Persist settings to settings.json
    save_settings(opacity)

    log.info(
        f"Applying global settings: opacity={opacity}%, run_on_startup={run_on_startup}"
    )

    # Apply opacity to all souls
    for soul in active_souls:
        wid = getattr(soul, "window_id", None)
        if wid is not None:
            window_manager.set_opacity(soul.window, wid, opacity)

    # Handle startup setting (Windows registry)
    if sys.platform == "win32":
        _set_windows_startup(run_on_startup)


def _set_windows_startup(enabled):
    """Add or remove Soulscape from Windows startup registry."""
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "Soulscape"

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        )
        if enabled:
            # Get the path to the current executable
            exe_path = sys.executable
            script_path = sys.argv[0] if sys.argv else ""
            if script_path and not script_path.endswith(".exe"):
                # Running as script, use pythonw to hide console
                cmd = f'"{exe_path}" "{script_path}"'
            else:
                cmd = f'"{exe_path}"'
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, cmd)
            log.info("Added Soulscape to Windows startup.")
        else:
            try:
                winreg.DeleteValue(key, app_name)
                log.info("Removed Soulscape from Windows startup.")
            except FileNotFoundError:
                pass  # Key doesn't exist, nothing to delete
        winreg.CloseKey(key)
    except Exception as e:
        log.error(f"Error modifying Windows startup: {e}")


def on_exit_from_tray():
    """Handle 'Exit' from system tray menu."""
    # Schedule cleanup on main thread
    pyglet.clock.schedule_once(lambda dt: cleanup_and_exit(), 0)


def cleanup_and_exit():
    """Clean up all resources and exit the application."""
    global tray_controller

    log.info("Cleaning up...")

    # Save soul states before cleanup
    if active_souls:
        soul_states = [soul.to_dict() for soul in active_souls]
        save_souls(soul_states)

    # Stop tray
    if tray_controller:
        tray_controller.stop()
        tray_controller = None

    # Cleanup souls
    for soul_app in active_souls:
        soul_app.cleanup()

    # Close windows
    for window_inst in active_windows:
        if not window_inst.has_exit:
            window_inst.close()

    log.info("All souls and windows cleaned up.")
    pyglet.app.exit()


if __name__ == "__main__":
    # Initialize system tray
    tray_controller = TrayController(
        on_add_soul=on_add_soul_from_tray,
        on_toggle_auras=toggle_all_auras,
        on_settings=on_settings_from_tray,
        on_exit=on_exit_from_tray,
    )
    tray_controller.start()
    log.info("Soulscape started. Use system tray to add souls or exit.")

    # Load saved souls or create default
    saved_souls = load_souls()
    if saved_souls:
        for soul_data in saved_souls:
            pos = soul_data.get("position", (100, 100))
            create_new_soul_window(
                orb_color_rgb=soul_data["orb_color"],
                aura_color_rgb=soul_data["aura_color"],
                name=soul_data["name"],
                position=pos,
            )
    else:
        # Create one default soul if no saved souls exist
        create_new_soul_window(
            orb_color_rgb=(0.3, 0.0, 0.3),  # Dark purple
            aura_color_rgb=(0.0, 1.0, 0.0),  # Green
            name="Dekute",
        )

    try:
        pyglet.app.run()
    except Exception as e:
        log.exception(f"An error occurred during Pyglet app run: {e}")
    finally:
        cleanup_and_exit()
