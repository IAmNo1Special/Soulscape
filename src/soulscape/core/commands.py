import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from soulscape.system.command_queue import Command

log = logging.getLogger("soulscape.commands")


@dataclass
class MoveCommand(Command):
    """Command to move a soul to a target location."""

    x: float = 0.0
    y: float = 0.0

    def execute(self, soul: Any) -> None:
        if hasattr(soul, "physics") and soul.physics:
            soul.physics.target_location = (float(self.x), float(self.y))
            log.debug(
                f"Applied MoveCommand to {soul.biology.name}: ({self.x}, {self.y})"
            )


@dataclass
class SpeakCommand(Command):
    """Command to make a soul speak (e.g. log or UI update)."""

    message: str = ""

    def execute(self, soul: Any) -> None:
        log.info(f"[SOUL SPEAK] {soul.biology.name}: {self.message}")


@dataclass
class InventoryCommand(Command):
    """Command to modify inventory."""

    action: str = "add"  # add, remove
    item: Any = None

    def execute(self, soul: Any) -> None:
        if not hasattr(soul, "inventory"):
            log.warning(f"Soul {soul.biology.name} has no inventory.")
            return

        if self.action == "add" and self.item:
            soul.inventory.add_item(self.item)
            log.debug(
                f"Applied InventoryCommand (add) to {soul.biology.name}: {self.item.name}"
            )
        elif self.action == "remove" and self.item:
            soul.inventory.remove_item(self.item)
            log.debug(
                f"Applied InventoryCommand (remove) to {soul.biology.name}: {self.item.name}"
            )
        else:
            log.warning(
                f"Unknown inventory action or missing item: {self.action}"
            )


@dataclass
class VitalCommand(Command):
    """Command to modify soul biological vitals (satiety, hydration, hp)."""

    vital_type: str = "satiety"  # satiety, hydration, hp
    amount: float = 0.0

    def execute(self, soul: Any) -> None:
        if self.vital_type == "satiety":
            soul.biology.satiety += self.amount
            soul.biology.satiety = max(0, min(100, soul.biology.satiety))
        elif self.vital_type == "hydration":
            soul.biology.hydration += self.amount
            soul.biology.hydration = max(0, min(100, soul.biology.hydration))
        elif self.vital_type == "hp":
            soul.biology.current_health += self.amount
            soul.biology.current_health = max(
                0, min(soul.biology.stats.max_hp, soul.biology.current_health)
            )

        log.debug(
            f"Applied VitalCommand ({self.vital_type}) to {soul.biology.name}: {self.amount:+}"
        )


@dataclass
class EssenceCommand(Command):
    """Command to modify soul essence."""

    amount: float = 0.0

    def execute(self, soul: Any) -> None:
        soul.essence += self.amount
        soul.essence = round(soul.essence, 2)
        log.debug(
            f"Applied EssenceCommand to {soul.biology.name}: {self.amount:+}"
        )


@dataclass
class StateUpdateCommand(Command):
    """Command to update remote soul references from Network."""

    owner_id: str = ""
    souls_data: List[Dict[str, Any]] = field(default_factory=list)
    priority: int = 5

    def execute(self, soul: Any) -> None:
        """Updates remote souls in the registry."""
        if not hasattr(soul, "soul_registry"):
            return

        for data in self.souls_data:
            sid = data.get("soul_id")
            if not sid:
                continue

            # Find in registry
            target = next(
                (s for s in soul.soul_registry if s.biology.soul_id == sid),
                None,
            )
            if target and target.owner_id == self.owner_id:
                target.update_from_dict(data)


@dataclass
class OwnerPresenceCommand(Command):
    """Command to handle owner presence changes."""

    owner_id: str = ""
    action: str = "online"  # online, offline
    priority: int = 10  # High priority

    def execute(self, soul: Any) -> None:
        # This logic is usually handled at the App level.
        # However, if we process commands via a Soul, we might need to
        # reach out to the app or registry.
        log.info(f"Owner {self.owner_id} is now {self.action}")
        if self.action == "offline" and hasattr(soul, "soul_registry"):
            # Remove souls for offline owner
            soul.soul_registry[:] = [
                s for s in soul.soul_registry if s.owner_id != self.owner_id
            ]
