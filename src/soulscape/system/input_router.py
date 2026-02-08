"""Input routing system."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from soulscape.core.soul import Soul


class InputRouter:
    """Handles input routing and hit testing for Soul entities on the overlay window.

    Attributes:
        hovered_soul: The soul currently hovered by the mouse.
        dragged_soul: The soul currently being dragged.
    """

    def __init__(self) -> None:
        """Initializes the InputRouter."""
        self.hovered_soul: Soul | None = None
        self.dragged_soul: Soul | None = None

    def get_soul_at(
        self, souls: list[Soul], x: int, y: int, window_height: int
    ) -> Soul | None:
        """Returns the top-most Soul at the given screen coordinates (x, y).

        Args:
            souls: List of Soul instances (assumed drawn in order).
            x: Screen X coordinate.
            y: Screen Y coordinate (Pyglet coordinates, bottom-left origin).
            window_height: Height of the window for Y-inversion.

        Returns:
            The Soul at the coordinates, or None if no soul is found.
        """
        # Invert Y to match SoulPhysics Top-Left logic
        y_top_left = window_height - y

        # Iterate backwards to find the top-most soul first
        for soul in reversed(souls):
            # Check bounds against SoulPhysics coordinates
            # Soul (and SoulPhysics) x/y are Top-Left coordinates.
            if (
                soul.x <= x <= soul.x + soul.width
                and soul.y <= y_top_left <= soul.y + soul.height
            ):
                return soul

        return None
