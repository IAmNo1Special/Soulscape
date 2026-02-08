"""Inventory and Item system for Souls.

This module defines the base classes for items and consumables,
and an Inventory class to manage a collection of items.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from soulscape.core.soul import Soul


class Item(ABC):
    """Abstract base class for all items.

    Attributes:
        name: Human-readable name of the item.
        description: Description of the item.
    """

    def __init__(self, name: str, description: str):
        """Initializes an Item.

        Args:
            name: Human-readable name of the item.
            description: Description of the item.
        """
        self.name = name
        self.description = description

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """Serializes the item for persistence.

        Returns:
            A dictionary containing item data.
        """
        pass

    @abstractmethod
    def __repr__(self) -> str:
        """String representation of the item."""
        pass

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Item:
        """Deserializes an item from a dictionary.

        Args:
            data: Serialization dictionary.

        Returns:
            An instance of the specific Item subclass.
        """
        return item_from_dict(data)


class Consumable(Item):
    """Abstract base class for items that can be consumed (food, drink)."""

    def __init__(self, name: str, description: str, value: int):
        """Initializes a Consumable.

        Args:
            name: Name of the consumable.
            description: Description.
            value: The recovery value (e.g., satiety or hydration points).
        """
        super().__init__(name, description)
        self.value = value

    @abstractmethod
    def consume(self, soul: Soul) -> str:
        """Consumes the item and applies its effect to the soul.

        Args:
            soul: The Soul instance consuming the item.

        Returns:
            A message describing the result of consumption.
        """
        pass

    def to_dict(self) -> dict[str, Any]:
        """Serializes the consumable.

        Returns:
            A dictionary containing type, name, description, and value.
        """
        return {
            "type": self.__class__.__name__,
            "name": self.name,
            "description": self.description,
            "value": self.value,
        }

    def __repr__(self) -> str:
        return f"{self.name} ({self.value} pts)"


class Food(Consumable):
    """An item that recovers satiety."""

    def consume(self, soul: Soul) -> str:
        """Consumes the food.

        Args:
            soul: The Soul instance.

        Returns:
            Message describing the effect.
        """
        soul.satiety += self.value
        if soul.satiety > 100:
            soul.satiety = 100
        return f"{soul.name} ate {self.name} and is now {soul.satiety}% full."


class Drink(Consumable):
    """An item that recovers hydration."""

    def consume(self, soul: Soul) -> str:
        """Consumes the drink.

        Args:
            soul: The Soul instance.

        Returns:
            Message describing the effect.
        """
        soul.hydration += self.value
        if soul.hydration > 100:
            soul.hydration = 100
        return (
            f"{soul.name} drank {self.name} and is now "
            f"{soul.hydration}% hydrated."
        )


def item_from_dict(data: dict[str, Any]) -> Item:
    """Factory function to create an Item from a dictionary.

    Args:
        data: Serialization dictionary with 'type', 'name', 'description', 'value'.

    Returns:
        An instance of Food or Drink.

    Raises:
        ValueError: If 'type' is unknown.
    """
    item_type = data.get("type")
    name = data.get("name", "Unknown Item")
    desc = data.get("description", "")
    val = data.get("value", 0)

    if item_type == "Food":
        return Food(name, desc, val)
    elif item_type == "Drink":
        return Drink(name, desc, val)
    else:
        raise ValueError(f"Unknown item type: {item_type}")


class Inventory:
    """Manages a collection of Items for a Soul.

    Attributes:
        items: List of items currently in inventory.
        capacity: Maximum number of items allowed.
    """

    def __init__(self, capacity: int = 10):
        """Initializes the inventory.

        Args:
            capacity: Maximum number of items. Defaults to 10.
        """
        self.items: list[Item] = []
        self.capacity = capacity

    def add_item(self, item: Item) -> bool:
        """Adds an item to the inventory.

        Args:
            item: The item to add.

        Returns:
            True if successful, False if inventory is full.
        """
        if len(self.items) < self.capacity:
            self.items.append(item)
            return True
        return False

    def remove_item(self, item: Item) -> bool:
        """Removes an item from the inventory.

        Args:
            item: The item to remove.

        Returns:
            True if removed, False if not found.
        """
        if item in self.items:
            self.items.remove(item)
            return True
        return False

    def get_consumables(
        self, consumable_type: type[Consumable]
    ) -> list[Consumable]:
        """Returns a list of consumables of a specific type.

        Args:
            consumable_type: The class (Food or Drink) to filter by.

        Returns:
            List of matching consumables.
        """
        return [i for i in self.items if isinstance(i, consumable_type)]

    def to_dict(self) -> dict[str, Any]:
        """Serializes the inventory.

        Returns:
            A dictionary containing capacity and serialized items.
        """
        return {
            "capacity": self.capacity,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Inventory:
        """Reconstructs an inventory from a dictionary.

        Args:
            data: Serialization dictionary.

        Returns:
            A new Inventory instance.
        """
        capacity = data.get("capacity", 10)
        inventory = cls(capacity=capacity)
        for item_data in data.get("items", []):
            try:
                item = item_from_dict(item_data)
                inventory.add_item(item)
            except ValueError:
                # Skip unknown item types
                continue
        return inventory

    def __repr__(self) -> str:
        return f"Inventory ({len(self.items)}/{self.capacity}): {self.items}"
