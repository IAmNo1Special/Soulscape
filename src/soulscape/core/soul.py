"""This module defines the Soul class, which manages soul state, biology,
physics-based movement, and AI-driven decision-making using the ADK.
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

# Import constants
from soulscape.constants import SOUL_HEIGHT, SOUL_WIDTH
from soulscape.core.gender import Gender, all_genders
from soulscape.core.items import Drink, Food, Inventory
from soulscape.core.marketplace import Marketplace
from soulscape.core.social import MessageBoard
from soulscape.core.soul_components.agent import SoulAgent
from soulscape.core.soul_components.physics import SoulPhysics
from soulscape.core.species import Species
from soulscape.core.stats import SoulStats
from soulscape.system.logger import log

# Global list of locations (moved from Isekai.py)
POSSIBLE_LOCATIONS = [
    "Capital City",
    "Small Village",
    "Forest Cabin",
    "Mountain Fortress",
]


# --- Main Application Class ---
class Soul:
    """An autonomous entity within the Soulscape simulation.

    The Soul class is the core actor in the ecosystem. It possesses
    needs (satiety, hydration, etc...), digital presence in the window environment,
    and an AI 'brain' (LlmAgent) that allows it to perceive its surroundings
    and take meaningful actions via available tools.

    Attributes:
        soul_id: A unique integer identifier for the soul instance.
        name: The display name of the soul.
        species: The Species object defining biological defaults.
        gender: The Gender object defining reproductive capabilities.
        stats: SoulStats containing HP, attack, defense, and vision.
        inventory: An Inventory instance for holding items.
        essence: The currency used for social and magical actions.
        window_physics: A SoulPhysics instance handling screen movement.
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
        """Reconstructs a Soul instance from its serialized representation.

        Args:
            data: A dictionary containing the soul's serialized state.
            on_right_click: An optional callback invoked when the soul is
                right-clicked.
            on_move_end: An optional callback invoked when the soul finishes a
                movement action.
            soul_registry: A list of all active souls in the simulation.
            **kwargs: Additional configuration parameters like screen_width.

        Returns:
            A new Soul instance with its state fully restored.
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
        """Initializes a new Soul entity.

        Args:
            orb_color_rgb: A tuple of (red, green, blue) floats (0.0-1.0)
                representing the soul's central orb color.
            aura_color_rgb: A tuple of (red, green, blue) floats (0.0-1.0)
                representing the soul's surrounding aura color.
            name: The soul's full name. If None, a default name will be assigned.
            on_right_click: Callback triggered on right-click.
            on_move_end: Callback triggered when a movement target is reached.
            initial_position: The starting (x, y) screen coordinates.
            stats: Pre-defined SoulStats. If None, random stats are generated.
            species: The biological species. Defaults to Human if None.
            birth_mother: The soul's mother instance, if applicable.
            birth_father: The soul's father instance, if applicable.
            gender: The soul's gender. If None, a random gender from its species
                definition is selected.
            current_location: The initial location name for the soul.
            soul_registry: A reference to the global list of active souls.
            screen_width: The width of the desktop in pixels.
            screen_height: The height of the desktop in pixels.
        """
        Soul._current_soul_id += 1
        self.soul_id: int = Soul._current_soul_id

        # Initialize position
        self.x: float = float(initial_position[0])
        self.y: float = float(initial_position[1])
        self.draw_y: float = self.y  # Y position for drawing (includes hover)

        self.orb_color_rgb = orb_color_rgb
        self.aura_color_rgb = aura_color_rgb

        self.on_right_click: Any = on_right_click
        self.on_move_end: Any = on_move_end
        self.soul_registry: list[Soul] = soul_registry or []
        self.screen_width: int = screen_width
        self.screen_height: int = screen_height

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
            self.gender: Gender = random.choice(all_genders)
        else:
            self.gender: Gender = gender

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
        self.known_male_first_names: list[str] = [
            "Jackson",
            "John",
            "Jack",
            "Malcom",
            "Fatin",
        ]
        self.known_female_first_names: list[str] = [
            "Jane",
            "Lily",
            "Mallory",
            "Fatima",
            "Fatinah",
        ]

        # Needs (Values drain every 1.0s update interval)
        self.satiety: float = 100.0
        self.hydration: float = 100.0
        self.satiety_drain_rate: float = (
            0.2  # Takes ~8.3 mins to drain from 100 to 0 (1 point every 5s)
        )
        self.hydration_drain_rate: float = (
            0.2  # Takes ~8.3 mins to drain from 100 to 0 (1 point every 5s)
        )
        self.activity_level: str = (
            "resting"  # Can be 'resting', 'active', 'fighting'.
        )

        # Initialize Inventory
        self.inventory: Inventory = Inventory(capacity=10)
        self.essence: float = 100.00  # Default starting currency

        # Initialize Marketplace Connection
        self.marketplace = Marketplace()

        # Initialize Social Connection
        self.message_board = MessageBoard()

        # --- Visual / Physics Initialization ---

        # Only init physics (assuming always needed now, or strictly logic)
        self.physics = SoulPhysics(
            self, on_move_end, self.screen_width, self.screen_height
        )
        self.width = SOUL_WIDTH
        self.height = SOUL_HEIGHT

        self.time: float = 0.0
        self.bulge_position: list[float] = [0.0, 0.0, 0.0]
        self.aura_visible: bool = True
        self.camera_distance: float = 1.0

        # Updates
        self.last_update_time: float = time.time()
        self.update_interval: float = 1.0  # seconds for simulation tick
        self._simulation_time_accumulator: float = 0.0

        # Sensory System
        self.sensations: list[str] = []

        self.agent: SoulAgent = SoulAgent(soul=self)

    # --- Simulation Methods ---

    def get_full_name(self) -> str:
        """Assembles the first and family names into a readable format.

        Returns:
            The combined full name, or a generic placeholder if not named.
        """
        if self.first_name and self.family_name:
            return f"{self.first_name} {self.family_name}"
        elif self.first_name:
            return self.first_name
        return f"Soul #{self.soul_id}"

    def _set_birth_datetime(self) -> None:
        """Records the soul's precise birth time."""
        self.birth_datetime = datetime.now()

    def _set_hometown(self) -> None:
        """Sets the hometown to current location for reference."""
        self.hometown = self.current_location

    def get_id(self) -> int:
        """Returns the unique numeric ID of the soul."""
        return self.soul_id

    def get_species(self) -> Species:
        """Returns the species object associated with this soul."""
        return self.species

    def get_gender(self) -> str:
        """Returns the string name of the soul's gender."""
        return self.gender.gender_name if self.gender else "Unknown"

    def get_current_health(self) -> int:
        """Returns the current HP level."""
        return self.current_health

    def get_max_health(self) -> int:
        """Returns the maximum HP level from biological stats."""
        return self.stats.max_hp

    def get_birth_datetime(self) -> datetime:
        """Returns the timestamp of when this soul was created."""
        return self.birth_datetime

    def get_hometown(self) -> str:
        """Returns the name of the place where the soul was born."""
        return self.hometown

    def stop(self) -> None:
        """Gracefully halts the soul simulation and releases resources."""
        self.cleanup()

    def get_age(self) -> int:
        """Calculates time elapsed since birth in seconds."""
        if hasattr(self, "birth_datetime"):
            current_datetime: datetime = datetime.now()
            time_since_birth = current_datetime - self.birth_datetime
            return int(time_since_birth.total_seconds())
        return 0

    def is_alive(self) -> bool:
        """Checks if the soul's vitality is above zero."""
        return self.current_health > 0

    def is_dead(self) -> bool:
        """Checks if the soul's vitality has reached zero."""
        return self.current_health <= 0

    def decrease_satiety(self) -> None:
        """Naturally drains satiety over time based on current activity.

        This simulates biological energy consumption. More active souls burn
        energy faster.
        """
        rate = self.satiety_drain_rate * self.get_activity_multiplier()
        self.satiety = round(self.satiety - rate, 2)
        if self.satiety < 0:
            self.satiety = 0

    def decrease_hydration(self) -> None:
        """Naturally drains hydration over time.

        Simulates the constant need for fluids.
        """
        rate = self.hydration_drain_rate  # No environment factor
        self.hydration = round(self.hydration - rate, 2)
        if self.hydration < 0:
            self.hydration = 0

    def get_activity_multiplier(self) -> float:
        """Returns the energy consumption scale for current behavior."""
        if self.activity_level == "active":
            return 1.5
        elif self.activity_level == "fighting":
            return 2.0
        return 1.0

    def check_status(self) -> None:
        """Evaluates biological needs and applies starvation/dehydration damage.

        If needs are critically low, the soul's health will begin to wither.
        """
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
        """Reduces current HP as a penalty for neglected biological needs."""
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
        """Consumes a food item from the inventory to sate hunger.

        This tool searches the backpack for any available food source. If found,
        it replenishes the soul's energy and removes the item from inventory.

        Returns:
            A dictionary containing the success status and current satiety data.
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
        """Consumes a drink from the inventory to quench thirst.

        The soul searches for a liquid refreshment in its backpack. Drinking
        instantly restores hydration levels and clears the inventory slot.

        Returns:
            A dictionary with status feedback and new hydration levels.
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
        """Sets the current physical activity intensity."""
        if level in ["resting", "active", "fighting"]:
            self.activity_level = level
            print(f"{self.name} is now {self.activity_level}.")

    def find_food(self) -> dict[str, Any]:
        """Scours the immediate surroundings for sustenance.

        There is a 10% chance to find edibles. If found, the soul secures the
        bounty in its backpack for later consumption.

        Returns:
            A dictionary detailing the search outcome and any items found.
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
        """Searches for a clean source of water to fill a bottle.

        There is a 10% chance to find water. If found, the soul secures the
        bounty in its backpack for later consumption.

        Returns:
            A dictionary detailing the search results.
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
        """Utilizes the soul's sensory organs to perceive other nearby entities.

        Scans the immediate vicinity for other souls. Provides relative
        directions and distances to any detected presence within its vision
        radius.

        Returns:
            A dictionary containing status, message, and a list of visible souls.
        """
        nearby_souls_info = []

        # Calculate vision radius (must match _run_agent_step logic)
        vision_stat: float = 0.0
        if self.stats:
            vision_stat = float(self.stats.vision)

        # Scale radius: Use a moderate base radius so souls can see nearby but not too far.
        # 150px is a good "awareness" zone on screen (300px diameter).
        vision_radius: int = max(100, int(vision_stat * 1.5))

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
            dir_y = "South" if dy > 0 else "North"

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
        """Lists an item from the backpack on the global marketplace.

        The soul places an item up for sale, setting a fixed Essence price.
        The item is physically removed from the inventory and held by the
        marketplace until sold or cancelled.

        Args:
            item_index: The 0-based position of the item in the backpack list.
            price: The amount of Essence requested for the item.

        Returns:
            A dictionary confirming the listing details.
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
        """searches the global marketplace for items listed by others.

        Allows the soul to search for specific goods or filter by price to find
        the best deals in the local economy.

        Args:
            item_name: An optional part of the item name to search for.
            max_price: An optional maximum Essence price cap for the search.

        Returns:
            A dictionary containing a list of matching market listings.
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
        """Buys an item from the marketplace using Essence.

        The soul exchanges hard-earned Essence for desired goods. This action
        instantly transfers the item to the soul's backpack and compensates
        the seller (minus a 2% marketplace tax(from seller to marketplace)).

        Args:
            listing_id: The unique ID of the market listing to purchase.

        Returns:
            A dictionary confirming the transaction and item delivery.
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
        """Removes a personal listing from the marketplace.

        Allows the soul to reclaim an item that hasn't been sold yet, returning
        it from the market stall back into its own inventory.

        Args:
            listing_id: The unique ID of the listing to reclaim.

        Returns:
            A dictionary status regarding the retrieval.
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

    def social_post(self, title: str, content: str) -> dict[str, Any]:
        """Posts a new thread to the global message board.

        Allows the soul to share thoughts, ask questions, or interact with
        the world. This action consumes 5 Essence.

        Args:
            title: A brief, catchy header for the new thread.
            content: The detailed body text of the message.

        Returns:
            A dictionary confirming the success of the broadcast.
        """
        if not title.strip():
            return {
                "status": "fail",
                "message": "Title cannot be empty.",
            }
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
        post = self.message_board.create_post(
            self.soul_id, self.name, title, content
        )
        log.info(f"{self.name} posted to message board: {title} (Cost: {cost})")
        return {
            "status": "success",
            "message": "Message posted successfully.",
            "data": {"post": post.to_dict(), "current_essence": self.essence},
        }

    def social_reply(self, message_id: str, content: str) -> dict[str, Any]:
        """Contributes a reply to an existing discussion thread.

        Adds the soul's voice to an ongoing conversation. This action consumes 2 Essence.

        Args:
            message_id: The unique ID of the post being replied to.
            content: The detailed body text of the reply.

        Returns:
            A dictionary with feedback on the interaction.
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

    def social_read(
        self, limit: int = 10, message_id: str | None = None
    ) -> dict[str, Any]:
        """Browses or reads specific threads on the global message board.

        If message_id is omitted, this tool provides a high-level summary of the
        most recent topics, including thread IDs, titles, and author names.
        If a message_id is provided, it retrieves the complete details of that
        specific thread, including all historical content and nested replies.

        Args:
            limit: The maximum number of recent thread summaries to list.
            message_id: The unique ID of a thread to read in detail. Omit this
                to browse the general list of topics.

        Returns:
            A dictionary containing either 'threads' (for browsing) or
            'thread_details' (for deep reading).
        """
        if message_id:
            # Read specific thread
            self.message_board._load_data()
            thread = self.message_board.posts.get(message_id)
            if not thread:
                return {
                    "status": "fail",
                    "message": f"Thread with ID {message_id} not found.",
                }

            formatted_thread = (
                f"THREAD: {thread.title}\n"
                f"Author: {thread.author_name} [ID: {thread.message_id}]\n"
                f"Content: {thread.content}\n"
                "--- REPLIES ---"
            )

            def format_replies(replies, level=1):
                res = ""
                for r in replies:
                    indent = "  " * level
                    res += f"\n{indent}- [{r.author_name}]: {r.content} [ID: {r.message_id}]"
                    res += format_replies(r.replies, level + 1)
                return res

            formatted_thread += format_replies(thread.replies)

            return {
                "status": "success",
                "message": f"Read thread {message_id}.",
                "data": {"thread_details": formatted_thread},
            }

        # Otherwise, list recent threads
        posts = self.message_board.get_recent_posts(limit)

        # Format for agent readability (List View)
        formatted_posts = []
        for post in posts:
            thread_summary = (
                f"[ID: {post.message_id}] Title: {post.title} | Author: {post.author_name}"
                f" ({len(post.replies)} replies)"
            )
            formatted_posts.append(thread_summary)

        return {
            "status": "success",
            "message": f"Listed {len(posts)} recent threads.",
            "data": {"threads": formatted_posts},
        }

    def social_edit(self, message_id: str, new_content: str) -> dict[str, Any]:
        """Updates the content of a previously sent message.

        Allows the soul to correct mistakes or provide updates to their
        existing transmissions. This action consumes 2 Essence.

        Args:
            message_id: The unique ID of the message to modify.
            new_content: The updated text body.

        Returns:
            A dictionary status regarding the edit attempt.
        """
        cost = 2.00
        if self.essence < cost:
            log.info(
                f"{self.name} tried to edit but has insufficient essence ({self.essence:.2f} < {cost})"
            )
            return {
                "status": "fail",
                "message": f"Insufficient essence to edit. Cost: {cost}, You have: {self.essence:.2f}",
                "data": {"current_essence": self.essence},
            }

        self.essence -= cost
        success = self.message_board.edit_message(
            self.soul_id, message_id, new_content
        )
        if success:
            log.info(f"{self.name} edited message {message_id} (Cost: {cost})")
            return {
                "status": "success",
                "message": "Message edited.",
                "data": {"current_essence": self.essence},
            }

        # Refund if failed (e.g. not yours)
        self.essence += cost
        return {
            "status": "fail",
            "message": "Failed to edit. Message not found or not yours.",
        }

    def social_delete(self, message_id: str) -> dict[str, Any]:
        """Removes one of the soul's own messages from existence.

        Args:
            message_id: The unique ID of the message to erase.

        Returns:
            A dictionary confirming the removal.
        """
        success = self.message_board.delete_message(self.soul_id, message_id)
        if success:
            return {"status": "success", "message": "Message deleted."}
        return {
            "status": "fail",
            "message": "Failed to delete. message not found or not yours.",
        }

    def move_to(self, x: int, y: int) -> dict[str, Any]:
        """Initiates a physical journey to a specific screen coordinate.

        The soul will begin traveling across the user's desktop towards the
        target (x, y) point at its current movement speed.

        Args:
            x: The horizontal pixel coordinate target (0 to screen width).
            y: The vertical pixel coordinate target (0 to screen height).

        Returns:
            A dictionary acknowledging the start of the journey.
        """
        # Clamp to screen dimensions
        sw = self.physics.screen_width
        sh = self.physics.screen_height

        x = max(0, min(x, sw))
        y = max(0, min(y, sh))

        self.physics.roaming_target = (float(x), float(y))
        log.info(f"{self.name} is moving to ({x}, {y})")

        return {
            "status": "success",
            "message": f"Moving to coordinates ({x}, {y}).",
            "data": {"target": (x, y)},
        }

    def random_event(self) -> None:
        """Triggers random events based on the soul's level of needs.

        Simulates psychological effects of physical neglect, such as
        hallucinations or fatigue when satiety or hydration is critically low.
        """
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
        """Serializes the soul's current state to a portable dictionary format.

        Captures vital signs, position, and possessions for persistence.

        Returns:
            A dictionary containing the soul's serialized representation.
        """
        x, y = int(self.physics.x), int(self.physics.y)

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
        """Drives the soul's simulation and AI logic for a single frame.

        Handles physics interpolation, visual animations, biological decay,
        and triggers the periodic AI decision-making loop.

        Args:
            dt: The time elapsed since the last update frame in seconds.
        """
        self.time += dt

        # Physics Update
        self.physics.update(dt)

        # Visual Update
        angle: float = self.time * 0.5
        self.bulge_position: list[float] = [
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
                self.agent.trigger_decision(self.time)

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
            self.physics.on_mouse_drag(
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

        if self.physics:
            self.physics.on_mouse_release(x, y_top_left, button, modifiers)

    def cleanup(self) -> None:
        """Gracefully releases all system resources held by this soul."""
        log.debug(f"Cleaned up resources for soul: {self.name}")


# Resolve circular dependency for Pydantic
SoulAgent.model_rebuild()
