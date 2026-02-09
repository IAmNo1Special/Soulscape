"""Scene rendering logic for Soulscape."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyglet import math as pmath
from pyglet.gl import (
    GL_BACK,
    GL_BLEND,
    GL_CULL_FACE,
    GL_DEPTH_TEST,
    GL_ONE,
    GL_ONE_MINUS_SRC_ALPHA,
    GL_SRC_ALPHA,
    GL_TRIANGLES,
    glBlendFunc,
    glCullFace,
    glDepthMask,
    glDisable,
    glEnable,
    glViewport,
)

from soulscape.constants import (
    AURA_BASE_BRIGHTNESS,
    AURA_BULGE_STRENGTH,
    AURA_SCALE_X,
    AURA_SCALE_Y,
    AURA_SCALE_Z,
    AURA_Y_OFFSET,
    CAMERA_FOV,
    CAMERA_ROTATION_SPEED,
    ORB_BULGE_STRENGTH,
    ORB_SCALE,
    ORB_Y_OFFSET,
)
from soulscape.ui.graphics.resources import ResourceManager, resource_manager

if TYPE_CHECKING:
    from soulscape.core import Soul


class SceneRenderer:
    """Handles the rendering of all souls in the scene.

    Optimizes performance by minimizing state changes (batching by shader).
    """

    def __init__(self):
        """Initializes the scene renderer."""
        # We access resources via the singleton manager
        self.resources: ResourceManager = resource_manager

    def render(self, souls: list[Soul], overlay_height: int) -> None:
        """Render all souls.

        Args:
            souls: List of Soul instances.
            overlay_height: Height of the overlay window (needed for glViewport).
        """
        if not souls:
            return

        # --- Pass 1: Render Orbs ---
        program = self.resources.get_orb_program()
        vlist = self.resources.get_orb_vertex_list()

        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)
        glCullFace(GL_BACK)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        with program:
            # We can bind the program once

            # Common Matrices
            # Note: Projection depends on aspect ratio, which depends on soul size.
            # If all souls are same size, we can calculate projection ONCE.
            # Currently SOUL_WIDTH/HEIGHT are constants?
            # Yes. But let's be safe and calculate per soul if needed,
            # or optimize if they are constant.
            # Soul.width/height are set to constants.

            # Optimization: If all souls have same aspect ratio, calc Projection once.
            # But let's stick to per-soul logic for correctness first.

            for soul in souls:
                # 1. Setup Viewport (converts local 3D space to screen space)
                # soul.y is top-left Y (physics), we need bottom-left for GL.
                # using soul.draw_y which is the visual Y position
                viewport_y = overlay_height - soul.draw_y - soul.height
                glViewport(
                    int(soul.x),
                    int(viewport_y),
                    int(soul.width),
                    int(soul.height),
                )

                # 2. Upload Uniforms

                # View Matrix (Camera Orbit)
                view = pmath.Mat4.from_translation(
                    pmath.Vec3(0, 0, -soul.camera_distance)
                )
                view = view.rotate(
                    soul.time * CAMERA_ROTATION_SPEED, pmath.Vec3(0, 1, 0)
                )

                # Projection Matrix
                aspect_ratio = (
                    soul.width / soul.height if soul.height > 0 else 1.0
                )
                projection = pmath.Mat4.perspective_projection(
                    aspect_ratio, z_near=0.1, z_far=100.0, fov=CAMERA_FOV
                )

                # Model Matrix (Orb transformation within its viewport)
                model = pmath.Mat4()
                scaled_orb_model = model.scale(
                    (ORB_SCALE, ORB_SCALE, ORB_SCALE)
                )
                positioned_orb_model = scaled_orb_model.translate(
                    pmath.Vec3(0, ORB_Y_OFFSET, 0)
                )

                program["model"] = positioned_orb_model
                program["view"] = view
                program["projection"] = projection
                program["time"] = soul.time
                program["bulge_position"] = soul.bulge_position
                program["bulge_strength"] = ORB_BULGE_STRENGTH
                program["base_color_uniform"] = soul.orb_color_rgb

                # 3. Draw
                vlist.draw(GL_TRIANGLES)

        # --- Pass 2: Render Auras ---
        program = self.resources.get_aura_program()
        vlist = self.resources.get_aura_vertex_list()

        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        glDepthMask(False)
        glDisable(GL_CULL_FACE)

        with program:
            for soul in souls:
                if not soul.aura_visible:
                    continue

                # 1. Setup Viewport (Same as Orb)
                viewport_y = overlay_height - soul.draw_y - soul.height
                glViewport(
                    int(soul.x),
                    int(viewport_y),
                    int(soul.width),
                    int(soul.height),
                )

                # 2. Upload Uniforms

                # View/Proj must be recalculated or stored?
                # Recalculate is cheap enough compared to Uniform upload.
                view = pmath.Mat4.from_translation(
                    pmath.Vec3(0, 0, -soul.camera_distance)
                )
                view = view.rotate(
                    soul.time * CAMERA_ROTATION_SPEED, pmath.Vec3(0, 1, 0)
                )

                aspect_ratio = (
                    soul.width / soul.height if soul.height > 0 else 1.0
                )
                projection = pmath.Mat4.perspective_projection(
                    aspect_ratio, z_near=0.1, z_far=100.0, fov=CAMERA_FOV
                )

                model = pmath.Mat4()
                aura_model = model.scale(
                    (AURA_SCALE_X, AURA_SCALE_Y, AURA_SCALE_Z)
                )
                aura_model = aura_model.translate(
                    pmath.Vec3(0, AURA_Y_OFFSET, 0)
                )

                program["model"] = aura_model
                program["view"] = view
                program["projection"] = projection
                program["time"] = soul.time
                program["bulge_position"] = soul.bulge_position
                program["bulge_strength"] = AURA_BULGE_STRENGTH
                program["base_brightness"] = AURA_BASE_BRIGHTNESS
                program["base_color_uniform"] = soul.aura_color_rgb

                # 3. Draw
                vlist.draw(GL_TRIANGLES)

        # Restore Depth Mask for next frame (or other renderers)
        glDepthMask(True)
