# soul.py
import math
import random
import time  # For double click timing

import pyautogui
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
    GLfloat,
    GLuint,
    glBlendFunc,
    glCullFace,
    glDepthMask,
    glDisable,
    glEnable,
    glViewport,
)
from pyglet.window import key, mouse

# Import constants and shader utilities
from constants import (
    AURA_BASE_BRIGHTNESS,
    AURA_BULGE_STRENGTH,
    AURA_FRAGMENT_SHADER_PATH,
    AURA_LAT_SEGMENTS,
    AURA_LONG_SEGMENTS,
    AURA_RADIUS,
    AURA_SCALE_X,
    AURA_SCALE_Y,
    AURA_SCALE_Z,
    AURA_VERTEX_SHADER_PATH,
    AURA_Y_OFFSET,
    CAMERA_FOV,
    CAMERA_ROTATION_SPEED,
    HOVER_AMPLITUDE,
    HOVER_FREQUENCY,
    ORB_BULGE_STRENGTH,
    ORB_FRAGMENT_SHADER_PATH,
    ORB_LAT_SEGMENTS,
    ORB_LONG_SEGMENTS,
    ORB_RADIUS,
    ORB_SCALE,
    ORB_VERTEX_SHADER_PATH,
    ORB_Y_OFFSET,
    ROAM_PAUSE_MAX,
    ROAM_PAUSE_MIN,
    WINDOW_OVERSHOOT,
)
from logger import log
from utils.shader_utils import create_shader_program


# --- Geometry Generation ---
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

    # North Pole (Index 0)
    vertices.extend([0.0, radius, 0.0])
    normals.extend([0.0, 1.0, 0.0])

    # Generate vertices for the rings (excluding poles)
    # lat goes from 1 to lat_segments - 1
    for lat in range(1, lat_segments):
        theta = lat * math.pi / lat_segments
        sin_theta = math.sin(theta)
        cos_theta = math.cos(theta)

        y = cos_theta
        for long in range(long_segments + 1):
            phi = long * 2 * math.pi / long_segments
            sin_phi = math.sin(phi)
            cos_phi = math.cos(phi)

            x = cos_phi * sin_theta
            z = sin_phi * sin_theta

            # For texturing, we might need duplicated vertices at the seam (long=0 and long=long_segments).
            # But for simple colored rendering, we could share them.
            # However, providing a seam allows valid UV mapping in the future.
            # We will keep the seam vertices (long + 1) per ring.

            vertices.extend([x * radius, y * radius, z * radius])
            normals.extend([x, y, z])

    # South Pole (Index: 1 + (lat_segments - 1) * (long_segments + 1))
    vertices.extend([0.0, -radius, 0.0])
    normals.extend([0.0, -1.0, 0.0])

    south_pole_index = len(vertices) // 3 - 1
    verts_per_ring = long_segments + 1

    # Generate Indices

    # 1. Top Cap (North Pole to first ring)
    # First ring starts at index 1
    for i in range(long_segments):
        # Triangle: NorthPole, Ring1[i], Ring1[i+1]
        indices.extend([0, 1 + i, 1 + i + 1])

    # 2. Body (Between rings)
    # We have lat_segments - 2 bands of quads (filled with 2 triangles)
    # Rings are indexed 0 to lat_segments-2 (internally in loop)
    # Current ring start index: 1 + ring_idx * verts_per_ring
    for i in range(lat_segments - 2):
        row_start = 1 + i * verts_per_ring
        next_row_start = row_start + verts_per_ring

        for j in range(long_segments):
            current = row_start + j
            next_val = next_row_start + j

            # Quad: current, next, current+1, next+1
            # Triangle 1
            indices.extend([current, next_val, current + 1])
            # Triangle 2
            indices.extend([next_val, next_val + 1, current + 1])

    # 3. Bottom Cap (Last ring to South Pole)
    last_ring_start = 1 + (lat_segments - 2) * verts_per_ring
    for i in range(long_segments):
        # Triangle: SouthPole, LastRing[i+1], LastRing[i]
        # Note winding order!
        indices.extend([south_pole_index, last_ring_start + i + 1, last_ring_start + i])

    return {
        "vertices": (GLfloat * len(vertices))(*vertices),
        "normals": (GLfloat * len(normals))(*normals),
        "indices": (GLuint * len(indices))(*indices),
    }


