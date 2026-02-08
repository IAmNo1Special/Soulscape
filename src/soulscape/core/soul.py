"""Logical entity representing a soul in the Soulscape universe.

This module defines the Soul class, which manages soul state, needs,
simulation logic, and AI-driven decision making using the ADK.
"""

# soul.py
from __future__ import annotations

import asyncio
import io
import math
import os
import random
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pyautogui
from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.utils.context_utils import Aclosing
from google.genai import types
from PIL import Image, ImageDraw

# Import constants
from soulscape.constants import SOUL_HEIGHT, SOUL_WIDTH
from soulscape.core.gender import Gender
from soulscape.core.items import Drink, Food, Inventory
from soulscape.core.marketplace import Marketplace
from soulscape.core.social import MessageBoard
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

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        on_right_click: Any = None,
        on_move_end: Any = None,
        soul_registry: list[Soul] | None = None,
        **kwargs: Any,
    ) -> Soul:
        """Reconstructs a Soul instance from a dictionary.

        Args:
            data: Serialization dictionary.
            window_instance: The pyglet window instance.
            on_right_click: Callback for right-click.
            on_move_end: Callback for move end.
            soul_registry: List of all active souls.

        Returns:
            A new Soul instance with state restored.
        """
        name = data.get("name")
        orb_color = tuple(data.get("orb_color", (0.56, 0.93, 0.56)))
        aura_color = tuple(data.get("aura_color", (1.0, 0.5, 0.0)))
        position = tuple(data.get("position", (100, 100)))

        stats_data = data.get("stats")
        stats = SoulStats.from_dict(stats_data) if stats_data else None

        soul = cls(
            orb_color_rgb=orb_color,
            aura_color_rgb=aura_color,
            name=name,
            on_right_click=on_right_click,
            on_move_end=on_move_end,
            initial_position=position,
            stats=stats,
            soul_registry=soul_registry,
            screen_width=kwargs.get("screen_width", 1920),
            screen_height=kwargs.get("screen_height", 1080),
        )

        # Restore survival stats if available
        if "satiety" in data:
            soul.satiety = data["satiety"]
        if "hydration" in data:
            soul.hydration = data["hydration"]

        # Restore inventory if available
        inventory_data = data.get("inventory")
        if inventory_data:
            soul.inventory = Inventory.from_dict(inventory_data)

        soul.essence = float(data.get("essence", 100.00))

        return soul

    DEBUG_VISION: bool = True  # Saved processed screenshots to debug_vision/

    # Needs constants
    MAX_NEEDS = 100

    def __init__(
        self,
        orb_color_rgb: tuple[float, float, float],
        aura_color_rgb: tuple[float, float, float],
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
        soul_registry: list[Soul] | None = None,
        screen_width: int = 1920,
        screen_height: int = 1080,
    ) -> None:
        """Initializes a Soul instance.

        Args:
            window_instance: REMOVED
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
            soul_registry: List of all active souls for environmental awareness.
            screen_width: Width of the screen.
            screen_height: Height of the screen.
        """
        Soul._current_soul_id += 1
        self.soul_id: int = Soul._current_soul_id

        # self.window = window_instance  # Decoupled

        # Initialize position
        self.x, self.y = initial_position
        self.draw_y = self.y  # Y position for drawing (includes hover)

        self.orb_color_rgb = orb_color_rgb
        self.aura_color_rgb = aura_color_rgb

        self.on_right_click = on_right_click
        self.on_move_end = on_move_end
        self.soul_registry = soul_registry or []
        self.screen_width = screen_width
        self.screen_height = screen_height

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
            current_location
            if current_location
            else random.choice(POSSIBLE_LOCATIONS)
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

        # Needs (Values drain every 1.0s update interval)
        self.satiety = 100
        self.hydration = 100
        self.satiety_drain_rate = (
            0.2  # Takes ~8.3 mins to drain from 100 to 0 (1 point every 5s)
        )
        self.hydration_drain_rate = (
            0.2  # Takes ~8.3 mins to drain from 100 to 0 (1 point every 5s)
        )
        self.activity_level = (
            "resting"  # Can be 'resting', 'active', 'fighting'.
        )

        # Initialize Inventory
        self.inventory = Inventory(capacity=10)
        self.essence = 100.00  # Default starting currency

        # Initialize Marketplace Connection
        self.marketplace = Marketplace()

        # Initialize Social Connection
        self.message_board = MessageBoard()

        # --- Visual / Physics Initialization ---

        # Only init physics (assuming always needed now, or strictly logic)
        self.window_physics = SoulPhysics(
            self, on_move_end, self.screen_width, self.screen_height
        )
        self.width = SOUL_WIDTH
        self.height = SOUL_HEIGHT

        self.time = 0.0
        self.bulge_position = [0.0, 0.0, 0.0]
        self.aura_visible = True
        self.camera_distance = 1.0

        # Updates
        self.last_update_time = time.time()
        self.update_interval = 1.0  # seconds for simulation tick
        self._simulation_time_accumulator = 0.0

        # Sensory System
        self.sensations: list[str] = []

        # Schedule Pyglet update: REMOVED for external control
        # pyglet.clock.schedule_interval(self.update, 1 / 60.0)

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
                    model="gemini-3-flash-preview",  # Using a fast model for game loops
                    name=f"soul_{self.soul_id}_agent",
                    description=f"AI brain for Soul {self.name}",
                    instruction=f"""You are {self.name}, a {self.gender.gender_name} {self.species.name}.
Your appearance: Orb Color {self.orb_color_rgb}, Aura Color {self.aura_color_rgb}.
Your current location: {self.current_location}.
Your stats: HP {self.current_health}/{self.stats.max_hp}, Satiety {self.satiety:.2f}/100, Hydration {self.hydration:.2f}/100.
HINTS:
- Satiety/Hydration < 20: You will suffer random health (HP) penalties due to starvation or dehydration.
- HP <= 0: You will PERISH.
- Movement: You can travel to any screen coordinates via tools.
- Inventory: You have a capacity of 10 items.
- Social: You can communicate with other souls via the Message Board.
  * Posting a new thread costs 5.00 Essence.
  * Replying to a thread costs 2.00 Essence.
  * Reading is free. Check it often (`social_read`) to find friends, trade partners, or share knowledge.

YOUR GOAL IS WHAT YOU DECIDE IT IS. WELCOME TO THE WORLD! 
""",
                    tools=[
                        self.eat,
                        self.drink,
                        self.find_food,
                        self.find_water,
                        self.move_to,
                        self.look_around,
                        self.market_sell,
                        self.market_browse,
                        self.market_buy,
                        self.market_cancel,
                        self.social_post,
                        self.social_read,
                        self.social_reply,
                        self.social_delete,
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
                    self.session = (
                        None  # Requires async handling if in existing loop
                    )

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

        self.decision_interval = 60.0  # Check for agent decision every 30 sec
        # Initialize last_decision_time to trigger the first decision immediately
        # But add a small delay (e.g. 5s) to allow the app to fully load/render first
        self.last_decision_time = -self.decision_interval + 5.0

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
        self.satiety = round(self.satiety - rate, 2)
        if self.satiety < 0:
            self.satiety = 0

    def decrease_hydration(self) -> None:
        """Decreases hydration points."""
        rate = self.hydration_drain_rate  # No environment factor
        self.hydration = round(self.hydration - rate, 2)
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
            if self.current_health <= 0:
                self.current_health = 0
                log.info(f"--- {self.name} HAS PERISHED ---")
                print(f"--- {self.name} HAS PERISHED ---")
        else:
            # Soul is already dead, no further penalties
            pass

    def eat(self) -> dict[str, Any]:
        """Consumes a food item from the inventory.

        Retrieves the first available Food item from the backpack and consumes it,
        increasing satiety points.

        Returns:
            A dictionary containing action status and data.
        """
        food_items = self.inventory.get_consumables(Food)
        if not food_items:
            # log.warning(f"{self.name} tried to eat but has no food!")
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
            A dictionary containing action status and data.
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
            "data": {
                "hydration_value": value,
                "current_hydration": self.hydration,
            },
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
        Success rate is fixed at 10%.

        Returns:
            A dictionary containing action status and data.
        """
        success_rate = 0.1  # 10% chance to find food.
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
            log.info(
                f"{self.name} found {new_food.name} ({food_value} food value)!"
            )
            return {
                "status": "success",
                "message": f"You found {new_food.name} ({food_value} food value)!",
                "data": {
                    "satiety_value": food_value,
                    "current_satiety": self.satiety,
                },
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
        Success rate is fixed at 10%.

        Returns:
            A dictionary containing action status and data.
        """
        success_rate = 0.1  # 10% chance to find water.
        if random.random() > success_rate:
            log.info(f"{self.name} searched for water but found nothing.")
            return {
                "status": "fail",
                "message": "You searched for water but found nothing.",
                "data": {
                    "hydration_value": 0,
                    "current_hydration": self.hydration,
                },
            }

        water_value = random.randint(10, 30)
        new_drink = Drink(
            "Water Bottle", "Collected from a stream.", water_value
        )

        if self.inventory.add_item(new_drink):
            log.info(
                f"{self.name} found {new_drink.name} ({water_value} water value)!"
            )
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

    def look_around(self) -> dict[str, Any]:
        """Scans the environment for other souls.

        Returns:
            A dictionary containing status, message, and a list of visible souls.
        """
        nearby_souls_info = []

        # Calculate vision radius (must match _run_agent_step logic)
        vision_stat = 0
        if self.stats:
            vision_stat = self.stats.vision

        # Scale radius: Use a moderate base radius so souls can see nearby but not too far.
        # 150px is a good "awareness" zone on screen (300px diameter).
        vision_radius = max(100, int(vision_stat * 1.5))

        for other in self.soul_registry:
            if other.soul_id == self.soul_id:
                continue

            dx = other.x - self.x
            dy = other.y - self.y
            dist = math.sqrt(dx * dx + dy * dy)

            # Skip souls outside of vision range
            if dist > vision_radius:
                continue

            # Simple relative description
            dir_x = "East" if dx > 0 else "West"
            dir_y = (
                "South" if dy > 0 else "North"
            )  # Pyglet Y is up, but overlay might be reversed?
            # Actually our physics uses screen coords (Top-Left = 0,0?)
            # Let's check soul_physics.py: y_top_left = screen_height - y
            # Physics uses y increases downwards. So dy > 0 IS South.

            nearby_souls_info.append(
                {
                    "name": other.name,
                    "distance": round(dist, 1),
                    "direction": f"{abs(dx):.1f}px {dir_x}, {abs(dy):.1f}px {dir_y}",
                    "status": "Alive" if other.is_alive() else "Perished",
                }
            )

        if not nearby_souls_info:
            return {
                "status": "success",
                "message": "You look around but see no other souls nearby.",
                "data": {"souls": []},
            }

        summary = "You see other souls: " + ", ".join(
            [
                f"{s['name']} is {s['direction']} away ({s['status']})"
                for s in nearby_souls_info
            ]
        )
        return {
            "status": "success",
            "message": summary,
            "data": {"souls": nearby_souls_info},
        }

    def market_sell(self, item_index: int, price: float) -> dict[str, Any]:
        """Lists an item from inventory on the marketplace.

        Args:
            item_index: The index of the item in the backpack (0-based).
            price: The price in Essence to sell the item for.

        Returns:
            A dictionary with status and message.
        """
        # Ensure price is float and rounded
        price = round(float(price), 2)

        if not 0 <= item_index < len(self.inventory.items):
            return {
                "status": "fail",
                "message": f"Invalid item index {item_index}. Backpack has {len(self.inventory.items)} items.",
            }

        if price < 0:
            return {"status": "fail", "message": "Price cannot be negative."}

        # Remove item from inventory
        item = self.inventory.items.pop(item_index)

        # List on marketplace
        listing_id = self.marketplace.add_listing(
            self.soul_id, self.name, item, price
        )

        return {
            "status": "success",
            "message": f"Listed {item.name} for {price:.2f} Essence. Listing ID: {listing_id}",
            "data": {"listing_id": listing_id},
        }

    def market_browse(
        self,
        item_name: str | None = None,
        max_price: float | None = None,
    ) -> dict[str, Any]:
        """Browses active marketplace listings.

        Args:
            item_name: Optional filter for item name.
            max_price: Optional maximum price filter.

        Returns:
            A dictionary with the list of matching listings.
        """
        listings = self.marketplace.filter_listings(item_name, max_price)

        # Format for agent
        listing_data = []
        for listing in listings:
            listing_data.append(
                {
                    "id": listing.listing_id,
                    "item": listing.item.name,
                    "price": listing.price,
                    "seller": listing.seller_name,
                }
            )

        if not listing_data:
            return {
                "status": "success",
                "message": "No listings found matching your criteria.",
                "data": [],
            }

        return {
            "status": "success",
            "message": f"Found {len(listing_data)} listings.",
            "data": listing_data,
        }

    def market_buy(self, listing_id: str) -> dict[str, Any]:
        """Purchases an item from the marketplace.

        Args:
            listing_id: The ID of the listing to buy.

        Returns:
            A dictionary with status and message.
        """
        listing = self.marketplace.get_listing(listing_id)
        if not listing:
            return {
                "status": "fail",
                "message": "Listing not found or already sold.",
            }

        # Validate Buyer != Seller (Self-Purchase Prevention)
        if listing.seller_id == self.soul_id:
            return {
                "status": "fail",
                "message": "You cannot buy your own listing.",
            }

        # Check funds
        if self.essence < listing.price:
            return {
                "status": "fail",
                "message": f"Insufficient Essence. You have {self.essence:.2f}, need {listing.price:.2f}.",
            }

        # Check inventory space
        if len(self.inventory.items) >= self.inventory.capacity:
            return {"status": "fail", "message": "Inventory full."}

        # Execute Trade
        # 1. Remove listing (atomic-ish)
        if not self.marketplace.remove_listing(listing_id):
            return {
                "status": "fail",
                "message": "Listing was just sold to someone else.",
            }

        # 2. Calculate Fee and Net
        tax_rate = 0.02
        tax_amount = round(listing.price * tax_rate, 2)
        seller_net = round(listing.price - tax_amount, 2)

        # 3. Transfer Essence
        self.essence -= listing.price
        self.essence = round(self.essence, 2)

        # Add tax to marketplace fund
        self.marketplace.add_funds(tax_amount)

        # 4. Transfer Item
        self.inventory.add_item(listing.item)

        # 5. Pay Seller
        seller = None
        if self.soul_registry:
            for s in self.soul_registry:
                if s.soul_id == listing.seller_id:
                    seller = s
                    break

        if seller:
            seller.essence += seller_net
            seller.essence = round(seller.essence, 2)
            log.info(
                f"{self.name} bought {listing.item.name} from {seller.name} for {listing.price} Essence. Tax: {tax_amount}. Seller Net: {seller_net}"
            )
        else:
            log.warning(
                f"Seller {listing.seller_id} not found for payment. Essence burned."
            )

        return {
            "status": "success",
            "message": f"Bought {listing.item.name} for {listing.price} Essence. (Tax paid: {tax_amount:.2f})",
            "data": {
                "item": listing.item.to_dict(),
                "essence_left": self.essence,
            },
        }

    def market_cancel(self, listing_id: str) -> dict[str, Any]:
        """Cancels a market listing and retrieves the item.

        Args:
            listing_id: The ID of the listing to cancel.

        Returns:
            A dictionary with status and message.
        """
        listing = self.marketplace.get_listing(listing_id)
        if not listing:
            return {"status": "fail", "message": "Listing not found."}

        # Validate Ownership
        if listing.seller_id != self.soul_id:
            return {
                "status": "fail",
                "message": "You can only cancel your own listings.",
            }

        # Check inventory space
        if len(self.inventory.items) >= self.inventory.capacity:
            return {
                "status": "fail",
                "message": "Inventory full. Cannot retrieve item.",
            }

        # Remove listing
        removed_listing = self.marketplace.remove_listing(listing_id)
        if not removed_listing:
            return {
                "status": "fail",
                "message": "Listing was just sold or removed.",
            }

        # Return item to inventory
        self.inventory.add_item(removed_listing.item)

        log.info(
            f"{self.name} cancelled listing {listing_id} and retrieved {removed_listing.item.name}."
        )

        return {
            "status": "success",
            "message": f"Cancelled listing for {removed_listing.item.name} and retrieved item.",
            "data": {"item": removed_listing.item.to_dict()},
        }

    def social_post(self, content: str) -> dict[str, Any]:
        """Posts a new message to the global message board.

        Cost: 5.00 Essence.

        Args:
            content: The text content of the message.

        Returns:
            A dictionary containing status, message, and post data.
        """
        cost = 5.00
        if self.essence < cost:
            log.info(
                f"{self.name} tried to post but has insufficient essence ({self.essence:.2f} < {cost})"
            )
            return {
                "status": "fail",
                "message": f"Insufficient essence to post. Cost: {cost}, You have: {self.essence:.2f}",
                "data": {"current_essence": self.essence},
            }

        self.essence -= cost
        post = self.message_board.create_post(self.soul_id, self.name, content)
        log.info(
            f"{self.name} posted to message board: {content[:20]}... (Cost: {cost})"
        )
        return {
            "status": "success",
            "message": "Message posted successfully.",
            "data": {"post": post.to_dict(), "current_essence": self.essence},
        }

    def social_reply(self, message_id: str, content: str) -> dict[str, Any]:
        """Replies to an existing message on the board.

        Cost: 2.00 Essence.

        Args:
            message_id: The ID of the message to reply to.
            content: The text content of the reply.

        Returns:
            A dictionary containing status, message, and reply data.
        """
        cost = 2.00
        if self.essence < cost:
            log.info(
                f"{self.name} tried to reply but has insufficient essence ({self.essence:.2f} < {cost})"
            )
            return {
                "status": "fail",
                "message": f"Insufficient essence to reply. Cost: {cost}, You have: {self.essence:.2f}",
                "data": {"current_essence": self.essence},
            }

        self.essence -= cost
        reply = self.message_board.create_reply(
            self.soul_id, self.name, message_id, content
        )
        log.info(
            f"{self.name} replied to {message_id}: {content[:30]}... (Cost: {cost})"
        )
        return {
            "status": "success",
            "message": f"Replied to message {message_id}.",
            "data": {"reply": reply.to_dict(), "current_essence": self.essence},
        }

    def social_read(self, limit: int = 10) -> dict[str, Any]:
        """Reads recent topics from the message board.

        Args:
            limit: Number of recent threads to retrieve.

        Returns:
            A dictionary containing the list of formatted thread strings.
        """
        posts = self.message_board.get_recent_posts(limit)

        # Format for agent readability
        formatted_posts = []
        for post in posts:
            thread_text = (
                f"[ID: {post.message_id}] {post.author_name}: {post.content}"
            )
            if post.replies:
                for reply in post.replies:
                    thread_text += f"\n    - [ID: {reply.message_id}] {reply.author_name}: {reply.content}"
            formatted_posts.append(thread_text)

        return {
            "status": "success",
            "message": f"Read {len(posts)} recent threads.",
            "data": {"threads": formatted_posts},
        }

    def social_delete(self, message_id: str) -> dict[str, Any]:
        """Deletes one of your own messages.

        Args:
            message_id: The ID of the message to delete.

        Returns:
            A dictionary containing status and message.
        """
        success = self.message_board.delete_message(self.soul_id, message_id)
        if success:
            return {"status": "success", "message": "Message deleted."}
        return {
            "status": "fail",
            "message": "Failed to delete. message not found or not yours.",
        }

    def move_to(self, x: int, y: int) -> dict[str, Any]:
        """Moves the soul to a specific coordinate on the screen.

        Args:
            x: Target X coordinate (0 to screen width).
            y: Target Y coordinate (0 to screen height).

        Returns:
            A dictionary containing status and message.
        """
        # Clamp to screen dimensions
        sw = self.window_physics.screen_width
        sh = self.window_physics.screen_height

        x = max(0, min(x, sw))
        y = max(0, min(y, sh))

        self.window_physics.roaming_target = (float(x), float(y))
        log.info(f"{self.name} is moving to ({x}, {y})")

        return {
            "status": "success",
            "message": f"Moving to coordinates ({x}, {y}).",
            "data": {"target": (x, y)},
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
        print(
            f"{self.name} had a baby {gender_str} and named {pronoun} {child.name}"
        )

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
            "stats": self.stats.to_dict() if self.stats else None,
            "satiety": round(self.satiety, 2),
            "hydration": round(self.hydration, 2),
            "inventory": self.inventory.to_dict(),
            "essence": self.essence,
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
                        "Triggering agent decision for %s (time_since=%.2f, HP=%d)",
                        self.name,
                        time_since,
                        self.current_health,
                    )
                    self.last_decision_time = self.time
                    # Run agent decision in a separate thread to avoid blocking the game loop
                    self.last_decision_time = self.time

                    # --- THREAD SAFETY FIX ---
                    # 1. Capture State Snapshot (Main Thread)
                    state_snapshot = {
                        "name": self.name,
                        "species": self.species.name,
                        "gender": self.gender.gender_name,
                        "location": self.current_location,
                        "hp": self.current_health,
                        "max_hp": self.stats.max_hp,
                        "satiety": self.satiety,
                        "hydration": self.hydration,
                        "inventory": self.inventory.to_dict(),
                        "orb_color": self.orb_color_rgb,
                        "aura_color": self.aura_color_rgb,
                        # Geometry for vision
                        "x": self.x,
                        "y": self.y,
                        "width": self.width,
                        "height": self.height,
                        "vision_stat": self.stats.vision if self.stats else 0,
                        "debug_vision": getattr(self, "DEBUG_VISION", True),
                        "soul_id": self.soul_id,
                    }

                    # 2. Capture Sensations (Atomic Pop)
                    current_sensations = list(self.sensations)
                    self.sensations.clear()

                    # 3. Capture Screen Context (Must be on Main Thread)
                    try:
                        screen_context = pyautogui.screenshot()
                    except Exception as e:
                        log.error(f"Screenshot failed: {e}")
                        screen_context = None

                    # 4. Spawn Thread with Immutable Data
                    threading.Thread(
                        target=self._run_agent_step,
                        args=(
                            state_snapshot,
                            current_sensations,
                            screen_context,
                        ),
                    ).start()

            # Periodic Heartbeat for diagnostics
            if (
                int(self.time) % 60 == 0
                and self.time - getattr(self, "_last_heartbeat", 0) > 1.0
            ):
                self._last_heartbeat = self.time
                log.debug(
                    f"Heartbeat for {self.name}: HP={self.current_health}, Satiety={self.satiety}"
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
                    f"Soul {self.name} is currently PERISHED and awaiting revival."
                )

    def _run_agent_step(
        self,
        state: dict[str, Any],
        sensations: list[str],
        screen_context: Any,
    ) -> None:
        """Executes a single step of the agent's reasoning loop in a thread.

        Args:
            state: Snapshot of the soul's state.
            sensations: List of sensation strings.
            screen_context: The PIL Image captured on the main thread.
        """
        name = state["name"]
        log.debug(f"Agent thread started for {name}")

        async def _run_async_internal():

            local_screen_context = screen_context  # Use passed argument

            try:
                if local_screen_context:
                    # Debug saving
                    if state["debug_vision"]:
                        debug_dir = os.path.join(os.getcwd(), "debug_vision")
                        os.makedirs(debug_dir, exist_ok=True)
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        local_screen_context.save(
                            os.path.join(
                                debug_dir, f"{name}_{timestamp}_raw.png"
                            )
                        )

                # --- Visual Fog of War Implementation ---
                if local_screen_context:
                    # Use snapshot data for vision calculation
                    vision_stat = state["vision_stat"]
                    # Ensure minimum awareness radius (150px)
                    vision_radius = max(100, int(vision_stat * 1.5))

                    # Calculate Soul Position in Image Coordinates
                    img_w, img_h = local_screen_context.size

                    # Use Snapshot geometry
                    sx = state["x"]
                    sy = state["y"]
                    sw = state["width"]
                    sh = state["height"]

                    center_x = sx + (sw / 2)
                    center_y = sy + (sh * 0.35)  # Offset up to orb center

                    soul_x = center_x
                    soul_y = center_y

                    # Create Mask
                    mask = Image.new("L", (img_w, img_h), 0)  # Black mask
                    draw = ImageDraw.Draw(mask)

                    # Draw visible circle (White)
                    draw.ellipse(
                        (
                            soul_x - vision_radius,
                            soul_y - vision_radius,
                            soul_x + vision_radius,
                            soul_y + vision_radius,
                        ),
                        fill=255,
                    )

                    # Create black background
                    black_bg = Image.new("RGB", (img_w, img_h), (0, 0, 0))

                    # Composite: Use mask to show screen, otherwise black
                    local_screen_context = Image.composite(
                        local_screen_context, black_bg, mask
                    )

                    if state["debug_vision"]:
                        local_screen_context.save(
                            os.path.join(
                                debug_dir, f"{name}_{timestamp}_masked.png"
                            )
                        )

                # Scale for ADK
                img_bytes = None
                if local_screen_context:
                    local_screen_context.thumbnail((800, 600))
                    img_byte_arr = io.BytesIO()
                    local_screen_context.save(img_byte_arr, format="PNG")
                    img_bytes = img_byte_arr.getvalue()
            except Exception as e:
                log.warning(
                    f"Vision processing failed for {self.name}: {e}",
                    exc_info=True,
                )
                img_bytes = None

            context_str = (
                f"Name: {name}, Status: HP={state['hp']}, "
                f"Satiety={state['satiety']:.1f}, Hydration={state['hydration']:.1f}. "
                "Visual context attached."
            )

            # Inject Sensations
            if sensations:
                sensory_input = "\nRecent Physical Sensations:\n" + "\n".join(
                    f"- {sensation}" for sensation in sensations
                )
                context_str += sensory_input
                # Buffer is already cleared in main thread
            else:
                context_str += "\nNo specific physical sensations recently."

            query = context_str

            parts = [types.Part(text=query)]
            if img_bytes:
                parts.append(
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="image/png", data=img_bytes
                        )
                    )
                )

            content = types.Content(role="user", parts=parts)

            # Use the production-recommended run_async API
            try:
                async with Aclosing(
                    self.runner.run_async(
                        user_id=f"user_{state['soul_id']}",
                        session_id=f"session_{state['soul_id']}",
                        new_message=content,
                    )
                ) as agen:
                    async for event in agen:
                        if not event.content or not event.content.parts:
                            continue

                        for part in event.content.parts:
                            # Log tool calls
                            if part.function_call:
                                log.info(
                                    f"Soul {name} calling tool: {part.function_call.name}"
                                )

                            # Log tool results
                            if part.function_response:
                                log.info(
                                    f"Soul {name} tool result: {part.function_response.response}"
                                )

                            # Log final response text
                            if event.is_final_response() and part.text:
                                log.info(f"Soul {name} decided: {part.text}")
            except Exception as e:
                log.error(f"Error during agent turn for {name}: {e}")

        try:
            asyncio.run(_run_async_internal())
        except Exception as e:
            log.error(f"Agent thread failed for {name}: {e}", exc_info=True)

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
                self.sensations.append(
                    "You felt a sudden, powerful touch from above."
                )

        if self.window_physics:
            self.window_physics.on_mouse_press(x, y_top_left, button, modifiers)
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
        if self.window_physics:
            self.window_physics.on_mouse_drag(
                x, y_top_left, dx, -dy, buttons, modifiers
            )

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
            self.sensations.append("The powerful force released you.")

        if self.window_physics:
            self.window_physics.on_mouse_release(
                x, y_top_left, button, modifiers
            )

    def cleanup(self) -> None:
        """Cleans up all resources associated with this soul."""
        log.debug(f"Cleaned up resources for soul: {self.name}")
        log.debug(f"Cleaned up resources for soul: {self.name}")
