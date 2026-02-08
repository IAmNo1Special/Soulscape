"""Separate process runner for Soulscape GUI dialogs.

Avoids threading conflicts between Pyglet and Tkinter.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

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
    initial_opacity: float,
    run_on_startup: bool,
    current_hotkey: str,
    result_queue: Any,
) -> None:
    """Run Global Settings Dialog in a separate process.

    Args:
        initial_opacity: Current opacity value.
        run_on_startup: Whether run on startup is enabled.
        current_hotkey: Current hotkey string.
        result_queue: Queue to put results in.
    """
    try:

        def on_apply(opacity: float, startup: bool) -> None:
            result_queue.put({"opacity": opacity, "startup": startup})

        dialog = GlobalSettingsDialog(
            current_opacity=initial_opacity,
            run_on_startup=run_on_startup,
            on_apply=on_apply,
        )
        dialog.show()
    except Exception as e:
        # Cannot use standard logger in separate process easily without config
        print(f"Error in global settings process: {e}")
    finally:
        result_queue.put(None)  # Signal done


def run_soul_settings(
    name: str,
    orb_color: str,
    aura_color: str,
    result_queue: Any,
) -> None:
    """Run Soul Settings Dialog in a separate process.

    Args:
        name: Current soul name.
        orb_color: Current orb color hex.
        aura_color: Current aura color hex.
        result_queue: Queue to put results in.
    """
    try:

        def on_apply(new_name: str, new_orb: str, new_aura: str) -> None:
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


def run_context_menu(name: str, x: int, y: int, result_queue: Any) -> None:
    """Run Context Menu in a separate process.

    Args:
        name: Name of the soul.
        x: Screen x-coordinate.
        y: Screen y-coordinate.
        result_queue: Queue to put results in.
    """
    try:

        def callback_wrapper(action: MenuAction) -> None:
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
