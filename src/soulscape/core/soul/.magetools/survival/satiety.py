import random
from typing import Any

from magetools import spell

from soulscape.core import Food
from soulscape.core.commands import EatCommand, InventoryCommand
from soulscape.system.logger import log
from soulscape.utils.helpers import action_guard


@action_guard
@spell
async def find_food(self) -> dict[str, Any]:
    """Scours the immediate surroundings for sustenance.

    There is a 25% chance to find edibles. If found, the soul secures the
    bounty in its backpack for later consumption.

    Returns:
        A dictionary detailing the search outcome and any items found.
    """
    success_rate = 0.25  # 25% chance to find food.
    if random.random() > success_rate:
        log.info(f"{self.biology.name} searched for food but found nothing.")
        return {
            "status": "fail",
            "message": "You searched for food but found nothing.",
            "data": {
                "satiety_value": 0,
                "current_satiety": self.biology.satiety,
            },
        }

    food_value = random.randint(10, 30)
    new_food = Food("Wild Berries", "Found in the wild.", food_value)

    if len(self.inventory.items) < self.inventory.capacity:
        # We queue the addition for execution on the main thread.
        self.command_queue.put(InventoryCommand(action="add", item=new_food))
        log.info(
            f"{self.biology.name} found {new_food.name} ({food_value} food value)!"
        )
        return {
            "status": "success",
            "message": f"You found {new_food.name} ({food_value} food value)!",
            "data": {
                "satiety_value": food_value,
                "current_satiety": self.biology.satiety,
            },
        }

    log.info("You found food but inventory is full!")
    return {
        "status": "fail",
        "message": "You found food but inventory is full!",
        "data": {
            "satiety_value": 0,
            "current_satiety": self.biology.satiety,
        },
    }


@action_guard
@spell
async def eat(self) -> dict[str, Any]:
    """Consumes a food item from the inventory to sate hunger.

    This tool searches the backpack for any available food source. If found,
    it replenishes the soul's energy and removes the item from inventory.

    Returns:
        A dictionary containing the success status and current satiety data.
    """
    food_items = self.inventory.get_consumables(Food)
    if not food_items:
        # log.warning(f"{self.biology.name} tried to eat but has no food!")
        return {
            "status": "fail",
            "message": "No food in inventory!",
            "data": {"current_satiety": self.biology.satiety},
        }

    # Consume the first food item.
    item = food_items[0]
    value = item.value
    # Atomic Eat: Enqueue the command and return early.
    self.command_queue.put(EatCommand(item=item))

    result_msg = f"You ate {item.name} and recovered {value} satiety."
    log.info(result_msg)

    return {
        "status": "success",
        "message": result_msg,
        "data": {
            "satiety_value": value,
            "current_satiety": self.biology.satiety,
        },
    }
