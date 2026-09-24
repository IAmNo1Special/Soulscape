from typing import Final

# Window Dimensions
SOUL_WIDTH: Final[int] = 50
SOUL_HEIGHT: Final[int] = 80
WINDOW_OVERSHOOT: Final[int] = 0

# Physics Parameters for Window Movement

HOVER_AMPLITUDE: Final[float] = 0.8  # Vertical hover range in pixels
HOVER_FREQUENCY: Final[float] = 1.5  # Hover cycles per second

ROAM_PAUSE_MIN: Final[float] = 1.0  # Minimum pause time at a roaming target
ROAM_PAUSE_MAX: Final[float] = 3.0  # Maximum pause time at a roaming target
