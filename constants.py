# constants.py
import os
import sys


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


# Window Dimensions
WINDOW_WIDTH = 100
WINDOW_HEIGHT = 100

# Physics Parameters for Window Movement

HOVER_AMPLITUDE = 0.8  # Vertical hover range in pixels
HOVER_FREQUENCY = 1.5  # Hover cycles per second

ROAM_PAUSE_MIN = 1.0  # Minimum pause time at a roaming target
ROAM_PAUSE_MAX = 3.0  # Maximum pause time at a roaming target

# Orb Properties
ORB_RADIUS = 0.1
ORB_LAT_SEGMENTS = 32
ORB_LONG_SEGMENTS = 32
ORB_BULGE_STRENGTH = 0.3  # Fixed bulge strength for the orb
ORB_SCALE = 1.0  # Overall scale of the main orb
ORB_Y_OFFSET = -0.15  # Y position adjustment for the orb

# Aura Properties
AURA_RADIUS = 0.4
AURA_LAT_SEGMENTS = 64
AURA_LONG_SEGMENTS = 64
AURA_BASE_BRIGHTNESS = 1.2  # Base brightness for the aura
AURA_BULGE_STRENGTH = 0.1  # Fixed bulge strength for the aura
AURA_SCALE_X = 1.0  # X scale for the aura
AURA_SCALE_Y = 1.0  # Y scale for the aura
AURA_SCALE_Z = 1.0  # Z scale for the aura

# Camera and Animation Properties
# CAMERA_DISTANCE removed from here to be calculated dynamically in soul.py
CAMERA_FOV = 45.0  # Field of view for the perspective projection (kept constant)
CAMERA_ROTATION_SPEED = 0.2  # Speed of the subtle camera rotation


# Shader File Paths
SHADER_DIR = resource_path("shaders/")
ORB_VERTEX_SHADER_PATH = f"{SHADER_DIR}orbs/default/default_orb.vert"
ORB_FRAGMENT_SHADER_PATH = f"{SHADER_DIR}orbs/default/default_orb.frag"
AURA_VERTEX_SHADER_PATH = f"{SHADER_DIR}auras/default/default_aura.vert"
AURA_FRAGMENT_SHADER_PATH = f"{SHADER_DIR}auras/default/default_aura.frag"
