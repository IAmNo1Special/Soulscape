# constants.py
import os
import sys


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    if hasattr(sys, "_MEIPASS"):
        # PyInstaller path
        base_path = sys._MEIPASS
    else:
        # Resolve path relative to this file's location
        # This ensures it works even if run from a different CWD
        base_path = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(base_path, relative_path)


# Window Dimensions
WINDOW_WIDTH = 50
WINDOW_HEIGHT = 65
WINDOW_OVERSHOOT = 40

# Physics Parameters for Window Movement

HOVER_AMPLITUDE = 0.8  # Vertical hover range in pixels
HOVER_FREQUENCY = 1.5  # Hover cycles per second

ROAM_PAUSE_MIN = 1.0  # Minimum pause time at a roaming target
ROAM_PAUSE_MAX = 3.0  # Maximum pause time at a roaming target

# Orb Properties
ORB_RADIUS = 0.1
ORB_LAT_SEGMENTS = 32
ORB_LONG_SEGMENTS = 32
ORB_BULGE_STRENGTH = 0.15  # Fixed bulge strength for the orb
ORB_SCALE = 1.0  # Overall scale of the main orb
ORB_Y_OFFSET = -0.25  # Y position adjustment for the orb

# Aura Properties
AURA_RADIUS = 0.3
AURA_LAT_SEGMENTS = 32
AURA_LONG_SEGMENTS = 32
AURA_BASE_BRIGHTNESS = 1.0  # Base brightness for the aura
AURA_BULGE_STRENGTH = 0.2  # Fixed bulge strength for the aura
AURA_SCALE_X = 1.0  # X scale for the aura
AURA_SCALE_Y = 1.0  # Y scale for the aura
AURA_SCALE_Z = 1.0  # Z scale for the aura
AURA_Y_OFFSET = -0.15  # Y position adjustment for the aura

# Camera and Animation Properties
# CAMERA_DISTANCE removed from here to be calculated dynamically in soul.py
CAMERA_FOV = 50.0  # Field of view for the perspective projection (kept constant)
CAMERA_ROTATION_SPEED = 0.2  # Speed of the subtle camera rotation


# Shader File Paths
SHADER_DIR = resource_path("shaders/")
ORB_VERTEX_SHADER_PATH = f"{SHADER_DIR}orbs/default/default_orb.vert"
ORB_FRAGMENT_SHADER_PATH = f"{SHADER_DIR}orbs/default/default_orb.frag"
AURA_VERTEX_SHADER_PATH = f"{SHADER_DIR}auras/default/default_aura.vert"
AURA_FRAGMENT_SHADER_PATH = f"{SHADER_DIR}auras/default/default_aura.frag"
