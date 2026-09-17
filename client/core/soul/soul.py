"""This module defines the Soul class, which manages soul state, biology,
and physics-based movement. Moment-to-moment decisions come from the GOAP
brain (see goap_brain.py); the LLM agent is parked (see client/ai/README.md).
"""

from __future__ import annotations

import json
import math
import random
import time
import uuid
from typing import Any

from ...constants import SOUL_HEIGHT, SOUL_WIDTH
from ...system.command_queue import CommandQueue
from ...system.logger import log
from ...system.network.viewport_client import (
    dormant_statue_orb_color,
    statue_orb_color,
)
from ..biology import Gender, SoulBiology, SoulStats, Species
from ..interactions import Inventory
from .goap_brain import GoapBrain
from .physics import SoulPhysics


class Soul:
    """An autonomous entity within the Soulscape simulation.

    The Soul class is the core actor in the ecosystem. It possesses
    needs (satiety, hydration, etc...) and digital presence in the window
    environment. Autonomous decision-making is handled by the attached
    brain (see client/core/soul/goap_brain.py); the decision tick below
    is a no-op while no brain is attached.

    Attributes:
        soul_id: A unique string identifier for the soul instance.
        name: The display name of the soul.
        species: The Species object defining biological defaults.
        gender: The Gender object defining reproductive capabilities.
        stats: SoulStats containing HP, attack, defense, and vision.
        inventory: An Inventory instance for holding items.
        essence: The currency used for social and magical actions.
        window_physics: A SoulPhysics instance handling screen movement.
        secret: A private UUID key used for Hub authentication (V3).
    """

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        on_right_click: Any = None,
        on_move_end: Any = None,
        on_state_change: Any = None,
        on_async_state_change: Any = None,
        soul_registry: list[Soul] | None = None,
        task_scheduler: Any = None,
        **kwargs: Any,
    ) -> Soul:
        """Reconstructs a Soul instance from its serialized representation.

        Args:
            data: A dictionary containing the soul's serialized state.
            on_right_click: An optional callback invoked when the soul is
                clicked with the right mouse button.
            on_move_end: An optional callback invoked when a movement target
                is reached.
            on_state_change: An optional callback invoked when a major soul
                state occurs.
            on_async_state_change: Async variant for state changes.
            soul_registry: A list of all active souls in the simulation.
            **kwargs: Additional configuration parameters like screen_width.

        Returns:
            A new Soul instance with its state fully restored.
        """
        # Data ingestion assumes flat Hub protocol
        name = data.get("name")

        # Colors
        orb_color = tuple(data.get("orb_color", (0.56, 0.93, 0.56)))
        aura_color = tuple(data.get("aura_color", (1.0, 0.5, 0.0)))
        aura_visible = bool(data.get("aura_visible", True))

        # Position (always flat 'position' list)
        position = tuple(data.get("position", (100, 100)))

        # Stats (always reconstructed from flat data)
        stats = SoulStats.from_dict(data)

        soul = cls(
            orb_color_rgb=orb_color,
            aura_color_rgb=aura_color,
            name=name,
            on_right_click=on_right_click,
            on_move_end=on_move_end,
            on_state_change=on_state_change,
            on_async_state_change=on_async_state_change,
            initial_position=position,
            stats=stats,
            soul_registry=soul_registry,
            screen_width=kwargs.get("screen_width", 1920),
            screen_height=kwargs.get("screen_height", 1080),
            owner_id=data.get("owner_id"),
            local_instance_id=kwargs.get("local_instance_id"),
            soul_id=data.get("soul_id"),
            task_scheduler=task_scheduler,
            secret=data.get("secret"),
        )

        # Restore soul instance features using biology helper (handles flat/nested)
        soul.biology = SoulBiology.from_dict(data)

        # Restore aura visibility
        soul.aura_visible = aura_visible

        # Restore inventory
        inventory_data = data.get("inventory")
        if inventory_data:
            soul.inventory = Inventory.from_dict(inventory_data)

        soul.essence = float(data.get("essence", 100.00))
        soul.owner_id = data.get("owner_id", soul.owner_id)

        return soul

    DEBUG_VISION: bool = True  # Saved processed screenshots to debug_vision/

    def __init__(
        self,
        orb_color_rgb: tuple[float, float, float],
        aura_color_rgb: tuple[float, float, float],
        name: str | None = None,
        on_right_click: Any = None,
        on_move_end: Any = None,
        on_state_change: Any = None,
        on_async_state_change: Any = None,
        initial_position: tuple[int, int] = (0, 0),
        stats: SoulStats | None = None,
        # Simulation Parameters
        species: Species | None = None,
        birth_mother: Soul | None = None,
        birth_father: Soul | None = None,
        gender: Gender | None = None,
        soul_registry: list[Soul] | None = None,
        screen_width: int = 1920,
        screen_height: int = 1080,
        owner_id: str | None = None,
        local_instance_id: str | None = None,
        soul_id: str | None = None,
        task_scheduler: Any = None,
        secret: str | None = None,
    ) -> None:
        """Initializes a new Soul entity.

        Args:
            orb_color_rgb: A tuple of (red, green, blue) floats (0.0-1.0)
                representing the soul's central orb color.
            aura_color_rgb: A tuple of (red, green, blue) floats (0.0-1.0)
                representing the soul's surrounding aura color.
            name: The soul's full name. If None, a default name will be assigned.
            on_right_click: Callback triggered on right-click.
            on_move_end: Callback triggered when a movement target is reached.
            on_state_change: Callback triggered when a major state change occurs (e.g. tools).
            initial_position: The starting (x, y) screen coordinates.
            stats: Pre-defined SoulStats. If None, random stats are generated.
            species: The biological species. Defaults to Human if None.
            birth_mother: The soul's mother instance, if applicable.
            birth_father: The soul's father instance, if applicable.
            gender: The soul's gender. If None, a random gender from its species
                definition is selected.
            soul_registry: A reference to the global list of active souls.
            screen_width: The width of the desktop in pixels.
            screen_height: The height of the desktop in pixels.
        """

        # Initialize position
        self.x: float = float(initial_position[0])
        self.y: float = float(initial_position[1])
        self.draw_y: float = self.y  # Y position for drawing (includes hover)
        self.orb_color_rgb = orb_color_rgb
        self.aura_color_rgb = aura_color_rgb
        self.on_right_click: Any = on_right_click
        self.on_move_end: Any = on_move_end
        self.on_state_change: Any = on_state_change
        self.on_async_state_change: Any = on_async_state_change
        self.soul_registry: list[Soul] = soul_registry or []
        self.screen_width: int = screen_width
        self.screen_height: int = screen_height
        self.task_scheduler: Any = task_scheduler

        # Hub V3 Authentication
        self.secret: str = secret or str(uuid.uuid4())

        self.citizenship: list[str] = []

        # Initialize Inventory
        self.inventory: Inventory = Inventory(capacity=10)
        self.essence: float = 100.00  # Default starting currency

        # Tool Tracking
        self._active_actions: set[str] = set()

        # Command Queue for thread-safe state mutations
        self.command_queue: CommandQueue = CommandQueue()

        # --- Visual / Physics Initialization ---

        self.width = SOUL_WIDTH
        self.height = SOUL_HEIGHT

        self.time: float = 0.0
        self.bulge_position: list[float] = [0.0, 0.0, 0.0]
        self.aura_visible: bool = True
        self.camera_distance: float = 1.0
        # Issue #21: statue render state streamed from the Hub. When True,
        # the renderer desaturates the orb/aura (stone treatment) and the
        # plasma pulse freezes (visual_tick is skipped in viewport mode).
        self.statue: bool = False
        # Issue #22: dormant (unfunded) souls render as statues too, but
        # amber-tinted to distinguish them from collapsed statues.
        self.dormant_statue: bool = False

        # Updates
        self.last_update_time: float = time.time()
        self.update_interval: float = 1.0  # seconds for simulation tick
        self._simulation_time_accumulator: float = 0.0

        self.biology = SoulBiology(
            species=species,
            name=name,
            birth_mother=birth_mother,
            birth_father=birth_father,
            gender=gender,
            stats=stats,
            current_location=(self.x, self.y),
            soul_id=soul_id,
        )
        self.physics = SoulPhysics(
            self, on_move_end, self.screen_width, self.screen_height
        )

        # Ownership and decision-making hook (GOAP brain for local souls)
        self.owner_id = owner_id or local_instance_id
        self.local_instance_id = local_instance_id
        self.agent: Any = None

        if self.owner_id == self.local_instance_id or self.local_instance_id is None:
            log.info(f"Attaching GOAP brain for local soul: {self.biology.name}")
            self.agent = GoapBrain(soul=self)
        else:
            log.info(
                f"Skipping brain for remote soul: {self.biology.name} (Owner: {self.owner_id})"
            )

    # --- Simulation Methods ---

    def set_activity_level(self, level: str) -> None:
        """Sets the current physical activity intensity."""
        if level in ["resting", "active", "fighting"]:
            self.biology.activity_level = level
            print(f"{self.biology.name} is now {self.biology.activity_level}.")

    def random_event(self) -> None:
        """Triggers random events based on the soul's level of needs.

        Simulates psychological effects of physical neglect, such as
        hallucinations or fatigue when satiety or hydration is critically low.
        """
        if self.biology.satiety < 20 or self.biology.hydration < 20:
            event = random.choice(["hallucination", "fatigue"])
            print(f"Due to low levels, {self.biology.name} experiences {event}!")

    # --- Main Update ---

    def to_dict(self, include_secret: bool = False) -> dict[str, Any]:
        """Serializes the soul's current state to a portable dictionary format.

        Captures vital signs, position, and possessions for persistence.

        Returns:
            A dictionary containing the soul's serialized representation.

        Args:
            include_secret: Whether to include the authentication secret.
                            Defaults to `False` for security. Set this to `True` only
                            for persistence or secure registration with the Hub.
        """
        data = {
            "soul_id": self.biology.soul_id,
            "owner_id": self.owner_id,
            **self.biology.to_flat_dict(),
            "position": [self.x, self.y],
            "orb_color": self.orb_color_rgb,
            "aura_color": self.aura_color_rgb,
            "aura_visible": self.aura_visible,
            "essence": self.essence,
            "inventory": self.inventory.to_dict(),
        }
        if include_secret:
            data["secret"] = self.secret
        return data

    def create_snapshot(self) -> dict[str, Any]:
        """Creates a thread-safe snapshot of the soul's current state.

        This method creates a deep copy of all data required by background
        consumers (network sync, future decision-making brains),
        ensuring that no live Pyglet objects or mutable shared state reference
        leak into the background thread.

        Returns:
            A dictionary containing a completely decoupled snapshot of the soul.
        """
        # Reuse to_dict for the base data but exclude secret for security
        snapshot = self.to_dict(include_secret=False)

        # Add volatile/runtime-only data that to_dict might skip but Agent needs
        # (Currently to_dict is quite comprehensive, but we separate intent here)
        snapshot.update(
            {
                # SANITIZATION: Prevent prompt injection via name
                "name": "".join(
                    c for c in self.biology.name if c.isalnum() or c in " -_"
                )[:50],
                "x": self.x,
                "y": self.y,
                "species": self.biology.species.name,
                "gender": self.biology.gender.gender_name,
                "location": self.biology.current_location,
                "hp": self.biology.current_health,
                "max_hp": self.biology.stats.max_hp,
                "satiety": self.biology.satiety,
                "hydration": self.biology.hydration,
                "vision_stat": float(self.biology.stats.vision),
                "width": self.width,
                "height": self.height,
                "screen_width": self.screen_width,
                "screen_height": self.screen_height,
                "debug_vision": getattr(self, "DEBUG_VISION", True),
                # Sensations are handled explicitly in the update loop to ensure
                # they are consumed only once per decision cycle.
            }
        )
        return snapshot

    def update_from_dict(self, data: dict[str, Any]) -> None:
        """Updates the soul's state from a serialized dictionary.

        Used for syncing remote souls without recreating the instance.
        """
        # Updates position
        position = data.get("position")
        if position:
            if isinstance(position, (list, tuple)):
                new_x, new_y = float(position[0]), float(position[1])
            elif isinstance(position, str):
                # Handle potential stringified list from DB
                try:
                    pos_list = json.loads(position)
                    new_x, new_y = float(pos_list[0]), float(pos_list[1])
                except Exception as e:
                    log.warning(f"Failed to parse position '{position}': {e}")
                    new_x, new_y = self.x, self.y

            if self.physics:
                # Use LERP for smooth transition
                self.physics.target_x = new_x
                self.physics.target_y = new_y
                self.physics.is_interpolating = True
            else:
                # No physics, snap immediately
                self.x, self.y = new_x, new_y
                self.draw_y = new_y

        # Update stats
        if "stat_hp_base" in data:
            self.biology.stats = SoulStats.from_dict(data)
        elif "stats" in data and isinstance(data["stats"], dict):
            log.warning(
                "Received nested 'stats' dictionary, "
                "which is not fully supported by SoulStats.from_dict yet."
            )

        # Update inventory
        inventory_data = data.get("inventory")
        if inventory_data:
            self.inventory = Inventory.from_dict(inventory_data)

        # Update Biology / Vitals
        if "satiety" in data:
            self.biology.satiety = float(data["satiety"])
        if "hydration" in data:
            self.biology.hydration = float(data["hydration"])
        if "hp" in data:
            self.biology.current_health = int(data["hp"])
        if "xp" in data:
            self.biology.experience_points = float(data["xp"])
        if "level" in data:
            self.biology.level = int(data["level"])

        # Update essence
        self.essence = float(data.get("essence", self.essence))

        # Biology status? (satiety etc) is in to_flat_dict()
        # Biology updates not critical for remote viewing unless displaying stats
        # For now, position is the main thing.

    def visual_tick(self, dt: float) -> None:
        """Advances animation-only state for one frame.

        Used by the viewport render path, where positions come from the Hub
        stream instead of the local simulation.
        """
        self.time += dt
        angle: float = self.time * 0.5
        self.bulge_position: list[float] = [
            math.cos(angle) * 0.5,
            math.sin(angle * 0.7) * 0.35,
            math.sin(angle) * 0.5,
        ]

    def display_orb_color(self) -> tuple[float, float, float]:
        """Orb color for the shader's base_color_uniform.

        Collapsed souls render as statues: desaturated stone gray.
        Dormant souls render as statues too: amber-tinted stone (issue
        #22), so the two freeze states are distinguishable.
        """
        if self.dormant_statue:
            return dormant_statue_orb_color(self.orb_color_rgb)
        if self.statue:
            return statue_orb_color(self.orb_color_rgb)
        return self.orb_color_rgb

    def display_aura_color(self) -> tuple[float, float, float]:
        """Aura color for the shader's base_color_uniform (statue: stone)."""
        if self.dormant_statue:
            return dormant_statue_orb_color(self.aura_color_rgb)
        if self.statue:
            return statue_orb_color(self.aura_color_rgb)
        return self.aura_color_rgb

    def update(self, dt: float) -> None:
        """Drives the soul's simulation and AI logic for a single frame.

        Handles physics interpolation, visual animations, biological decay,
        and triggers the periodic AI decision-making loop if owned locally.

        Args:
            dt: The time delta in fractional seconds.
        """
        self.visual_tick(dt)

        # Physics updates happen for all souls (synced via Hub)
        self.physics.update(dt)

        # Simulation Update (Tick-based)
        if self.biology.is_alive():
            self._simulation_time_accumulator += dt
            if self._simulation_time_accumulator >= self.update_interval:
                self._simulation_time_accumulator -= self.update_interval
                self.biology.decrease_satiety()
                self.biology.decrease_hydration()
                self.biology.check_status()

            # 4. Agent Decision (Thread Safe)
            if self.agent:
                # OPTIMIZATION: Check if agent needs to run BEFORE creating heavy snapshot
                if (
                    not self.agent.is_busy
                    and self.time - self.agent.last_decision_time
                    > self.agent.decision_interval
                ):
                    snapshot = self.create_snapshot()
                    sensations = list(self.biology.sensations)
                    self.biology.sensations.clear()
                    self.agent.trigger_decision(
                        self.time, snapshot=snapshot, sensations=sensations
                    )

            # Periodic Heartbeat for diagnostics
            if (
                int(self.time) % 60 == 0
                and self.time - getattr(self, "_last_heartbeat", 0) > 1.0
            ):
                self._last_heartbeat = self.time
                log.debug(
                    f"Heartbeat for {self.biology.name}: HP={self.biology.current_health}, Satiety={self.biology.satiety}, Hydration={self.biology.hydration}"
                )
        else:
            # If dead, handle a small visual fade or stop
            self.aura_visible = False
            if (
                int(self.time) % 60 == 0
                and self.time - getattr(self, "_last_death_log", 0) > 60.0
            ):
                self._last_death_log = self.time
                log.info(
                    f"Soul {self.biology.name} is currently PERISHED and awaiting revival."
                )

        # 5. Process thread-safe commands
        self.process_commands()

    def process_commands(self) -> None:
        """Processes and executes all pending commands in the queue."""
        while not self.command_queue.empty():
            command = self.command_queue.get()
            if command:
                try:
                    command.execute(self)
                except Exception as e:
                    log.error(f"Error executing command {type(command).__name__}: {e}")

    def schedule_task(self, coro: Any) -> None:
        """Schedules an async task on the background loop via the scheduler callback."""
        if self.task_scheduler:
            self.task_scheduler(coro)
        else:
            log.warning(
                f"Soul {self.biology.name} attempted to schedule task commands but has no scheduler."
            )

    def on_mouse_press(
        self, x: int, y: int, button: int, modifiers: int, screen_height: int
    ) -> bool | None:
        """Event handler for mouse press.

        Args:
            x: X coordinate of the mouse.
            y: Y coordinate of the mouse.
            button: Mouse button pressed.
            modifiers: Key modifiers.
            screen_height: Height of the screen for coordinate conversion.

        Returns:
            True if handled, False otherwise.
        """
        y_top_left = screen_height - y

        if button == 4 and self.on_right_click:  # 4 is RIGHT mouse button
            if (
                self.x <= x <= self.x + self.width
                and self.y <= y_top_left <= self.y + self.height
            ):
                self.on_right_click(self, x, y_top_left)
                return True

        # Left click (selection/drag start) - Log Sensation
        if button == 1:  # 1 is LEFT mouse button
            if (
                self.x <= x <= self.x + self.width
                and self.y <= y_top_left <= self.y + self.height
            ):
                self.biology.sensations.append(
                    "You felt a sudden, powerful touch from above."
                )

        if self.physics:
            self.physics.on_mouse_press(x, y_top_left, button, modifiers)
        return False

    def on_mouse_drag(
        self,
        x: int,
        y: int,
        dx: int,
        dy: int,
        buttons: int,
        modifiers: int,
        screen_height: int,
    ) -> None:
        """Event handler for mouse drag.

        Args:
            x: X coordinate.
            y: Y coordinate.
            dx: Delta X.
            dy: Delta Y.
            buttons: Mouse buttons held.
            modifiers: Key modifiers.
            screen_height: Height of the screen.
        """
        y_top_left = screen_height - y
        if self.physics:
            self.physics.on_mouse_drag(x, y_top_left, dx, -dy, buttons, modifiers)

    def on_mouse_release(
        self, x: int, y: int, button: int, modifiers: int, screen_height: int
    ) -> None:
        """Event handler for mouse release.

        Args:
            x: X coordinate.
            y: Y coordinate.
            button: Mouse button.
            modifiers: Key modifiers.
            screen_height: Height of the screen.
        """
        y_top_left = screen_height - y

        # If we were being dragged (physics would know, but here we can infer or just log the release)
        # We can just check bounds or if we were the target.
        # For simplicity, if this event fires on us (handled by input router usually),
        # but InputRouter calls soul.on_mouse_release...
        # Let's just log a generic "release" sensation if we were held.
        # Actually input router calls this on the specific soul.
        if button == 1:  # 1 is LEFT mouse button
            # We might check if we moved significantly, but a simple log is fine.
            self.biology.sensations.append("The powerful force released you.")

        if self.physics:
            self.physics.on_mouse_release(x, y_top_left, button, modifiers)

    def stop(self) -> None:
        """Gracefully halts the soul simulation and releases resources."""
        self.cleanup()

    def cleanup(self) -> None:
        """Gracefully releases all system resources held by this soul."""
        if self.agent:
            self.agent.stop()
        log.debug(f"Cleaned up resources for soul: {self.biology.name}")
