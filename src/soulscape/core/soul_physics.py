# soul.py
import math
import random
import time  # For double click timing

import pyautogui
import pyglet
from pyglet.window import mouse

# Import constants
from soulscape.constants import (
    HOVER_AMPLITUDE,
    HOVER_FREQUENCY,
    ROAM_PAUSE_MAX,
    ROAM_PAUSE_MIN,
    SOUL_HEIGHT,
    SOUL_WIDTH,
    WINDOW_OVERSHOOT,
)
from soulscape.system.logger import log


# --- Window Physics and Interaction ---
class SoulPhysics:
    """
    Manages the movement and interaction of a Soul entity.
    """

    def __init__(self, entity, on_move_end=None):
        log.debug(f"Initializing physics for Soul: {entity.name}")
        self.entity = entity
        self.window = None  # No direct window access, relies on entity.window (which is the overlay)
        self.on_move_end = on_move_end

        # Initialize from entity position
        self.x = float(self.entity.x)
        self.y = float(self.entity.y)

        self.vx = 0.0
        self.vy = 0.0
        self.is_dragging = False
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self.roaming_target = None
        self.roaming_pause = 0.0
        self.roaming_enabled = False  # Souls only move when instructed
        self.follow_mouse = False
        self.last_click_time = 0
        self.mouse_x = 0
        self.mouse_y = 0
        self.hover_phase = 0.0  # For hover animation
        self.mouse_y = 0
        self.hover_phase = 0.0  # For hover animation

        # Calculate speeds based on stats if available
        base_speed_val = 100.0
        if hasattr(entity, "stats") and entity.stats:
            speed_stat = entity.stats.speed
        else:
            speed_stat = base_speed_val

        # Speed modifiers (increased for better responsiveness)
        speed_multiplier = speed_stat / 100.0

        self.follow_speed = 1.5 * speed_multiplier
        self.max_speed = 5.0 * speed_multiplier

        log.debug(
            f"Physics initialized with max_speed={self.max_speed:.2f} (Stat: {speed_stat})"
        )
        self.follow_delay = 0.0  # Timer for follow delay
        self.follow_delay_duration = (
            0.5  # 0.5 second delay before following starts
        )
        self.name = entity.name
        self.is_hovered = False

        # Track previous position for smooth movement
        self.last_x = int(self.x)
        self.last_y = int(self.y)

        # Get screen dimensions from pyglet display since we don't have a specific window yet
        screen = pyglet.display.get_display().get_default_screen()
        self.screen_width = screen.width
        self.screen_height = screen.height

        self.width = SOUL_WIDTH
        self.height = SOUL_HEIGHT
        log.debug(
            f"Soul: {self.name} initialized with position ({self.x}, {self.y})"
        )

    def on_mouse_press(self, x, y, button, modifiers):
        """Handles mouse press events to initiate dragging.
        x, y are absolute screen coordinates passed from overlay.
        """
        if button == mouse.LEFT:
            # Check if click is inside this soul's bounds
            # x, y are absolute coordinates from overlay
            # soul is at self.x, self.y with size self.width, self.height
            if (
                self.x <= x <= self.x + self.width
                and self.y <= y <= self.y + self.height
            ):

                # Check for double click (within 300ms)
                current_time = time.time()
                if (
                    current_time - self.last_click_time < 0.3
                ):  # Double click detected
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
                self.drag_offset_x = x - self.x
                self.drag_offset_y = y - self.y
                self.vx = 0.0  # Stop independent movement
                self.vy = 0.0

    def on_mouse_release(self, x, y, button, modifiers):
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

    def on_mouse_drag(self, x, y, dx, dy, buttons, modifiers):
        """
        Handles mouse drag events. Directly updates soul position.
        x, y are absolute screen coordinates.
        """
        if self.is_dragging and (buttons & mouse.LEFT):
            # Calculate new top-left position based on drag offset
            new_soul_x = x - self.drag_offset_x
            new_soul_y = y - self.drag_offset_y

            self.x = new_soul_x
            self.y = new_soul_y

            # Update mouse position for following if enabled
            self.mouse_x = x
            self.mouse_y = y

            # Clamp to screen edges immediately during drag
            self.x = max(
                -WINDOW_OVERSHOOT,
                min(self.x, self.screen_width - self.width + WINDOW_OVERSHOOT),
            )
            self.y = max(
                -WINDOW_OVERSHOOT,
                min(
                    self.y, self.screen_height - self.height + WINDOW_OVERSHOOT
                ),
            )

            # Update entity position
            self.entity.x = self.x
            self.entity.y = self.y
            self.entity.draw_y = self.y

    def update(self, dt):
        """Updates the soul's position and physics."""

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
                    self.screen_height - self.height + WINDOW_OVERSHOOT,
                ),
            )
            # Update entity position (we update Y here for visual hover in overlay)
            self.entity.draw_y = final_y

        else:
            self.entity.draw_y = self.y  # No hover

            # Pick target if needed
            # Pick target if needed (only if autonomous roaming is enabled)
            if self.roaming_enabled and (
                self.roaming_target is None or random.random() < 0.005
            ):
                padding = min(self.screen_width, self.screen_height) * 0.1
                tx = random.uniform(
                    padding, self.screen_width - self.width - padding
                )
                ty = random.uniform(
                    padding, self.screen_height - self.height - padding
                )
                self.roaming_target = (tx, ty)

            # Move towards target
            if self.roaming_target:
                reached = self._move_towards_target(
                    self.roaming_target[0], self.roaming_target[1], dt
                )
                if reached:
                    self.roaming_target = None
                    self.roaming_pause = random.uniform(
                        ROAM_PAUSE_MIN, ROAM_PAUSE_MAX
                    )

            # Apply separation force (Personal Space) to avoid stacking
            self._apply_separation(dt)

    def _apply_separation(self, dt):
        """Applies a repulsive force to separate overlapping souls."""
        separation_radius = self.width * 1.2  # Personal space bubble
        separation_force = 200.0  # Strength of push

        my_center_x = self.x + self.width / 2
        my_center_y = self.y + self.height / 2

        push_x = 0.0
        push_y = 0.0
        count = 0

        # Access registry from entity if available
        if hasattr(self.entity, "soul_registry") and self.entity.soul_registry:
            for other in self.entity.soul_registry:
                if other is self.entity:
                    continue

                # Check rough bounds first
                if abs(other.x - self.x) > separation_radius:
                    continue
                if abs(other.y - self.y) > separation_radius:
                    continue

                other_center_x = other.x + other.width / 2
                other_center_y = other.y + other.height / 2

                dx = my_center_x - other_center_x
                dy = my_center_y - other_center_y
                dist_sq = dx * dx + dy * dy

                min_dist_sq = separation_radius * separation_radius

                if 0 < dist_sq < min_dist_sq:
                    dist = math.sqrt(dist_sq)
                    # Calculate vector pointing away from neighbor
                    # Weight by how close they are (closer = stronger push)
                    force = (separation_radius - dist) / separation_radius
                    push_x += (dx / dist) * force
                    push_y += (dy / dist) * force
                    count += 1
                elif dist_sq == 0:
                    # Exact overlap, pick random direction
                    angle = random.random() * 2 * math.pi
                    push_x += math.cos(angle)
                    push_y += math.sin(angle)
                    count += 1

        if count > 0:
            # Apply offset
            # Normalize? No, we weighted by force.
            move_x = push_x * separation_force * dt
            move_y = push_y * separation_force * dt

            self.x += move_x
            self.y += move_y

            # Sync to entity
            self.entity.x = self.x
            self.entity.y = self.y

    def _move_towards_target(self, target_x, target_y, dt):
        """
        Moves the soul towards a target position with smooth deceleration.
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
        deceleration_distance = (
            50.0  # Distance over which to decelerate (pixels)
        )
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

        # Clamp to screen edges with sub-pixel precision
        self.x = max(
            float(-WINDOW_OVERSHOOT),
            min(
                self.x, self.screen_width - self.width + float(WINDOW_OVERSHOOT)
            ),
        )
        self.y = max(
            float(-WINDOW_OVERSHOOT),
            min(
                self.y,
                self.screen_height - self.height + float(WINDOW_OVERSHOOT),
            ),
        )

        # Only update entity position if it changed significantly
        if abs(self.x - self.last_x) > 0.5 or abs(self.y - self.last_y) > 0.5:
            self.entity.x = self.x
            self.entity.y = self.y
            self.entity.draw_y = self.y  # Sync draw Y
            self.last_x, self.last_y = self.x, self.y

        return False  # Still moving

    def _update_follow_mouse(self, dt):
        """Update the soul position to follow the mouse."""
        mouse_x, mouse_y = pyautogui.position()
        target_x = mouse_x - (self.width // 2)
        target_y = mouse_y - (self.height // 2)
        # Note: -20 offset removed, centering on soul center which is clearer
        self._move_towards_target(target_x, target_y, dt)