# --- Renderer Classes ---
class OrbRenderer:
    """
    Handles the rendering of the main orb.
    """

    def __init__(self, base_color_rgb):
        self.program = create_shader_program(
            ORB_VERTEX_SHADER_PATH, ORB_FRAGMENT_SHADER_PATH
        )
        sphere_data = create_sphere(ORB_RADIUS, ORB_LAT_SEGMENTS, ORB_LONG_SEGMENTS)
        self.vertex_list = self.program.vertex_list_indexed(
            len(sphere_data["vertices"]) // 3,
            GL_TRIANGLES,
            sphere_data["indices"],
            position=("f", sphere_data["vertices"]),
            normal=("f", sphere_data["normals"]),
        )
        self.base_color_rgb = base_color_rgb  # Store the base color

    def draw(self, model, view, projection, time, bulge_position):
        """
        Draws the orb with given matrices and uniform values.
        """
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)
        glCullFace(GL_BACK)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        with self.program:
            scaled_orb_model = model.scale((ORB_SCALE, ORB_SCALE, ORB_SCALE))
            positioned_orb_model = scaled_orb_model.translate(
                pmath.Vec3(0, ORB_Y_OFFSET, 0)
            )

            self.program["model"] = positioned_orb_model
            self.program["view"] = view
            self.program["projection"] = projection
            self.program["time"] = time
            self.program["bulge_position"] = bulge_position
            self.program["bulge_strength"] = ORB_BULGE_STRENGTH
            self.program["base_color_uniform"] = self.base_color_rgb

            self.vertex_list.draw(GL_TRIANGLES)


class AuraRenderer:
    """
    Handles the rendering of the aura surrounding the orb.
    """

    def __init__(self, base_color_rgb):
        self.program = create_shader_program(
            AURA_VERTEX_SHADER_PATH, AURA_FRAGMENT_SHADER_PATH
        )
        aura_data = create_sphere(AURA_RADIUS, AURA_LAT_SEGMENTS, AURA_LONG_SEGMENTS)
        self.vertex_list = self.program.vertex_list_indexed(
            len(aura_data["vertices"]) // 3,
            GL_TRIANGLES,
            aura_data["indices"],
            position=("f", aura_data["vertices"]),
            normal=("f", aura_data["normals"]),
        )
        self.base_color_rgb = base_color_rgb

    def draw(self, model, view, projection, time, bulge_position):
        """
        Draws the aura with given matrices and uniform values.
        """
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE)
        glDepthMask(False)
        glDisable(GL_CULL_FACE)

        with self.program:
            aura_model = model.scale((AURA_SCALE_X, AURA_SCALE_Y, AURA_SCALE_Z))
            aura_model = aura_model.translate(pmath.Vec3(0, AURA_Y_OFFSET, 0))

            self.program["model"] = aura_model
            self.program["view"] = view
            self.program["projection"] = projection
            self.program["time"] = time
            self.program["bulge_position"] = bulge_position
            self.program["bulge_strength"] = AURA_BULGE_STRENGTH
            self.program["base_brightness"] = AURA_BASE_BRIGHTNESS
            self.program["base_color_uniform"] = self.base_color_rgb

            self.vertex_list.draw(GL_TRIANGLES)


