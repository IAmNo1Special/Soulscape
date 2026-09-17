"""Scene rendering logic for Soulscape."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pyglet
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

from ...constants import (
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
from .resources import ResourceManager, resource_manager
from .soul_uniforms import (
    STATUE_COLLAPSED,
    STATUE_DORMANT,
    STATUE_OFFLINE,
    state_to_uniforms,
)

if TYPE_CHECKING:
    from ...core import Soul


def _statue_kind(soul: Soul) -> str | None:
    if soul.dormant_statue:
        return STATUE_DORMANT
    if soul.statue:
        return STATUE_COLLAPSED
    if soul.offline_stale:
        return STATUE_OFFLINE
    return None


def _soul_state(soul: Soul, base_color: tuple[float, float, float]) -> dict:
    # Issue #30: work mode dims visuals (tray toggle).
    if getattr(soul, "work_dim", False):
        base_color = tuple(c * 0.55 for c in base_color)
    biology = soul.biology
    return {
        "satiety": max(0.0, min(1.0, biology.satiety / 100.0)),
        "hydration": max(0.0, min(1.0, biology.hydration / 100.0)),
        "hp": biology.get_current_health() / max(1, biology.max_health()),
        "statue_kind": _statue_kind(soul),
        "typing_dip": soul.typing_dip,
        "reflex": soul.reflex_kind,
        "reflex_t": soul.reflex_t,
        "base_color": base_color,
    }


def _bob_offset(uniforms: dict, soul_time: float) -> float:
    return uniforms["bob_amplitude"] * math.sin(
        soul_time * uniforms["bob_speed"] + uniforms["bob_phase"]
    )


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
                uniforms = state_to_uniforms(
                    _soul_state(soul, soul.display_orb_color())
                )
                positioned_orb_model = scaled_orb_model.translate(
                    pmath.Vec3(
                        0, ORB_Y_OFFSET + _bob_offset(uniforms, soul.time), 0
                    )
                )

                program["model"] = positioned_orb_model
                program["view"] = view
                program["projection"] = projection
                program["time"] = soul.time
                program["bulge_position"] = soul.bulge_position
                program["bulge_strength"] = ORB_BULGE_STRENGTH
                program["base_color_uniform"] = uniforms["base_color_uniform"]
                program["desat_factor"] = uniforms["desat_factor"]
                program["brightness"] = uniforms["brightness"]
                program["opacity"] = uniforms["opacity"]
                program["pulse_rate"] = uniforms["pulse_rate"]
                program["pulse_strength"] = uniforms["pulse_strength"]

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
                uniforms = state_to_uniforms(
                    _soul_state(soul, soul.display_aura_color())
                )
                aura_model = aura_model.translate(
                    pmath.Vec3(
                        0, AURA_Y_OFFSET + _bob_offset(uniforms, soul.time), 0
                    )
                )

                program["model"] = aura_model
                program["view"] = view
                program["projection"] = projection
                program["time"] = soul.time
                program["bulge_position"] = soul.bulge_position
                program["bulge_strength"] = AURA_BULGE_STRENGTH
                program["base_brightness"] = (
                    uniforms["brightness"] * AURA_BASE_BRIGHTNESS
                )
                program["base_color_uniform"] = uniforms["base_color_uniform"]
                program["desat_factor"] = uniforms["desat_factor"]
                program["opacity"] = uniforms["opacity"]
                program["pulse_rate"] = uniforms["pulse_rate"]
                program["pulse_strength"] = uniforms["pulse_strength"]

                # 3. Draw
                vlist.draw(GL_TRIANGLES)

        # Restore Depth Mask for next frame (or other renderers)
        glDepthMask(True)

    def render_bubbles(self, jobs: list[tuple[float, float, str]]) -> None:
        """Draw transient speech bubbles (issue #30).

        Args:
            jobs: (x, y, text) draw jobs from BubbleManager.layout(),
                in window coordinates (origin bottom-left).
        """
        glDisable(GL_DEPTH_TEST)
        for x, y, text in jobs:
            label = pyglet.text.Label(
                text,
                x=x,
                y=y,
                anchor_x="center",
                anchor_y="bottom",
                font_size=12,
                bold=True,
                color=(235, 245, 255, 230),
            )
            label.draw()
        glEnable(GL_DEPTH_TEST)

    def render_nameplate(self, x: float, y: float, text: str) -> None:
        """Hover nameplate (issue #31): `name -- species . Lvl N` above the
        orb, read from viewport identity state."""
        glDisable(GL_DEPTH_TEST)
        label = pyglet.text.Label(
            text,
            x=x,
            y=y,
            anchor_x="center",
            anchor_y="bottom",
            font_size=11,
            color=(255, 255, 255, 235),
        )
        label.draw()
        glEnable(GL_DEPTH_TEST)

    def render_info_card(
        self,
        x: float,
        y: float,
        lines: list[str],
        window_width: float,
        window_height: float,
    ) -> None:
        """Right-click info card (issue #31): bordered panel with the
        soul's needs / activity / essence / whereabouts / presence."""
        if not lines:
            return
        font_size = 12
        pad = 10
        line_h = 20
        char_w = 7
        card_w = max(len(line) for line in lines) * char_w + pad * 2
        card_h = len(lines) * line_h + pad * 2
        # Anchor above the cursor; clamp inside the window.
        cx = min(max(x - card_w / 2, 4), max(window_width - card_w - 4, 4))
        cy = min(y + 12, max(window_height - card_h - 4, 4))
        glDisable(GL_DEPTH_TEST)
        pyglet.shapes.BorderedRectangle(
            cx,
            cy,
            card_w,
            card_h,
            border=2,
            color=(18, 22, 30),
            border_color=(120, 160, 220),
        ).draw()
        for i, line in enumerate(lines):
            pyglet.text.Label(
                line,
                x=cx + pad,
                y=cy + card_h - pad - (i + 0.8) * line_h,
                anchor_x="left",
                anchor_y="center",
                font_size=font_size,
                bold=(i == 0),
                color=(235, 245, 255, 235),
            ).draw()
        glEnable(GL_DEPTH_TEST)
