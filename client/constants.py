from pathlib import Path
from typing import Final

from .utils.helpers import resource_path

# Window Dimensions
SOUL_WIDTH: Final[int] = 50
SOUL_HEIGHT: Final[int] = 80
WINDOW_OVERSHOOT: Final[int] = 0

# Physics Parameters for Window Movement

HOVER_AMPLITUDE: Final[float] = 0.8  # Vertical hover range in pixels
HOVER_FREQUENCY: Final[float] = 1.5  # Hover cycles per second

ROAM_PAUSE_MIN: Final[float] = 1.0  # Minimum pause time at a roaming target
ROAM_PAUSE_MAX: Final[float] = 3.0  # Maximum pause time at a roaming target

# Orb Properties
ORB_RADIUS: Final[float] = 0.1
ORB_LAT_SEGMENTS: Final[int] = 32
ORB_LONG_SEGMENTS: Final[int] = 32
ORB_BULGE_STRENGTH: Final[float] = 0.15  # Fixed bulge strength for the orb
ORB_SCALE: Final[float] = 1.0  # Overall scale of the main orb
ORB_Y_OFFSET: Final[float] = -0.28  # Y position adjustment for the orb

# Aura Properties
AURA_RADIUS: Final[float] = 0.3
AURA_LAT_SEGMENTS: Final[int] = 32
AURA_LONG_SEGMENTS: Final[int] = 32
AURA_BASE_BRIGHTNESS: Final[float] = 0.8  # Base brightness for the aura
AURA_BULGE_STRENGTH: Final[float] = 0.2  # Fixed bulge strength for the aura
AURA_SCALE_X: Final[float] = 1.0  # X scale for the aura
AURA_SCALE_Y: Final[float] = 1.0  # Y scale for the aura
AURA_SCALE_Z: Final[float] = 1.0  # Z scale for the aura
AURA_Y_OFFSET: Final[float] = -0.15  # Y position adjustment for the aura

# Camera and Animation Properties
# CAMERA_DISTANCE removed from here to be calculated dynamically in soul.py
CAMERA_FOV: Final[float] = (
    60.0  # Field of view for the perspective projection (kept constant)
)
CAMERA_ROTATION_SPEED: Final[float] = 0.2  # Speed of the subtle camera rotation


# Shader File Paths
SHADER_DIR: Final[Path] = resource_path("shaders/")
ORB_VERTEX_SHADER_PATH: Final[Path] = (
    SHADER_DIR / "orbs/default/default_orb.vert"
)
ORB_FRAGMENT_SHADER_PATH: Final[Path] = (
    SHADER_DIR / "orbs/default/default_orb.frag"
)
AURA_VERTEX_SHADER_PATH: Final[Path] = (
    SHADER_DIR / "auras/default/default_aura.vert"
)
AURA_FRAGMENT_SHADER_PATH: Final[Path] = (
    SHADER_DIR / "auras/default/default_aura.frag"
)