# --- Window Physics and Interaction ---
class WindowPhysics:
    """
    Manages the movement and interaction of a specific Pyglet window.
    """

    def __init__(self, window_ref, name="Soul", on_move_end=None):
        self.window = window_ref
        self.on_move_end = on_move_end
        initial_x, initial_y = self.window.get_location()
        self.x = float(initial_x)  # Window's top-left X on screen
        self.y = float(initial_y)  # Window's top-left Y on screen
        self.vx = 0.0
        self.vy = 0.0
        self.is_dragging = False
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.roaming_target = None
        self.roaming_pause = 0.0
        self.follow_mouse = False
        self.last_click_time = 0
        self.mouse_x = 0
        self.mouse_y = 0
        self.hover_phase = 0.0  # For hover animation
        self.follow_speed = 0.15  # Speed of following (lower is slower)
        self.max_speed = 0.5  # Maximum speed when following
        self.follow_delay = 0.0  # Timer for follow delay
        self.follow_delay_duration = 0.5  # 0.5 second delay before following starts
        self.name = name
        self.is_hovered = False

        # Track previous position for smooth movement
        self.last_x = int(self.x)
        self.last_y = int(self.y)

    def on_mouse_press(self, x_mouse_relative, y_mouse_relative, button, modifiers):
        """Handles mouse press events to initiate dragging."""
        if button == mouse.LEFT:
            # x_mouse_relative, y_mouse_relative are from window's bottom-left
            if (
                0 <= x_mouse_relative <= self.window.width
                and 0 <= y_mouse_relative <= self.window.height
            ):

                # Get current window screen position (top-left)
                win_screen_x, win_screen_y = self.window.get_location()

                # Check for double click (within 300ms)
                current_time = time.time()
                if current_time - self.last_click_time < 0.3:  # Double click detected
                    self.follow_mouse = not self.follow_mouse

                    if self.follow_mouse:
                        # Start follow delay timer
                        self.follow_delay = self.follow_delay_duration
                        # Stop any current movement
                        self.vx = 0.0
                        self.vy = 0.0
                        self.roaming_target = None
                        self.roaming_pause = 0.0
                    return  # Skip dragging on double click

                self.last_click_time = current_time

                # Start dragging
                self.is_dragging = True
                self.drag_offset_x = x_mouse_relative
                self.drag_offset_y = self.window.height - y_mouse_relative
                self.vx = 0.0  # Stop independent movement
                self.vy = 0.0

    def on_mouse_release(self, x_mouse, y_mouse, button, modifiers):
        """Handles mouse release events to stop dragging."""
        if button == mouse.LEFT:
            was_dragging = self.is_dragging
            self.is_dragging = False

            if was_dragging and self.on_move_end:
                self.on_move_end()

    def on_mouse_enter(self, x, y):
        """Handles mouse enter event to show the name label."""
        self.is_hovered = True

    def on_mouse_leave(self, x, y):
        """Handles mouse leave event to hide the name label."""
        self.is_hovered = False

    def on_mouse_motion(self, x, y, dx, dy):
        """Tracks mouse motion to update follow target."""
        # Update mouse position for following
        self.mouse_x = x
        self.mouse_y = y

    def on_mouse_drag(
        self, x_mouse_relative, y_mouse_relative, dx, dy, buttons, modifiers
    ):
        """
        Handles mouse drag events. Directly updates window position.
        x_mouse_relative, y_mouse_relative are current mouse coords relative to window's bottom-left.
        dx, dy are the change in these window-relative mouse positions.
        """
        if self.is_dragging and (buttons & mouse.LEFT):
            # The new window top-left (self.x, self.y) should be:
            # (current_mouse_absolute_screen_x - self.drag_offset_x,
            #  current_mouse_absolute_screen_y - self.drag_offset_y)

            # To get current_mouse_absolute_screen_x:
            # We know the window's current top-left is (self.x, self.y).
            # The mouse is at x_mouse_relative from the window's left edge.
            # So, current_mouse_absolute_screen_x = self.x (current window left) + x_mouse_relative.
            current_mouse_abs_x = self.x + x_mouse_relative

            # To get current_mouse_absolute_screen_y:
            # Window's top is self.y. Mouse y_mouse_relative is from window bottom.
            # Offset from window top is (self.window.height - y_mouse_relative).
            # So, current_mouse_absolute_screen_y = self.y (current window top) + (self.window.height - y_mouse_relative).
            current_mouse_abs_y = self.y + (self.window.height - y_mouse_relative)

            # Update mouse position for following if enabled
            self.mouse_x = current_mouse_abs_x
            self.mouse_y = current_mouse_abs_y

            # Now calculate the new window top-left position
            new_window_x = current_mouse_abs_x - self.drag_offset_x
            new_window_y = current_mouse_abs_y - self.drag_offset_y

            self.x = new_window_x
            self.y = new_window_y

            # Clamp to screen edges immediately during drag
            screen_width = self.window.screen.width
            screen_height = self.window.screen.height
            self.x = max(
                -WINDOW_OVERSHOOT,
                min(self.x, screen_width - self.window.width + WINDOW_OVERSHOOT),
            )
            self.y = max(
                -WINDOW_OVERSHOOT,
                min(self.y, screen_height - self.window.height + WINDOW_OVERSHOOT),
            )

            self.window.set_location(int(self.x), int(self.y))

    def update(self, dt):
        """Updates the window's position and physics."""

        if self.is_dragging:
            self.vx = 0.0
            self.vy = 0.0
            return

        # Handle following the mouse
        if self.follow_mouse:
            if self.follow_delay <= 0.0:
                self._update_follow_mouse(dt)
            else:
                self.follow_delay -= dt
            return

        # Roaming - only if not following and not dragging
        if self.roaming_pause > 0:
            self.roaming_pause -= dt
            if self.roaming_pause < 0:
                self.roaming_pause = 0

            # Hover effect when paused
            self.hover_phase += dt * HOVER_FREQUENCY * 2 * math.pi
            hover_y = math.sin(self.hover_phase) * HOVER_AMPLITUDE

            # Apply hover to Y position directly for display
            final_y = max(
                -WINDOW_OVERSHOOT,
                min(
                    self.y + hover_y,
                    self.window.screen.height - self.window.height + WINDOW_OVERSHOOT,
                ),
            )
            self.window.set_location(int(self.x), int(final_y))

        else:
            # Pick target if needed
            if self.roaming_target is None or random.random() < 0.005:
                screen_width = self.window.screen.width
                screen_height = self.window.screen.height
                padding = min(screen_width, screen_height) * 0.1

                tx = random.uniform(padding, screen_width - self.window.width - padding)
                ty = random.uniform(
                    padding, screen_height - self.window.height - padding
                )
                self.roaming_target = (tx, ty)

            # Move towards target
            if self.roaming_target:
                reached = self._move_towards_target(
                    self.roaming_target[0], self.roaming_target[1], dt
                )
                if reached:
                    self.roaming_target = None
                    self.roaming_pause = random.uniform(ROAM_PAUSE_MIN, ROAM_PAUSE_MAX)

    def _move_towards_target(self, target_x, target_y, dt):
        """
        Moves the window towards a target position with smooth deceleration.
        Returns True if the target has been reached (within dead zone).
        """
        # Calculate direction vector and distance
        dx = target_x - self.x
        dy = target_y - self.y
        distance_sq = dx * dx + dy * dy

        # Define a dead zone where we stop moving (squared for efficiency)
        dead_zone_sq = 4.0  # 2.0 pixels squared

        if distance_sq <= dead_zone_sq:
            return True  # Reached target

        # Calculate actual distance
        distance = math.sqrt(distance_sq)

        # Normalize direction
        inv_distance = 1.0 / distance
        dx *= inv_distance
        dy *= inv_distance

        # Calculate speed with smooth deceleration using an ease-out curve
        # This creates a more natural slowdown as we approach the target
        deceleration_distance = 50.0  # Distance over which to decelerate (pixels)
        speed_factor = min(1.0, distance / deceleration_distance)

        # Apply a smoother curve (ease-out cubic)
        speed_factor = 1.0 - (1.0 - speed_factor) ** 3

        # Calculate speed with frame-rate independence
        speed = self.max_speed * speed_factor

        # Calculate movement amount with sub-pixel precision
        move_amount = speed * 60.0 * dt  # 60.0 is our target FPS

        # Ensure we don't overshoot the target
        move_amount = min(move_amount, distance - math.sqrt(dead_zone_sq))

        # Update position with sub-pixel precision
        self.x += dx * move_amount
        self.y += dy * move_amount

        # Get screen bounds once
        screen_width = self.window.screen.width
        screen_height = self.window.screen.height
        window_width = self.window.width
        window_height = self.window.height

        # Clamp to screen edges with sub-pixel precision
        # Clamp to screen edges with sub-pixel precision
        self.x = max(
            float(-WINDOW_OVERSHOOT),
            min(self.x, screen_width - window_width + float(WINDOW_OVERSHOOT)),
        )
        self.y = max(
            float(-WINDOW_OVERSHOOT),
            min(self.y, screen_height - window_height + float(WINDOW_OVERSHOOT)),
        )

        # Only update window position if it changed by at least 1 pixel
        new_x, new_y = int(round(self.x)), int(round(self.y))
        if (new_x, new_y) != (self.last_x, self.last_y):
            self.window.set_location(new_x, new_y)
            self.last_x, self.last_y = new_x, new_y

        return False  # Still moving

    def _update_follow_mouse(self, dt):
        """Update the window position to follow the mouse."""
        mouse_x, mouse_y = pyautogui.position()
        target_x = mouse_x - (self.window.width // 2) - 20
        target_y = mouse_y - (self.window.height // 2) - 20
        self._move_towards_target(target_x, target_y, dt)


# --- Main Application Class ---
class SoulApp:
    """
    Main application class that orchestrates the orb, aura, and window physics
    for a single Pyglet window.
    """

    def __init__(
        self,
        window_instance,
        orb_color_rgb,
        aura_color_rgb,
        name=None,
        on_right_click=None,
        on_move_end=None,
    ):
        self.window = window_instance
        self.orb_color_rgb = orb_color_rgb  # Store for serialization
        self.aura_color_rgb = aura_color_rgb  # Store for serialization
        self.name = name if name is not None else f"Soul {len(pyglet.app.windows)}"
        self.on_right_click = on_right_click
        self.on_move_end = on_move_end
        self.orb_renderer = OrbRenderer(orb_color_rgb)
        self.aura_renderer = AuraRenderer(aura_color_rgb)
        self.time = 0.0
        self.bulge_position = [0.0, 0.0, 0.0]
        self.aura_visible = True  # Add flag to track aura visibility

        self.camera_distance = 1.0

        # Create window physics with the provided name or default
        self.window_physics = WindowPhysics(self.window, self.name, on_move_end)

        # Push handlers in layers - WindowPhysics first (bottom), then SoulApp (top)
        # This way SoulApp.on_mouse_press is called first and can intercept RIGHT clicks
        self.window.push_handlers(self.window_physics)
        self.window.push_handlers(
            on_draw=self.on_draw,
            on_resize=self.on_resize,
            on_mouse_press=self.on_mouse_press,
            on_key_press=self.on_key_press,
        )
        pyglet.clock.schedule_interval(self.update, 1 / 60.0)

    def to_dict(self):
        """
        Serialize the soul's state to a dictionary for persistence.

        Returns:
            dict with name, orb_color, aura_color, and position
        """
        if self.window_physics:
            x, y = int(self.window_physics.x), int(self.window_physics.y)
        else:
            x, y = self.window.get_location()
        return {
            "name": self.name,
            "orb_color": self.orb_color_rgb,
            "aura_color": self.aura_color_rgb,
            "position": (x, y),
        }

    def on_draw(self):
        """
        Main drawing function for this window, called by Pyglet.
        """
        self.window.clear()

        view = pmath.Mat4.from_translation(pmath.Vec3(0, 0, -self.camera_distance))
        view = view.rotate(self.time * CAMERA_ROTATION_SPEED, pmath.Vec3(0, 1, 0))

        aspect_ratio = (
            self.window.width / self.window.height if self.window.height > 0 else 1.0
        )
        projection = pmath.Mat4.perspective_projection(
            aspect_ratio, z_near=0.1, z_far=100.0, fov=CAMERA_FOV
        )

        model = pmath.Mat4.from_translation(pmath.Vec3(0, 0, 0))

        # Draw 3D elements
        if self.aura_visible:
            self.aura_renderer.draw(
                model, view, projection, self.time, self.bulge_position
            )
        self.orb_renderer.draw(model, view, projection, self.time, self.bulge_position)

    def on_resize(self, width, height):
        """
        Handles window resize events for this specific window.
        """
        if height == 0:
            height = 1
        glViewport(0, 0, width, height)
        return pyglet.event.EVENT_HANDLED

    def on_key_press(self, symbol, modifiers):
        """
        Handles key press events for this specific window.
        """
        if symbol == key.A:  # Toggle aura visibility when 'A' is pressed
            self.aura_visible = not self.aura_visible
            return pyglet.event.EVENT_HANDLED
        if symbol == key.ESCAPE:
            self.window.close()
            return pyglet.event.EVENT_HANDLED

    def update(self, dt):
        """
        Main update loop for this window, called periodically by Pyglet.
        """
        self.time += dt
        self.window_physics.update(dt)

        angle = self.time * 0.5
        self.bulge_position = [
            math.cos(angle) * 0.5,
            math.sin(angle * 0.7) * 0.35,
            math.sin(angle) * 0.5,
        ]

    def on_mouse_press(self, x, y, button, modifiers):
        """Handle mouse press events."""
        if button == mouse.RIGHT and self.on_right_click:
            # Convert window coordinates to screen coordinates
            # Pyglet window.get_location() returns top-left (x, y)
            # Mouse (x, y) are relative to bottom-left of window content
            win_x, win_y = self.window.get_location()
            screen_x = win_x + x
            screen_y = win_y + (self.window.height - y)
            self.on_right_click(self, screen_x, screen_y)
            return pyglet.event.EVENT_HANDLED

    def cleanup(self):
        """
        Cleans up all resources associated with this soul.
        Unschedules callbacks and cleans up OpenGL resources.
        """
        try:
            # Ensure the correct OpenGL context is active before deleting resources
            if self.window and hasattr(self.window, "context") and self.window.context:
                self.window.switch_to()
        except Exception as e:
            log.error(f"Error switching context during cleanup for {self.name}: {e}")

        # Unschedule the update callback
        try:
            pyglet.clock.unschedule(self.update)
        except Exception:
            pass  # May not be scheduled

        # Remove event handlers to prevent circular references or unwanted events
        if self.window:
            try:
                self.window.remove_handlers(self.window_physics)
                self.window.remove_handlers(
                    on_draw=self.on_draw,
                    on_resize=self.on_resize,
                    on_mouse_press=self.on_mouse_press,
                    on_key_press=self.on_key_press,
                )
            except Exception as e:
                log.warning(f"Error removing handlers for {self.name}: {e}")

        # Clean up orb renderer
        if hasattr(self, "orb_renderer"):
            if (
                hasattr(self.orb_renderer, "vertex_list")
                and self.orb_renderer.vertex_list
            ):
                try:
                    self.orb_renderer.vertex_list.delete()
                except Exception:
                    pass
            if hasattr(self.orb_renderer, "program") and self.orb_renderer.program:
                try:
                    self.orb_renderer.program.delete()
                except Exception:
                    pass

        # Clean up aura renderer
        if hasattr(self, "aura_renderer"):
            if (
                hasattr(self.aura_renderer, "vertex_list")
                and self.aura_renderer.vertex_list
            ):
                try:
                    self.aura_renderer.vertex_list.delete()
                except Exception:
                    pass
            if hasattr(self.aura_renderer, "program") and self.aura_renderer.program:
                try:
                    self.aura_renderer.program.delete()
                except Exception:
                    pass

        log.debug(f"Cleaned up resources for soul: {self.name}")
