class InputRouter:
    """
    Handles input routing and hit testing for Soul entities on the overlay window.
    """

    def __init__(self):
        self.hovered_soul = None
        self.dragged_soul = None

    def get_soul_at(self, souls, x, y, window_height):
        """
        Returns the top-most Soul at the given screen coordinates (x, y).

        Args:
            souls (list): List of Soul instances (assumed drawn in order).
            x (int): Screen X coordinate.
            y (int): Screen Y coordinate (Pyglet coordinates, bottom-left origin).
            window_height (int): Height of the window for Y-inversion.

        Returns:
            Soul or None
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
