# constants.py

# Window Dimensions
# User feedback: "waay too big" with previous 700x500.
# Adjusting to a smaller square window (250x250) to better fit the small soul
# while still providing padding.
WINDOW_WIDTH = 150
WINDOW_HEIGHT = 150

# Physics Parameters for Window Movement
FRICTION = 0.92           # Damping for smoother stop
ACCELERATION = 0.04       # Base acceleration
BOUNCE_COEFFICIENT = 0.75 # Energy retention on bounce (0.0 to 1.0)
HOVER_AMPLITUDE = 0.8     # Vertical hover range in pixels
HOVER_FREQUENCY = 1.5     # Hover cycles per second
ROAM_ACCELERATION = 0.03  # Roaming acceleration
MIN_FOLLOW_DISTANCE = 50  # Minimum distance for following behavior (not fully implemented in refactor)
MAX_FOLLOW_DISTANCE = 150 # Maximum distance for following behavior (not fully implemented in refactor)
ROAM_PAUSE_MIN = 1.0      # Minimum pause time at a roaming target
ROAM_PAUSE_MAX = 3.0      # Maximum pause time at a roaming target

# Orb Properties
ORB_RADIUS = 0.1
ORB_LAT_SEGMENTS = 64
ORB_LONG_SEGMENTS = 64
ORB_BULGE_STRENGTH = 0.3  # Fixed bulge strength for the orb
ORB_SCALE = 1.0          # Overall scale of the main orb
ORB_Y_OFFSET = -0.1       # Y position adjustment for the orb

# Aura Properties
AURA_RADIUS = 0.4
AURA_LAT_SEGMENTS = 128
AURA_LONG_SEGMENTS = 128
AURA_BASE_BRIGHTNESS = 1.2 # Base brightness for the aura
AURA_BULGE_STRENGTH = 0.1  # Fixed bulge strength for the aura
AURA_SCALE_X = 1.0        # X scale for the aura
AURA_SCALE_Y = 1.0         # Y scale for the aura
AURA_SCALE_Z = 1.0         # Z scale for the aura

# Camera and Animation Properties
# CAMERA_DISTANCE removed from here to be calculated dynamically in soul.py
CAMERA_FOV = 45.0         # Field of view for the perspective projection (kept constant)
CAMERA_ROTATION_SPEED = 0.2 # Speed of the subtle camera rotation
WANDER_SPEED = 0.3        # Speed of the orb's wandering motion
WANDER_AMPLITUDE = 0.5    # Amplitude of the orb's wandering motion

# Shader File Paths
SHADER_DIR = 'shaders/'
ORB_VERTEX_SHADER_PATH = f"{SHADER_DIR}orbs/default/default_orb.vert"
ORB_FRAGMENT_SHADER_PATH = f"{SHADER_DIR}orbs/default/default_orb.frag"
AURA_VERTEX_SHADER_PATH = f"{SHADER_DIR}auras/default/default_aura.vert"
AURA_FRAGMENT_SHADER_PATH = f"{SHADER_DIR}auras/default/default_aura.frag"
