import random
from typing import Any

from magetools import spell

from soulscape.core import Drink
from soulscape.system.logger import log
from soulscape.utils.helpers import action_guard


@action_guard
@spell
async def find_water(self) -> dict[str, Any]:
    """Searches for a clean source of water to fill a bottle.

    There is a 25% chance to find water. If found, the soul secures the
    bounty in its backpack for later consumption.

    Returns:
        A dictionary detailing the search results.
    """
    success_rate = 0.25  # 25% chance to find water.
    if random.random() > success_rate:
        log.info(f"{self.biology.name} searched for water but found nothing.")
        return {
            "status": "fail",
            "message": "You searched for water but found nothing.",
            "data": {
                "hydration_value": 0,
                "current_hydration": self.biology.hydration,
            },
        }

    water_value = random.randint(10, 30)
    new_drink = Drink("Water Bottle", "Collected from a stream.", water_value)

    if self.inventory.add_item(new_drink):
        log.info(
            f"{self.biology.name} found {new_drink.name} ({water_value} water value)!"
        )
        if self.on_state_change:
            self.on_state_change()
        return {
            "status": "success",
            "message": f"You found {new_drink.name} ({water_value} water value)!",
            "data": {
                "hydration_value": water_value,
                "current_hydration": self.biology.hydration,
            },
        }

    log.info("You found water but inventory is full!")
    return {
        "status": "fail",
        "message": "You found water but inventory is full!",
        "data": {
            "hydration_value": 0,
            "current_hydration": self.biology.hydration,
        },
    }


@action_guard
@spell
async def drink(self) -> dict[str, Any]:
    """Consumes a drink from the inventory to quench thirst.

    The soul searches for a liquid refreshment in its backpack. Drinking
    instantly restores hydration levels and clears the inventory slot.

    Returns:
        A dictionary with status feedback and new hydration levels.
    """
    drink_items = self.inventory.get_consumables(Drink)
    if not drink_items:
        log.warning("%s tried to drink but has no water!", self.biology.name)
        return {
            "status": "fail",
            "message": "No water in inventory!",
            "data": {"current_hydration": self.biology.hydration},
        }

    # Consume the first drink item.
    item = drink_items[0]
    value = item.value
    result_msg = item.consume(self)
    self.inventory.remove_item(item)
    log.info(result_msg)
    if self.on_async_state_change:
        await self.on_async_state_change()
    elif self.on_state_change:
        self.on_state_change()
    return {
        "status": "success",
        "message": result_msg,
        "data": {
            "hydration_value": value,
            "current_hydration": self.biology.hydration,
        },
    }
