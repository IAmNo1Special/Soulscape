"""Window configuration for Soulscape."""

from __future__ import annotations

import pyglet


class SoulscapeWindow(pyglet.window.Window):
    """Window manager for Soulscape."""

    def __init__(self) -> None:
        """Initializes the SoulscapeWindow."""
        screen = pyglet.display.get_display().get_default_screen()
        super().__init__(
            width=screen.width,
            height=screen.height - 0,  # Leave 1px at bottom for taskbar trigger
            style=pyglet.window.Window.WINDOW_STYLE_TRANSPARENT,
            screen=screen,
            vsync=True,
            resizable=False,
            visible=False,
            fullscreen=False,
        )
        self.set_location(0, 0)
