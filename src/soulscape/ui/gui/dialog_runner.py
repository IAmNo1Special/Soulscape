"""
Separate process runner for Soulscape GUI dialogs.
Avoids threading conflicts between Pyglet and Tkinter.
"""

from enum import Enum

# Import the GUI classes (ensure these don't import pyglet/main app)
from soulscape.ui.gui.settings_gui import (
    GlobalSettingsDialog,
    SoulContextMenu,
    SoulSettingsDialog,
)


class MenuAction(str, Enum):
    EDIT = "EDIT"
    TOGGLE_AURA = "TOGGLE_AURA"
    DISMISS = "DISMISS"


def run_global_settings(
    initial_opacity, run_on_startup, current_hotkey, result_queue
):
    """Run Global Settings Dialog in a separate process."""
    try:

        def on_apply(opacity, startup):
            result_queue.put({"opacity": opacity, "startup": startup})

        dialog = GlobalSettingsDialog(
            current_opacity=initial_opacity,
            run_on_startup=run_on_startup,
            on_apply=on_apply,
        )
        dialog.show()
    except Exception as e:
        print(f"Error in global settings process: {e}")
    finally:
        result_queue.put(None)  # Signal done


def run_soul_settings(name, orb_color, aura_color, result_queue):
    """Run Soul Settings Dialog in a separate process."""
    try:

        def on_apply(new_name, new_orb, new_aura):
            result_queue.put(
                {"name": new_name, "orb_color": new_orb, "aura_color": new_aura}
            )

        dialog = SoulSettingsDialog(
            name=name,
            orb_color=orb_color,
            aura_color=aura_color,
            on_apply=on_apply,
        )
        dialog.show()
    except Exception as e:
        print(f"Error in soul settings process: {e}")
    finally:
        result_queue.put(None)


def run_context_menu(name, x, y, result_queue):
    """Run Context Menu in a separate process."""
    try:
        selected_action = None

        def on_edit():
            nonlocal selected_action
            selected_action = MenuAction.EDIT

        def on_toggle():
            nonlocal selected_action
            selected_action = MenuAction.TOGGLE_AURA

        def on_dismiss():
            nonlocal selected_action
            selected_action = MenuAction.DISMISS

        # We can't return immediately from callbacks, we just store state
        # But SoulContextMenu.show blocks.
        # We need to modify SoulContextMenu or wrap it to return action?
        # The current implementation executes callback immediately.
        # We can pass wrappers that put to queue.

        # Actually, simpler:
        def callback_wrapper(action):
            result_queue.put(action)

        menu = SoulContextMenu(
            soul_name=name,
            on_edit=lambda: callback_wrapper(MenuAction.EDIT),
            on_toggle_aura=lambda: callback_wrapper(MenuAction.TOGGLE_AURA),
            on_dismiss=lambda: callback_wrapper(MenuAction.DISMISS),
        )
        menu.show(x, y)

    except Exception as e:
        print(f"Error in context menu process: {e}")
    finally:
        result_queue.put(None)
