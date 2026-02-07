"""Logical entity representing a soul in the Soulscape universe.

This module defines the Soul class, which manages soul state, needs,
simulation logic, and AI-driven decision making using the ADK.
"""

# soul.py
from __future__ import annotations

import asyncio
import math
import os
import random
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pyglet
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pyglet.window import key, mouse

# Import constants
from soulscape.constants import SOUL_HEIGHT, SOUL_WIDTH
from soulscape.core.gender import Gender
from soulscape.core.items import Drink, Food, Inventory
from soulscape.core.soul_physics import SoulPhysics
from soulscape.core.species import Species
from soulscape.core.stats import SoulStats
from soulscape.system.logger import log

load_dotenv()

# Global list of locations (moved from Isekai.py)
POSSIBLE_LOCATIONS = [
    "Capital City",
    "Small Village",
    "Forest Cabin",
    "Mountain Fortress",
]

if TYPE_CHECKING:
    from soulscape.core.soul import Soul


# --- Main Application Class ---
class Soul:
    """Logical entity representing a soul.

    Manages its own state, biology, physics, and AI-driven decision making.
    Optimized for overlay rendering without an internal window.

    Attributes:
        soul_id: Unique identifier for this soul.
        name: Full name of the soul.
        satiety: Current satiety level (0-100).
        hydration: Current hydration level (0-100).
        current_health: Current health points.
        stats: SoulStats object containing base attributes.
        species: The species of the soul.
        gender: The gender of the soul.
        current_location: The soul's current location in the world.
    """

    _current_soul_id: int = 0

    def __init__(
        self,
        window_instance: Any,
        orb_color_rgb: tuple[int, int, int],
        aura_color_rgb: tuple[int, int, int],
        name: str | None = None,
        on_right_click: Any = None,
        on_move_end: Any = None,
        initial_position: tuple[int, int] = (0, 0),
        stats: SoulStats | None = None,
        # Simulation Parameters
        species: Species | None = None,
        birth_mother: Soul | None = None,
        birth_father: Soul | None = None,
        gender: Gender | None = None,
        current_location: str | None = None,
    ):
        """Initializes a Soul instance.

        Args:
            window_instance: The pyglet window instance for rendering.
            orb_color_rgb: RGB tuple for the soul's main color.
            aura_color_rgb: RGB tuple for the soul's aura color.
            name: Full name of the soul. Defaults to None.
            on_right_click: Callback for right-click events. Defaults to None.
            on_move_end: Callback triggered when movement stops. Defaults to None.
            initial_position: Starting (x, y) coordinates. Defaults to (0, 0).
            stats: Initial SoulStats. If None, random stats are generated.
            species: The species of the soul. If None, defaults to Human.
            birth_mother: Reference to the mother Soul. Defaults to None.
            birth_father: Reference to the father Soul. Defaults to None.
            gender: The gender of the soul. If None, random choice from species.
            current_location: Initial location string. Defaults to random.
        """
        Soul._current_soul_id += 1
        self.soul_id: int = Soul._current_soul_id

        self.window = window_instance

        # Initialize position
        self.x, self.y = initial_position
        self.draw_y = self.y  # Y position for drawing (includes hover)

        self.orb_color_rgb = orb_color_rgb
        self.aura_color_rgb = aura_color_rgb

        self.on_right_click = on_right_click
        self.on_move_end = on_move_end

        # --- Simulation Logic Initialization ---

        # Species & Gender
        # We need a default species if none provided, to avoid crashes in simulation
        # In a real app, maybe we'd require it, but for compatibility:
        if species is None:
            # Basic default if not provided (e.g. legacy/testing)
            # Ideally simulation should provide this.
            # We create a fallback locally if needed or assume user handles it.
            # For now, let's allow None but simulation methods might need checking.
            self.species = Species(
                "Human", [Gender("Male", False), Gender("Female", True)]
            )
        else:
            self.species = species

        if gender is None:
            self.gender: Gender = random.choice(self.species.genders)
        else:
            self.gender = gender

        # Name Handling
        self.first_name: str | None = None
        self.family_name: str | None = "Doe"

        if name:
            parts = name.split(" ", 1)
            self.first_name = parts[0]
            if len(parts) > 1:
                self.family_name = parts[1]

        self.name = self.get_full_name()  # Update interaction name

        # Lineage
        self.birth_mother: Soul | None = birth_mother
        self.birth_father: Soul | None = birth_father

        # Location
        self.current_location: str = (
            current_location if current_location else random.choice(POSSIBLE_LOCATIONS)
        )
        self._set_hometown()
        self._set_birth_datetime()

        # Stats & Biology
        if stats:
            self.stats = stats
        else:
            self.stats = SoulStats.create_random()

        self.experience_points: float = 0.0
        self.level: int = 1

        # Derived Stats
        self.current_health: int = self.stats.max_hp

        self.citizenship: list[str] = []

        # Known Names (Simple lists for now, logic from Isekai.py used constant lists)
        self.known_male_first_names = [
            "Jackson",
            "John",
            "Jack",
            "Malcom",
            "Fatin",
        ]
        self.known_female_first_names = [
            "Jane",
            "Lily",
            "Mallory",
            "Fatima",
            "Fatinah",
        ]

        # Needs
        self.satiety = 100
        self.hydration = 100
        self.satiety_drain_rate = 1
        self.hydration_drain_rate = 1
        self.activity_level = "resting"  # Can be 'resting', 'active', 'fighting'.

        # --- Visual / Physics Initialization ---

        # Only init physics if window is present (headless support).
        if self.window:
            self.window_physics = SoulPhysics(self, on_move_end)
            self.width = SOUL_WIDTH
            self.height = SOUL_HEIGHT
        else:
            self.window_physics = None
            self.width = 0
            self.height = 0

        self.time = 0.0
        self.bulge_position = [0.0, 0.0, 0.0]
        self.aura_visible = True
        self.camera_distance = 1.0

        # Updates
        self.last_update_time = time.time()
        self.update_interval = 1.0  # seconds for simulation tick
        self._simulation_time_accumulator = 0.0

        # Schedule Pyglet update if not headless (or caller handles loop)
        # We'll assume if window_instance is passed, we hook into pyglet clock
        pyglet.clock.schedule_interval(self.update, 1 / 60.0)

        # --- ADK Agent Initialization ---
        # Explicitly load dotenv
        env_path = os.path.join(os.getcwd(), ".env")
        load_dotenv(dotenv_path=env_path, override=True)

        api_key = os.getenv("GOOGLE_API_KEY")
        if api_key:
            log.info(
                "GOOGLE_API_KEY found: %s...%s",
                api_key[:4],
                api_key[-4:] if len(api_key) > 8 else "",
            )
            try:
                self.agent = LlmAgent(
                    model="gemini-2.0-flash",  # Using a fast model for game loops
                    name=f"soul_{self.soul_id}_agent",
                    description=f"AI brain for Soul {self.name}",
                    instruction=f"""You are {self.name}, a {self.gender.gender_name} {self.species.name}.
Current Location: {self.current_location}.
Stats: HP {self.current_health}/{self.stats.max_hp}, Satiety {self.satiety}/100, Hydration {self.hydration}/100.
Goal: Survive, explore, and thrive. If satiety or hydration is low, prioritize finding food/water.
""",
                    tools=[
                        self.eat,
                        self.drink,
                        self.find_food,
                        self.find_water,
                    ],
                )
                self.session_service = InMemorySessionService()
                # We initialize the session once
                try:
                    self.session = asyncio.run(
                        self.session_service.create_session(
                            app_name="soulscape",
                            user_id=f"user_{self.soul_id}",
                            session_id=f"session_{self.soul_id}",
                        )
                    )
                except RuntimeError:
                    # Handle running in existing loop if necessary
                    self.session = None  # Requires async handling if in existing loop

                self.runner = Runner(
                    agent=self.agent,
                    app_name="soulscape",
                    session_service=self.session_service,
                )
                log.info(f"Agent initialized for {self.name}")
            except Exception as e:
                log.error(f"Failed to initialize agent for {self.name}: {e}")
                self.agent = None
        else:
            log.warning("GOOGLE_API_KEY not found. Agent disabled.")
            self.agent = None

        self.last_decision_time = 0.0  # Reset to 0.0 to match simulation time
        self.decision_interval = 30.0  # Check for agent decision every 10 sec

        # Inventory
        self.inventory = Inventory(capacity=10)

    # --- Simulation Methods ---

    def get_full_name(self) -> str:
        """Constructs the full name from first name and family name.

        Returns:
            The full name as a string.
        """
        if self.first_name and self.family_name:
            return f"{self.first_name} {self.family_name}"
        elif self.first_name:
            return self.first_name
        return f"Soul #{self.soul_id}"

    def _set_birth_datetime(self) -> None:
        """Sets the birth datetime to the current system time."""
        self.birth_datetime = datetime.now()

    def _set_hometown(self) -> None:
        """Sets the hometown to the current location."""
        self.hometown = self.current_location

    def get_id(self) -> int:
        """Returns the soul's unique ID.

        Returns:
            The soul_id as an integer.
        """
        return self.soul_id

    def get_species(self) -> Species:
        """Returns the soul's species.

        Returns:
            A Species instance.
        """
        return self.species

    def get_gender(self) -> str:
        """Returns the name of the soul's gender.

        Returns:
            A string representing the gender name.
        """
        return self.gender.gender_name if self.gender else "Unknown"

    def get_current_health(self) -> int:
        """Returns the current health points.

        Returns:
            Current health as an integer.
        """
        return self.current_health

    def get_max_health(self) -> int:
        """Returns the maximum health points from stats.

        Returns:
            Maximum health as an integer.
        """
        return self.stats.max_hp

    def get_birth_datetime(self) -> datetime:
        """Returns the birth datetime recorded at creation.

        Returns:
            A datetime object.
        """
        return self.birth_datetime

    def get_hometown(self) -> str:
        """Returns the recorded hometown.

        Returns:
            The hometown location as a string.
        """
        return self.hometown

    def stop(self) -> None:
        """Stops the soul simulation and cleans up resources."""
        self.cleanup()

    def get_age(self) -> int:
        """Calculates the age of the soul in seconds since birth.

        Returns:
            Age in seconds as an integer.
        """
        if hasattr(self, "birth_datetime"):
            current_datetime = datetime.now()
            time_since_birth = current_datetime - self.birth_datetime
            return (time_since_birth).seconds
        return 0

    def is_alive(self) -> bool:
        """Checks if the soul is currently alive (health > 0).

        Returns:
            True if alive, False otherwise.
        """
        return self.current_health > 0

    def is_dead(self) -> bool:
        """Checks if the soul is currently dead (health <= 0).

        Returns:
            True if dead, False otherwise.
        """
        return self.current_health <= 0

    def decrease_satiety(self) -> None:
        """Decreases satiety based on activity level."""
        rate = self.satiety_drain_rate * self.get_activity_multiplier()
        self.satiety -= rate
        if self.satiety < 0:
            self.satiety = 0

    def decrease_hydration(self) -> None:
        """Decreases hydration points."""
        rate = self.hydration_drain_rate  # No environment factor
        self.hydration -= rate
        if self.hydration < 0:
            self.hydration = 0

    def get_activity_multiplier(self) -> float:
        """Returns a multiplier for satiety decrease based on activity level.

        Returns:
            Float multiplier (1.0, 1.5, or 2.0).
        """
        if self.activity_level == "active":
            return 1.5
        elif self.activity_level == "fighting":
            return 2.0
        return 1.0

    def check_status(self) -> None:
        """Checks satiety and hydration levels and applies penalties if needed."""
        if self.is_dead():
            return

        if self.satiety < 20:
            # Ideally use log or event system, simple print implies headless
            print(f"{self.name} is starving!")
            self.apply_health_penalty()
        elif self.satiety < 50:
            print(f"{self.name} is hungry.")

        if self.hydration < 20:
            print(f"{self.name} is dehydrated!")
            self.apply_health_penalty()
        elif self.hydration < 50:
            print(f"{self.name} is thirsty.")

    def apply_health_penalty(self) -> None:
        """Applies a random health penalty for starvation or dehydration."""
        if self.current_health > 0:
            health_penalty = random.randint(1, 5)
            self.current_health -= health_penalty
            print(f"{self.name} suffers a health penalty of {health_penalty}!")
        else:
            print(f"{self.name} is already dead.")

    def eat(self) -> dict[str, Any]:
        """Consumes a food item from the inventory.

        Retrieves the first available Food item from the backpack and consumes it,
        increasing satiety points.

        Returns:
            A dictionary containing:
                - status (str): 'success' if food was eaten, 'fail' otherwise.
                - message (str): Descriptive result of the action.
                - data (dict): Contains 'satiety_value' and 'current_satiety'.
        """
        food_items = self.inventory.get_consumables(Food)
        if not food_items:
            log.warning(f"{self.name} tried to eat but has no food!")
            return {
                "status": "fail",
                "message": "No food in inventory!",
                "data": {"current_satiety": self.satiety},
            }

        # Consume the first food item.
        item = food_items[0]
        value = item.value
        result_msg = item.consume(self)
        self.inventory.remove_item(item)
        log.info(result_msg)
        return {
            "status": "success",
            "message": result_msg,
            "data": {"satiety_value": value, "current_satiety": self.satiety},
        }

    def drink(self) -> dict[str, Any]:
        """Consumes a drink item from the inventory.

        Retrieves the first available Drink item from the backpack and consumes it,
        increasing hydration points.

        Returns:
            A dictionary containing:
                - status (str): 'success' if water was drunk, 'fail' otherwise.
                - message (str): Descriptive result of the action.
                - data (dict): Contains 'hydration_value' and 'current_hydration'.
        """
        drink_items = self.inventory.get_consumables(Drink)
        if not drink_items:
            log.warning("%s tried to drink but has no water!", self.name)
            return {
                "status": "fail",
                "message": "No water in inventory!",
                "data": {"current_hydration": self.hydration},
            }

        # Consume the first drink item.
        item = drink_items[0]
        value = item.value
        result_msg = item.consume(self)
        self.inventory.remove_item(item)
        log.info(result_msg)
        return {
            "status": "success",
            "message": result_msg,
            "data": {"hydration_value": value, "current_hydration": self.hydration},
        }

    def set_activity_level(self, level: str) -> None:
        """Sets the current activity level.

        Args:
            level: A string, one of 'resting', 'active', or 'fighting'.
        """
        if level in ["resting", "active", "fighting"]:
            self.activity_level = level
            print(f"{self.name} is now {self.activity_level}.")

    def find_food(self) -> dict[str, Any]:
        """Searches for food in the environment with a chance of success.

        If successful, a Food item is created and added to the soul's inventory.
        Success rate is fixed at 70%.

        Returns:
            A dictionary containing:
                - status (str): 'success' if food was found and added, 'fail' if
                  nothing found or inventory full.
                - message (str): Descriptive result of the search.
                - data (dict): Contains 'satiety_value' and 'current_satiety'.
        """
        success_rate = 0.7  # 70% chance to find food.
        if random.random() > success_rate:
            log.info(f"{self.name} searched for food but found nothing.")
            return {
                "status": "fail",
                "message": "You searched for food but found nothing.",
                "data": {"satiety_value": 0, "current_satiety": self.satiety},
            }

        food_value = random.randint(10, 30)
        new_food = Food("Wild Berries", "Found in the wild.", food_value)

        if self.inventory.add_item(new_food):
            log.info(f"{self.name} found {new_food.name} ({food_value} food value)!")
            return {
                "status": "success",
                "message": f"You found {new_food.name} ({food_value} food value)!",
                "data": {"satiety_value": food_value, "current_satiety": self.satiety},
            }

        log.info("You found food but inventory is full!")
        return {
            "status": "fail",
            "message": "You found food but inventory is full!",
            "data": {"satiety_value": 0, "current_satiety": self.satiety},
        }

    def find_water(self) -> dict[str, Any]:
        """Searches for water in the environment with a chance of success.

        If successful, a Drink item is created and added to the soul's inventory.
        Success rate is fixed at 70%.

        Returns:
            A dictionary containing:
                - status (str): 'success' if water was found and added, 'fail' if
                  nothing found or inventory full.
                - message (str): Descriptive result of the search.
                - data (dict): Contains 'hydration_value' and 'current_hydration'.
        """
        success_rate = 0.7  # 70% chance to find water.
        if random.random() > success_rate:
            log.info(f"{self.name} searched for water but found nothing.")
            return {
                "status": "fail",
                "message": "You searched for water but found nothing.",
                "data": {"hydration_value": 0, "current_hydration": self.hydration},
            }

        water_value = random.randint(10, 30)
        new_drink = Drink("Water Bottle", "Collected from a stream.", water_value)

        if self.inventory.add_item(new_drink):
            log.info(f"{self.name} found {new_drink.name} ({water_value} water value)!")
            return {
                "status": "success",
                "message": f"You found {new_drink.name} ({water_value} water value)!",
                "data": {
                    "hydration_value": water_value,
                    "current_hydration": self.hydration,
                },
            }

        log.info("You found water but inventory is full!")
        return {
            "status": "fail",
            "message": "You found water but inventory is full!",
            "data": {"hydration_value": 0, "current_hydration": self.hydration},
        }

    def random_event(self) -> None:
        """Triggers random events based on the soul's level of needs."""
        if self.satiety < 20 or self.hydration < 20:
            event = random.choice(["hallucination", "fatigue"])
            print(f"Due to low levels, {self.name} experiences {event}!")

    def name_child(self, child: Soul) -> None:
        """Names a child soul based on gender and known names.

        Args:
            child: The child Soul instance to be named.
        """
        if not child.first_name:
            if child.gender.gender_name == "male" or (
                hasattr(child.gender, "gender_name")
                and child.gender.gender_name == "male"
            ):
                names = self.known_male_first_names
            else:
                names = self.known_female_first_names  # simplified check

            if names:
                child.first_name = random.choice(names)
            else:
                child.first_name = "Unnamed"

        if not child.family_name:
            child.family_name = self.family_name

        child.name = child.get_full_name()  # Update display name

        gender_str = "boy" if child.gender.gender_name == "male" else "girl"
        pronoun = "him" if child.gender.gender_name == "male" else "her"
        print(f"{self.name} had a baby {gender_str} and named {pronoun} {child.name}")

    def claim_child(self, child: Soul) -> None:
        """Sets the parent-child relationship based on this soul's gender.

        Args:
            child: The child Soul instance.
        """
        if self.gender.gender_name == "male":
            child.birth_father = self
        elif self.gender.gender_name == "female":
            child.birth_mother = self

    def give_birth(self) -> Soul | None:
        """Simulates giving birth to a new soul if gender allows.

        Returns:
            A new Soul instance if successful, None otherwise.
        """
        if self.gender.can_give_birth:
            child = Soul(
                species=self.species,
                gender=None,
                birth_mother=self,
                current_location=self.current_location,
                # Pass None for visual args to default
            )
            print(f"{self.name} gave birth.")
            return child
        else:
            print(f"{self.gender}s can't give birth.")
            return None

    def attack(self, target: Soul) -> int | None:
        """Attacks another soul and deals damage based on stats.

        Args:
            target: The Soul instance to attack.

        Returns:
            The amount of damage dealt as an integer, or None if invalid.
        """
        if self.is_dead():
            print("You are dead, you can't attack!")
            return None
        if not target or target.is_dead():
            print("Target is invalid or already dead.")
            return None

        damage = self.stats.attack - target.stats.defense
        if damage < 0:
            damage = 0
        if damage == 0 and self.stats.attack > 0:
            damage = 1

        target.current_health -= damage
        print(f"{self.name} attacks {target.name} for {damage} damage!")

        if target.current_health <= 0:
            target.current_health = 0
            print(f"You killed {target.name}!")

        return damage

    # --- Main Update ---

    def to_dict(self) -> dict[str, Any]:
        """Serializes the soul's state to a dictionary for persistence.

        Returns:
            A dictionary containing name, colors, position, and stats.
        """
        x, y = int(self.window_physics.x), int(self.window_physics.y)

        return {
            "name": self.name,
            "orb_color": self.orb_color_rgb,
            "aura_color": self.aura_color_rgb,
            "position": (x, y),
            "stats": self.stats.to_dict(),
            "satiety": self.satiety,
            "hydration": self.hydration,
            "inventory": self.inventory.to_dict(),
        }

    def update(self, dt: float) -> None:
        """Main update loop for this soul.

        Args:
            dt: Delta time (time elapsed since last frame) in seconds.
        """
        self.time += dt

        # Physics Update
        self.window_physics.update(dt)

        # Visual Update
        angle = self.time * 0.5
        self.bulge_position = [
            math.cos(angle) * 0.5,
            math.sin(angle * 0.7) * 0.35,
            math.sin(angle) * 0.5,
        ]

        # Simulation Update (Tick-based)
        if self.is_alive():
            self._simulation_time_accumulator += dt
            if self._simulation_time_accumulator >= self.update_interval:
                self._simulation_time_accumulator -= self.update_interval
                self.decrease_satiety()
                self.decrease_hydration()
                self.check_status()

            # Agent Decision Update
            if self.agent:
                time_since = self.time - self.last_decision_time
                if time_since > self.decision_interval:
                    log.info(
                        "Triggering agent decision for %s (time_since=%.2f)",
                        self.name,
                        time_since,
                    )
                    self.last_decision_time = self.time
                    # Run agent decision in a separate thread to avoid blocking the game loop
                    threading.Thread(target=self._run_agent_step).start()

    def _run_agent_step(self) -> None:
        """Executes a single step of the agent's reasoning loop in a thread."""
        log.debug(f"Agent thread started for {self.name}")
        try:
            # Construct a query based on state
            query = f"Status: Satiety={self.satiety}, Hydration={self.hydration}. What should I do?"

            content = types.Content(role="user", parts=[types.Part(text=query)])

            # This call blocks until the agent completes its turn
            events = self.runner.run(
                user_id=f"user_{self.soul_id}",
                session_id=f"session_{self.soul_id}",
                new_message=content,
            )

            for event in events:
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        # Log tool calls.
                        if part.function_call:
                            log.info(
                                "Soul %s calling tool: %s",
                                self.name,
                                part.function_call.name,
                            )

                        # Log tool results.
                        if part.function_response:
                            log.info(
                                "Soul %s tool result: %s",
                                self.name,
                                part.function_response.response,
                            )

                        # Log final response text.
                        if event.is_final_response() and part.text:
                            log.info("Soul %s decided: %s", self.name, part.text)
        except Exception as e:
            log.error(f"Agent error in thread: {e}")

    def on_key_press(self, symbol: int, modifiers: int):
        """Pyglet event handler for key press.

        Args:
            symbol: The key symbol pressed.
            modifiers: Key modifiers (shift, ctrl, etc.).

        Returns:
            pyglet.event.EVENT_HANDLED if handled, None otherwise.
        """
        if symbol == key.A:  # Toggle aura visibility when 'A' is pressed
            self.aura_visible = not self.aura_visible
            return pyglet.event.EVENT_HANDLED

    def on_mouse_press(self, x: int, y: int, button: int, modifiers: int) -> bool:
        """Pyglet event handler for mouse press.

        Args:
            x: X coordinate of the mouse.
            y: Y coordinate of the mouse.
            button: Mouse button pressed.
            modifiers: Key modifiers.

        Returns:
            True if handled, False otherwise.
        """
        screen_height = self.window.height
        y_top_left = screen_height - y

        if button == mouse.RIGHT and self.on_right_click:
            if (
                self.x <= x <= self.x + self.width
                and self.y <= y_top_left <= self.y + self.height
            ):
                self.on_right_click(self, x, y_top_left)
                return True

        self.window_physics.on_mouse_press(x, y_top_left, button, modifiers)

    def on_mouse_drag(
        self, x: int, y: int, dx: int, dy: int, buttons: int, modifiers: int
    ) -> None:
        """Pyglet event handler for mouse drag.

        Args:
            x: X coordinate.
            y: Y coordinate.
            dx: Delta X.
            dy: Delta Y.
            buttons: Mouse buttons held.
            modifiers: Key modifiers.
        """
        screen_height = self.window.height
        y_top_left = screen_height - y
        self.window_physics.on_mouse_drag(x, y_top_left, dx, -dy, buttons, modifiers)

    def on_mouse_release(self, x: int, y: int, button: int, modifiers: int) -> None:
        """Pyglet event handler for mouse release.

        Args:
            x: X coordinate.
            y: Y coordinate.
            button: Mouse button.
            modifiers: Key modifiers.
        """
        screen_height = self.window.height
        y_top_left = screen_height - y
        self.window_physics.on_mouse_release(x, y_top_left, button, modifiers)

    def cleanup(self) -> None:
        """Cleans up all resources associated with this soul (clock, etc.)."""
        try:
            pyglet.clock.unschedule(self.update)
        except Exception:
            pass
        log.debug(f"Cleaned up resources for soul: {self.name}")
