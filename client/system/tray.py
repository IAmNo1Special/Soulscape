"""System tray controller for Soulscape using pystray."""

from __future__ import annotations

import threading
from typing import Any, Callable

import pystray
from PIL import Image, ImageDraw
from pystray import MenuItem as Item


class TrayController:
    """Manages the system tray icon and menu for Soulscape."""

    def __init__(
        self,
        on_add_soul: Callable[[], None],
        on_toggle_auras: Callable[[], None],
        on_settings: Callable[[], None],
        on_message_board: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        """Initialize the tray controller.

        Args:
            on_add_soul: Callback when "Add Soul" is clicked.
            on_toggle_auras: Callback when "Toggle All Auras" is clicked.
            on_settings: Callback when "Settings..." is clicked.
            on_message_board: Callback when "Message Board" is clicked.
            on_exit: Callback when "Exit" is clicked.
        """
        self.on_add_soul = on_add_soul
        self.on_toggle_auras = on_toggle_auras
        self.on_settings = on_settings
        self.on_message_board = on_message_board
        self.on_exit = on_exit
        self.icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None

    def _create_icon_image(self, size: int = 64) -> Image.Image:
        """Create a simple orb-like icon for the system tray."""
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        # Draw outer glow (aura)
        center = size // 2
        for i in range(center, 8, -2):
            alpha = int(255 * (1 - i / center) * 0.5)
            draw.ellipse(
                [center - i, center - i, center + i, center + i],
                fill=(100, 200, 100, alpha),
            )

        # Draw inner orb
        orb_radius = size // 4
        draw.ellipse(
            [
                center - orb_radius,
                center - orb_radius,
                center + orb_radius,
                center + orb_radius,
            ],
            fill=(80, 180, 80, 255),
        )

        # Draw highlight
        highlight_radius = orb_radius // 2
        draw.ellipse(
            [
                center - highlight_radius - 2,
                center - highlight_radius - 2,
                center,
                center,
            ],
            fill=(150, 255, 150, 200),
        )

        return image

    def _create_menu(self) -> pystray.Menu:
        """Create the right-click context menu."""
        return pystray.Menu(
            Item("Add Soul", self._on_add_soul),
            Item("Toggle All Auras", self._on_toggle_auras),
            pystray.Menu.SEPARATOR,
            Item("Message Board", self._on_message_board),
            Item("Settings...", self._on_settings),
            pystray.Menu.SEPARATOR,
            Item("Exit Soulscape", self._on_exit),
        )

    def _on_add_soul(self, icon: Any, item: Any) -> None:
        """Handle Add Soul menu click."""
        if self.on_add_soul:
            self.on_add_soul()

    def _on_toggle_auras(self, icon: Any, item: Any) -> None:
        """Handle Toggle All Auras menu click."""
        if self.on_toggle_auras:
            self.on_toggle_auras()

    def _on_settings(self, icon: Any, item: Any) -> None:
        """Handle Settings menu click."""
        if self.on_settings:
            self.on_settings()

    def _on_message_board(self, icon: Any, item: Any) -> None:
        """Handle Message Board menu click."""
        if self.on_message_board:
            self.on_message_board()

    def _on_exit(self, icon: Any, item: Any) -> None:
        """Handle Exit menu click."""
        if self.on_exit:
            self.on_exit()
        self.stop()

    def start(self) -> None:
        """Start the system tray icon in a background thread."""
        self.icon = pystray.Icon(
            "Soulscape",
            self._create_icon_image(),
            "Soulscape",
            self._create_menu(),
        )
        self._thread = threading.Thread(target=self.icon.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the system tray icon."""
        if self.icon:
            self.icon.stop()
            self.icon = None
