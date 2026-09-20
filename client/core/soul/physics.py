"""Physics and interaction logic for Souls."""

from __future__ import annotations

import math
import random
import time
from typing import TYPE_CHECKING, Callable

from shared.spatial import SpatialHashGrid

from ...constants import (
    HOVER_AMPLITUDE,
    HOVER_FREQUENCY,
    ROAM_PAUSE_MAX,
    ROAM_PAUSE_MIN,
    SOUL_HEIGHT,
    SOUL_WIDTH,
    WINDOW_OVERSHOOT,
)
from ...system.logger import log

if TYPE_CHECKING:
    from .soul import Soul


SEPARATION_CELL_SIZE = 32.0

_separation_frame = 0
_separation_grids: dict[int, tuple[int, SpatialHashGrid]] = {}


def begin_separation_frame() -> None:
    """Advances the separation frame so the next lookup rebuilds the grid.

    Call once per simulation frame before updating souls. The neighbor
    grid is then built lazily on the first separation query of the frame
    and reused by every soul, keeping the whole frame O(n).
    """
    global _separation_frame
    _separation_frame += 1


def _separation_key(other: Soul) -> str:
    biology = getattr(other, "biology", None)
    soul_id = getattr(biology, "soul_id", None)
    return soul_id if soul_id else str(id(other))


def _separation_grid(registry: list[Soul]) -> SpatialHashGrid:
    key = id(registry)
    cached = _separation_grids.get(key)
    if cached is not None and cached[0] == _separation_frame:
        return cached[1]
    grid = SpatialHashGrid(cell_size=SEPARATION_CELL_SIZE)
    for other in registry:
        grid.insert(
            _separation_key(other),
            other.x + other.width / 2,
            other.y + other.height / 2,
            other,
        )
    _separation_grids[key] = (_separation_frame, grid)
    return grid


