import math

from pyglet.gl import GL_TRIANGLES

from soulscape.constants import (
    AURA_FRAGMENT_SHADER_PATH,
    AURA_LAT_SEGMENTS,
    AURA_LONG_SEGMENTS,
    AURA_RADIUS,
    AURA_VERTEX_SHADER_PATH,
    ORB_FRAGMENT_SHADER_PATH,
    ORB_LAT_SEGMENTS,
    ORB_LONG_SEGMENTS,
    ORB_RADIUS,
    ORB_VERTEX_SHADER_PATH,
)
from soulscape.utils.shader_utils import create_shader_program


def create_sphere(radius, lat_segments, long_segments):
    """
    Generates vertex, normal, and index data for a sphere using shared pole vertices.

    Args:
        radius (float): The radius of the sphere.
        lat_segments (int): Number of segments along the latitude.
        long_segments (int): Number of segments along the longitude.

    Returns:
        dict: A dictionary containing 'vertices', 'normals', and 'indices' arrays.
    """
    vertices = []
    normals = []
    indices = []

    # North Pole
    vertices.extend([0.0, radius, 0.0])
    normals.extend([0.0, 1.0, 0.0])

    # South Pole
    vertices.extend([0.0, -radius, 0.0])
    normals.extend([0.0, -1.0, 0.0])

    # Vertices between poles
    for i in range(1, lat_segments):
        lat_frac = i / float(lat_segments)
        # Map lat_frac (0..1) to angle (pi/2 .. -pi/2)
        lat_angle = (math.pi / 2) - (lat_frac * math.pi)

        y = radius * math.sin(lat_angle)
        xz = radius * math.cos(lat_angle)

        for j in range(long_segments):
            long_frac = j / float(long_segments)
            long_angle = long_frac * 2 * math.pi

            x = xz * math.cos(long_angle)
            z = xz * math.sin(long_angle)

            vertices.extend([x, y, z])

            # Normal is normalized vector from center
            inv_len = 1.0 / math.sqrt(x * x + y * y + z * z)
            normals.extend([x * inv_len, y * inv_len, z * inv_len])

            # UV coordinates could be added here if needed

    # Indices
    north_pole_idx = 0
    south_pole_idx = 1
    first_ring_start_idx = 2

    # North Pole connectivity
    for j in range(long_segments):
        next_j = (j + 1) % long_segments
        indices.extend(
            [
                north_pole_idx,
                first_ring_start_idx + j,
                first_ring_start_idx + next_j,
            ]
        )

    # Intermediate rings
    num_rings = lat_segments - 1

    for i in range(num_rings - 1):  # Connect generic rings
        current_ring_start = first_ring_start_idx + i * long_segments
        next_ring_start = first_ring_start_idx + (i + 1) * long_segments

        for j in range(long_segments):
            next_j = (j + 1) % long_segments

            v0 = current_ring_start + j
            v1 = next_ring_start + j
            v2 = next_ring_start + next_j
            v3 = current_ring_start + next_j

            # Triangle 1
            indices.extend([v0, v1, v2])
            # Triangle 2
            indices.extend([v0, v2, v3])

    # South Pole connectivity
    last_ring_start = first_ring_start_idx + (num_rings - 1) * long_segments
    for j in range(long_segments):
        next_j = (j + 1) % long_segments
        indices.extend(
            [last_ring_start + j, south_pole_idx, last_ring_start + next_j]
        )

    return {"vertices": vertices, "normals": normals, "indices": indices}


class ResourceManager:
    """
    Singleton manager for shared graphics resources (shaders, meshes).
    Ensures that heavy assets are loaded only once.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ResourceManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.orb_program = None
        self.aura_program = None

        self.orb_mesh_data = None
        self.aura_mesh_data = None

        self.orb_vertex_list = None
        self.aura_vertex_list = None

        self._initialized = True

    def get_orb_program(self):
        """Returns the compiled shader program for the Orb."""
        if self.orb_program is None:
            self.orb_program = create_shader_program(
                ORB_VERTEX_SHADER_PATH, ORB_FRAGMENT_SHADER_PATH
            )
        return self.orb_program

    def get_aura_program(self):
        """Returns the compiled shader program for the Aura."""
        if self.aura_program is None:
            self.aura_program = create_shader_program(
                AURA_VERTEX_SHADER_PATH, AURA_FRAGMENT_SHADER_PATH
            )
        return self.aura_program

    def get_orb_mesh_data(self):
        """Returns dict(vertices, normals, indices) for the Orb sphere."""
        if self.orb_mesh_data is None:
            self.orb_mesh_data = create_sphere(
                ORB_RADIUS, ORB_LAT_SEGMENTS, ORB_LONG_SEGMENTS
            )
        return self.orb_mesh_data

    def get_aura_mesh_data(self):
        """Returns dict(vertices, normals, indices) for the Aura sphere."""
        if self.aura_mesh_data is None:
            self.aura_mesh_data = create_sphere(
                AURA_RADIUS, AURA_LAT_SEGMENTS, AURA_LONG_SEGMENTS
            )
        return self.aura_mesh_data

    def get_orb_vertex_list(self):
        """Returns the cached VertexList for the orb.
        Note: This currently returns an indexed vertex list bound to the shader.
        This vertex list can be drawn multiple times.
        """
        if self.orb_vertex_list is None:
            program = self.get_orb_program()
            data = self.get_orb_mesh_data()
            self.orb_vertex_list = program.vertex_list_indexed(
                len(data["vertices"]) // 3,
                GL_TRIANGLES,
                data["indices"],
                position=("f", data["vertices"]),
                normal=("f", data["normals"]),
            )
        return self.orb_vertex_list

    def get_aura_vertex_list(self):
        """Returns the cached VertexList for the aura."""
        if self.aura_vertex_list is None:
            program = self.get_aura_program()
            data = self.get_aura_mesh_data()
            self.aura_vertex_list = program.vertex_list_indexed(
                len(data["vertices"]) // 3,
                GL_TRIANGLES,
                data["indices"],
                position=("f", data["vertices"]),
                normal=("f", data["normals"]),
            )
        return self.aura_vertex_list


# Global instance
resource_manager = ResourceManager()
