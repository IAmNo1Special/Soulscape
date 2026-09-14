from __future__ import annotations

import os
import random
from typing import TYPE_CHECKING, Any

from goapauto.models.actions import Increment, Set
from goapauto.models.goal import Goal
from goapauto.models.goal_arbitrator import GoalArbitrator
from goapauto.models.goap_planner import Planner
from goapauto.models.sensors import Sensor, SensorManager
from goapauto.models.worldstate import WorldState

from ...system.logger import log
from ..commands import DrinkCommand, EatCommand, InventoryCommand
from ..interactions import Drink, Food

if TYPE_CHECKING:
    from .soul import Soul

SATIETY_TARGET = 50.0
HYDRATION_TARGET = 50.0
FORAGE_SUCCESS_RATE = 0.25
FORAGE_VALUE_MIN = 10
FORAGE_VALUE_MAX = 30


class NeedsSensor(Sensor):
    def __init__(self, soul: Soul):
        self._soul = soul

    def sense(self) -> dict[str, Any]:
        soul = self._soul
        items = soul.inventory.items
        return {
            "satiety": float(soul.biology.satiety),
            "hydration": float(soul.biology.hydration),
            "hp": float(soul.biology.current_health),
            "has_food": any(isinstance(i, Food) for i in items),
            "has_drink": any(isinstance(i, Drink) for i in items),
            "inventory_full": len(items) >= soul.inventory.capacity,
        }


def build_goals() -> list[Goal]:
    from goapauto.models.actions import GreaterThan

    return [
        Goal(
            name="satiated",
            target_state={"satiety": GreaterThan(SATIETY_TARGET)},
            priority=1,
        ),
        Goal(
            name="hydrated",
            target_state={"hydration": GreaterThan(HYDRATION_TARGET)},
            priority=2,
        ),
    ]


def build_actions() -> list[tuple[str, dict[str, Any], dict[str, Any], float]]:
    return [
        (
            "eat",
            {"has_food": True},
            {"has_food": Set(False), "satiety": Increment(15)},
            1.0,
        ),
        (
            "drink",
            {"has_drink": True},
            {"has_drink": Set(False), "hydration": Increment(15)},
            1.0,
        ),
        (
            "find_food",
            {"has_food": False, "inventory_full": False},
            {"has_food": Set(True)},
            4.0,
        ),
        (
            "find_water",
            {"has_drink": False, "inventory_full": False},
            {"has_drink": Set(True)},
            4.0,
        ),
    ]


def _do_eat(soul: Soul) -> None:
    foods = soul.inventory.get_consumables(Food)
    if foods:
        soul.command_queue.put(EatCommand(item=foods[0]))
        log.info(f"{soul.biology.name} eats {foods[0].name}.")


def _do_drink(soul: Soul) -> None:
    drinks = soul.inventory.get_consumables(Drink)
    if drinks:
        soul.command_queue.put(DrinkCommand(item=drinks[0]))
        log.info(f"{soul.biology.name} drinks {drinks[0].name}.")


def _do_find_food(soul: Soul) -> None:
    if random.random() > FORAGE_SUCCESS_RATE:
        log.info(f"{soul.biology.name} searched for food but found nothing.")
        return
    value = random.randint(FORAGE_VALUE_MIN, FORAGE_VALUE_MAX)
    item = Food("Wild Berries", "Found in the wild.", value)
    soul.command_queue.put(InventoryCommand(action="add", item=item))
    log.info(f"{soul.biology.name} found {item.name} ({value} food value).")


def _do_find_water(soul: Soul) -> None:
    if random.random() > FORAGE_SUCCESS_RATE:
        log.info(f"{soul.biology.name} searched for water but found nothing.")
        return
    value = random.randint(FORAGE_VALUE_MIN, FORAGE_VALUE_MAX)
    item = Drink("Water Bottle", "Collected from a stream.", value)
    soul.command_queue.put(InventoryCommand(action="add", item=item))
    log.info(f"{soul.biology.name} found {item.name} ({value} water value).")


EXECUTORS = {
    "eat": _do_eat,
    "drink": _do_drink,
    "find_food": _do_find_food,
    "find_water": _do_find_water,
}


class GoapBrain:
    def __init__(self, soul: Soul):
        self._soul = soul
        self.sensors = SensorManager([NeedsSensor(soul)])
        self.arbitrator = GoalArbitrator(build_goals())
        self.planner = Planner(actions_list=build_actions(), verbose=False)
        self.is_busy = False
        self.decision_interval = float(os.getenv("SOUL_BRAIN_INTERVAL", "5.0"))
        self.last_decision_time = -self.decision_interval
        self.last_goal: str | None = None
        self.last_plan: list[str] | None = None

    def trigger_decision(
        self,
        current_time: float,
        snapshot: dict[str, Any] | None = None,
        sensations: list[str] | None = None,
    ) -> str:
        try:
            state = WorldState()
            self.sensors.update_state(state)
            goal = self.arbitrator.select_goal(state)
            if goal is None:
                self.last_goal = None
                self.last_plan = []
                self.last_decision_time = current_time
                return "idle"
            result = self.planner.generate_plan(state, goal, max_depth=6)
            if not result.plan:
                self.last_decision_time = current_time
                return "no-plan"
            self.last_goal = goal.name
            self.last_plan = list(result.plan)
            executor = EXECUTORS.get(result.plan[0])
            if executor is not None:
                executor(self._soul)
            self.last_decision_time = current_time
            return result.plan[0]
        except Exception as e:
            log.error(f"GoapBrain decision failed: {e}")
            self.last_decision_time = current_time
            return "error"

    def stop(self) -> None:
        return None