# --- Window Physics and Interaction ---
class SoulPhysics:
    """Manages the movement and interaction of a Soul soul.

    Handles mouse dragging, following, roaming, and collision avoidance.
    """

    def __init__(
        self,
        soul: Soul,
        on_move_end: Callable[[Soul, float, float], None] | None = None,
        screen_width: int = 1920,
        screen_height: int = 1080,
    ):
        """Initializes physics for a Soul.

        Args:
            soul: The Soul instance to control.
            on_move_end: Optional callback triggered when movement stops.
            screen_width: Width of the screen.
            screen_height: Height of the screen.
        """
        log.debug(f"Initializing physics for Soul: {soul.biology.name}")
        self.soul = soul
        self.window = None
        self.on_move_end = on_move_end

        # Initialize from soul position
        self.x: float = float(self.soul.x)
        self.y: float = float(self.soul.y)

        self.vx: float = 0.0
        self.vy: float = 0.0
        self.is_dragging: bool = False
        self.drag_offset_x: float = 0
        self.drag_offset_y: float = 0
        self.target_location: tuple[float, float] | None = None

        # Remote Movement Interpolation
        self.is_interpolating: bool = False
        self.target_x: float = self.x
        self.target_y: float = self.y

        self.roaming_pause: float = 0.0
        self.roaming_enabled: bool = False  # Souls only move when instructed
        self.follow_mouse: bool = False
        self.last_click_time: float = 0
        self.mouse_x: float = 0
        self.mouse_y: float = 0
        self.hover_phase: float = 0.0  # For hover animation

        # Calculate speeds based on stats if available
        base_speed_val = 100.0
        if hasattr(soul, "biology") and soul.biology.stats:
            speed_stat = soul.biology.stats.speed
        else:
            speed_stat = base_speed_val

        # Speed modifiers (increased for better responsiveness)
        speed_multiplier = speed_stat / 100.0

        self.follow_speed: float = 1.5 * speed_multiplier
        self.max_speed: float = 5.0 * speed_multiplier

        log.debug(
            f"Physics initialized with max_speed={self.max_speed:.2f} "
            f"(Stat: {speed_stat})"
        )
        self.follow_delay: float = 0.0  # Timer for follow delay
        self.follow_delay_duration: float = 0.5
        self.name: str = soul.biology.name
        self.is_hovered: bool = False

        # Track previous position for smooth movement
        self.last_x: int = int(self.x)
        self.last_y: int = int(self.y)

        # Initialize screen dimensions from arguments
        self.screen_width: int = screen_width
        self.screen_height: int = screen_height

        self.width: int = SOUL_WIDTH
        self.height: int = SOUL_HEIGHT
        log.debug(f"Soul: {self.name} initialized with position ({self.x}, {self.y})")

    def on_mouse_press(self, x: int, y: int, button: int, modifiers: int) -> None:
        """Handles mouse press events to initiate dragging.

        Args:
            x: Absolute screen x-coordinate.
            y: Absolute screen y-coordinate.
            button: Mouse button pressed.
            modifiers: Function keys modifiers.
        """
        if button == 1:  # 1 is LEFT mouse button
            # Check if click is inside this soul's bounds
            # x, y are absolute screen coordinates
            if (
                self.x <= x <= self.x + self.width
                and self.y <= y <= self.y + self.height
            ):
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
                        self.target_location = None
                        self.roaming_pause = 0.0
                    return  # Skip dragging on double click

                self.last_click_time = current_time

                # Start dragging
                self.is_dragging = True
                self.drag_offset_x = x - self.x
                self.drag_offset_y = y - self.y
                self.vx = 0.0  # Stop independent movement
                self.vy = 0.0

    def on_mouse_release(self, x: int, y: int, button: int, modifiers: int) -> None:
        """Handles mouse release events to stop dragging."""
        if button == 1:  # 1 is LEFT mouse button
            was_dragging = self.is_dragging
            self.is_dragging = False

            if was_dragging and self.on_move_end:
                self.on_move_end(self.soul, self.x, self.y)

    def on_mouse_enter(self, x: int, y: int) -> None:
        """Handles mouse enter event to show the name label.

        Args:
            x: Mouse x-coordinate.
            y: Mouse y-coordinate.
        """
        self.is_hovered = True

    def on_mouse_leave(self, x: int, y: int) -> None:
        """Handles mouse leave event to hide the name label.

        Args:
            x: Mouse x-coordinate.
            y: Mouse y-coordinate.
        """
        self.is_hovered = False

    def on_mouse_motion(self, x: int, y: int, dx: int, dy: int) -> None:
        """Tracks mouse motion to update follow target.

        Args:
            x: Mouse x-coordinate.
            y: Mouse y-coordinate.
            dx: Change in x.
            dy: Change in y.
        """
        self.mouse_x = float(x)
        self.mouse_y = float(y)

    def on_mouse_drag(
        self, x: int, y: int, dx: int, dy: int, buttons: int, modifiers: int
    ) -> None:
        """Handles mouse drag events. Directly updates soul position.

        Args:
            x: Mouse x-coordinate.
            y: Mouse y-coordinate.
            dx: Change in x.
            dy: Change in y.
            buttons: Buttons pressed.
            modifiers: Modifier keys.
        """
        if self.is_dragging and (buttons & 1):  # 1 is LEFT mouse button bitmask
            # Calculate new top-left position based on drag offset
            new_soul_x = float(x - self.drag_offset_x)
            new_soul_y = float(y - self.drag_offset_y)

            self.x = new_soul_x
            self.y = new_soul_y

            # Update mouse position for following if enabled
            self.mouse_x = float(x)
            self.mouse_y = float(y)

            # Clamp to screen edges immediately during drag
            self.x = max(
                -WINDOW_OVERSHOOT,
                min(self.x, self.screen_width - self.width + WINDOW_OVERSHOOT),
            )
            self.y = max(
                -WINDOW_OVERSHOOT,
                min(self.y, self.screen_height - self.height + WINDOW_OVERSHOOT),
            )

            # Update soul position
            self.soul.x = self.x
            self.soul.y = self.y
            self.soul.draw_y = self.y

    def update(self, dt: float) -> None:
        """Updates the soul's position and physics.

        Args:
            dt: Delta time since last frame.
        """

        if self.is_dragging:
            self.vx = 0.0
            self.vy = 0.0
            return

        # Remote Interpolation (Smooth Movement)
        if self.is_interpolating:
            # Simple LERP towards target
            lerp_speed = 20.0 * dt  # Adjust for smoothness vs responsiveness
            dx = self.target_x - self.x
            dy = self.target_y - self.y

            # If close enough, snap to target
            if abs(dx) < 1.0 and abs(dy) < 1.0:
                self.x = self.target_x
                self.y = self.target_y
                self.is_interpolating = False
            else:
                self.x += dx * lerp_speed
                self.y += dy * lerp_speed

            # Update Soul syncing
            self.soul.x = self.x
            self.soul.y = self.y
            self.soul.draw_y = self.y
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
            # Update soul position (we update Y here for visual hover in overlay)
            self.soul.draw_y = final_y

        else:
            self.soul.draw_y = self.y  # No hover

            # Pick target if needed
            # Pick target if needed (only if autonomous roaming is enabled)
            if self.roaming_enabled and (
                self.target_location is None or random.random() < 0.005
            ):
                padding = min(self.screen_width, self.screen_height) * 0.1
                tx = random.uniform(padding, self.screen_width - self.width - padding)
                ty = random.uniform(padding, self.screen_height - self.height - padding)
                self.target_location = (tx, ty)

            # Move towards target
            if self.target_location:
                reached = self._move_towards_target(
                    self.target_location[0], self.target_location[1], dt
                )
                if reached:
                    self.target_location = None
                    self.roaming_pause = random.uniform(ROAM_PAUSE_MIN, ROAM_PAUSE_MAX)
                    if self.on_move_end:
                        self.on_move_end(self.soul, self.x, self.y)

            # Apply separation force (Personal Space) to avoid stacking
            self._apply_separation(dt)

    def _apply_separation(self, dt: float) -> None:
        """Applies a repulsive force to separate overlapping souls.

        Args:
            dt: Delta time.
        """
        separation_radius = self.width * 0.3  # Personal space bubble
        separation_force = 100.0  # Strength of push

        my_center_x = self.x + self.width / 2
        my_center_y = self.y + self.height / 2

        push_x = 0.0
        push_y = 0.0
        count = 0

        # Access registry from soul if available
        if hasattr(self.soul, "soul_registry") and self.soul.soul_registry:
            grid = _separation_grid(self.soul.soul_registry)
            neighbors = grid.query_radius(
                my_center_x, my_center_y, separation_radius
            )
            for _, other_center_x, other_center_y, other in neighbors:
                if other is self.soul:
                    continue

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

            # Sync to soul
            self.soul.x = self.x
            self.soul.y = self.y

    def _move_towards_target(self, target_x: float, target_y: float, dt: float) -> bool:
        """Moves the soul towards a target position with smooth deceleration.

        Args:
            target_x: Target x-coordinate.
            target_y: Target y-coordinate.
            dt: Delta time.

        Returns:
            True if the target has been reached (within dead zone), False otherwise.
        """
        # Calculate direction vector and distance
        dx = target_x - self.x
        dy = target_y - self.y
        distance_sq = dx * dx + dy * dy

        # Define a dead zone where we stop moving (squared for efficiency)
        # 10.0 pixels is a good threshold for "arrived" in screen space
        dead_zone_sq = 100.0  # 10.0 pixels squared

        if distance_sq <= dead_zone_sq:
            # Snap exactly to target for perfection
            self.x = target_x
            self.y = target_y
            self.soul.x = self.x
            self.soul.y = self.y
            return True  # Reached target

        # Calculate actual distance
        distance = math.sqrt(distance_sq)

        # Normalize direction
        inv_distance = 1.0 / distance
        dx *= inv_distance
        dy *= inv_distance

        # Calculate speed with smooth deceleration using an ease-out curve
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

        # Clamp to screen edges with sub-pixel precision
        self.x = max(
            float(-WINDOW_OVERSHOOT),
            min(self.x, self.screen_width - self.width + float(WINDOW_OVERSHOOT)),
        )
        self.y = max(
            float(-WINDOW_OVERSHOOT),
            min(
                self.y,
                self.screen_height - self.height + float(WINDOW_OVERSHOOT),
            ),
        )

        # Only update soul position if it changed significantly
        if abs(self.x - self.last_x) > 0.5 or abs(self.y - self.last_y) > 0.5:
            self.soul.x = self.x
            self.soul.y = self.y
            self.soul.draw_y = self.y  # Sync draw Y
            self.last_x, self.last_y = self.x, self.y

        return False  # Still moving

    def _update_follow_mouse(self, dt: float) -> None:
        """Update the soul position to follow the mouse.

        Args:
            dt: Delta time.
        """
        import pyautogui

        mouse_x, mouse_y = pyautogui.position()
        target_x = float(mouse_x) - (self.width // 2)
        target_y = float(mouse_y) - (self.height // 2)
        # Note: -20 offset removed, centering on soul center which is clearer
        self._move_towards_target(target_x, target_y, dt)
