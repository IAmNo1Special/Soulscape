from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal

from soulscape.system.command_queue import Command
from soulscape.system.logger import log


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

    action: Literal["add", "remove"] = "add"
    item: Any = None

    def execute(self, soul: Any) -> None:
        if not hasattr(soul, "inventory"):
            log.warning(f"Soul {soul.biology.name} has no inventory.")
            return

        if self.action == "add" and self.item:
            # Check capacity atomically on the main thread
            capacity = getattr(soul.inventory, "capacity", 100)
            if len(soul.inventory.items) < capacity:
                soul.inventory.add_item(self.item)
                log.debug(
                    f"Applied InventoryCommand (add) to {soul.biology.name}: {self.item.name}"
                )
            else:
                log.warning(
                    f"Applied InventoryCommand (add) FAILED: Inventory full for {soul.biology.name}"
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

    vital_type: Literal["satiety", "hydration", "hp"] = "satiety"
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
        """Updates remote souls in the registry.

        Includes spoofing protection to prevent remote updates from
        overwriting the local soul's state.
        """
        if not hasattr(soul, "soul_registry"):
            return

        # Security: The hub should never be updating the local soul's state
        # in a way that overwrites authoritative local physics/biology.
        # Local soul is defined by having local_instance_id matching owner_id.
        local_id = getattr(soul, "local_instance_id", None)

        for data in self.souls_data:
            sid = data.get("soul_id")
            if not sid:
                continue

            # Spoofing Protection: Skip if this data targets the local soul
            if local_id and sid == local_id:
                log.warning(f"Prevented spoofing attempt for local soul: {sid}")
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
    action: Literal["online", "offline"] = "online"
    priority: int = 10  # High priority

    def execute(self, soul: Any) -> None:
        # This logic is usually handled at the App level.
        # However, if we process commands via a Soul, we might need to
        # reach out to the app or registry.
        log.info(f"Owner {self.owner_id} is now {self.action}")
        if self.action == "offline" and hasattr(soul, "soul_registry"):
            soul.soul_registry[:] = [
                s for s in soul.soul_registry if s.owner_id != self.owner_id
            ]


@dataclass
class SellItemCommand(Command):
    """Atomic command to sell an item on the marketplace."""

    item_index: int = 0
    price: float = 0.0

    def execute(self, soul: Any) -> None:
        if not hasattr(soul, "inventory") or not hasattr(soul, "marketplace"):
            return

        if self.item_index < 0 or self.item_index >= len(soul.inventory.items):
            log.warning(
                f"Atomic Sell failed: Index {self.item_index} out of range"
            )
            return

        # Optimistic local update (Main Thread)
        item = soul.inventory.items.pop(self.item_index)

        async def _do_sell():
            try:
                await soul.marketplace.add_listing(
                    soul.soul_id, soul.biology.name, item, self.price
                )
                log.info(f"Marketplace listing confirmed: {item.name}")
            except Exception as e:
                log.error(f"Marketplace listing failed for {item.name}: {e}")
                # Rollback
                soul.command_queue.put(
                    InventoryCommand(action="add", item=item)
                )

        if hasattr(soul, "schedule_task"):
            soul.schedule_task(_do_sell())
            log.info(f"Atomic Sell initiated: {item.name} for {self.price}")
        else:
            log.error("Sell failed: Soul has no task scheduler.")
            # Immediate rollback if no scheduler (e.g. strict test environment)
            soul.inventory.items.insert(self.item_index, item)


@dataclass
class BuyItemCommand(Command):
    """Atomic command to buy an item from the marketplace."""

    listing_id: str = ""

    def execute(self, soul: Any) -> None:
        if not hasattr(soul, "marketplace") or not hasattr(soul, "inventory"):
            log.warning(
                f"Atomic Buy failed: Soul {soul.biology.name} lacks marketplace or inventory."
            )
            return

        listing = soul.marketplace.get_listing(self.listing_id)
        if not listing:
            log.warning(
                f"Atomic Buy failed for {soul.biology.name}: Listing {self.listing_id} not found"
            )
            return

        if soul.essence < listing.price:
            log.warning(
                f"Atomic Buy failed for {soul.biology.name}: Not enough essence for {listing.item.name} (needed {listing.price}, has {soul.essence})"
            )
            return

        # Perform atomic transfer via Hub asynchronously
        async def _do_buy():
            try:
                # Essence is deducted by Hub upon successful buy_listing
                listing = await soul.marketplace.buy_listing(
                    self.listing_id, soul.soul_id, soul.biology.name
                )
                if listing and listing.item:
                    soul.command_queue.put(
                        InventoryCommand(action="add", item=listing.item)
                    )
                    log.info(
                        f"Atomic Buy success for {soul.biology.name}: {listing.item.name} purchased"
                    )
                else:
                    log.warning(
                        f"Atomic Buy failed for {soul.biology.name}: Listing {self.listing_id} disappeared."
                    )
            except Exception as e:
                log.error(f"Atomic Buy error for {soul.biology.name}: {e}")

        if hasattr(soul, "schedule_task"):
            soul.schedule_task(_do_buy())
        else:
            log.error("Buy failed: Soul has no task scheduler.")


@dataclass
class CancelListingCommand(Command):
    """Atomic command to cancel a marketplace listing."""

    listing_id: str = ""

    def execute(self, soul: Any) -> None:
        if not hasattr(soul, "marketplace") or not hasattr(soul, "inventory"):
            return

        listing = soul.marketplace.get_listing(self.listing_id)
        if not listing:
            return

        # Security: Only the seller can cancel
        if listing.seller_id != soul.soul_id:
            log.warning(
                f"Unauthorized Cancel attempt by {soul.soul_id} for listing {self.listing_id} (Seller: {listing.seller_id})"
            )
            return

        async def _do_cancel():
            try:
                listing = await soul.marketplace.remove_listing(self.listing_id)
                if listing and listing.item:
                    soul.command_queue.put(
                        InventoryCommand(action="add", item=listing.item)
                    )
                    log.info(f"Atomic Cancel success: {listing.item.name}")
            except Exception as e:
                log.error(f"Atomic Cancel error: {e}")

        if hasattr(soul, "schedule_task"):
            soul.schedule_task(_do_cancel())
        else:
            log.error("Cancel failed: Soul has no task scheduler.")


@dataclass
class EatCommand(Command):
    """Atomic command to eat a food item."""

    item: Any = None

    def execute(self, soul: Any) -> None:
        if not hasattr(soul, "inventory") or not hasattr(soul, "biology"):
            return
        if self.item and self.item in soul.inventory.items:
            soul.inventory.items.remove(self.item)
            value = getattr(self.item, "value", 0)
            soul.biology.satiety += value
            soul.biology.satiety = max(0, min(100, soul.biology.satiety))
            log.info(
                f"Atomic Eat success: {soul.biology.name} ate {self.item.name} (+{value} satiety)"
            )


@dataclass
class DrinkCommand(Command):
    """Atomic command to drink a liquid."""

    item: Any = None

    def execute(self, soul: Any) -> None:
        if not hasattr(soul, "inventory") or not hasattr(soul, "biology"):
            return
        if self.item and self.item in soul.inventory.items:
            soul.inventory.items.remove(self.item)
            value = getattr(self.item, "value", 0)
            soul.biology.hydration += value
            soul.biology.hydration = max(0, min(100, soul.biology.hydration))
            log.info(
                f"Atomic Drink success: {soul.biology.name} drank {self.item.name} (+{value} hydration)"
            )


@dataclass
class PresenceReconcileCommand(Command):
    """Command to reconcile the local registry with online users."""

    online_owners: List[str] = field(default_factory=list)
    priority: int = 15  # Very high priority for consistency

    def execute(self, context: Any) -> None:
        """Reconciles souls in the registry or app level."""
        # This command can be executed on a Soul (registry) or the App (active_souls)
        registry = []
        if hasattr(context, "soul_registry"):
            registry = context.soul_registry
        elif hasattr(context, "active_souls"):
            registry = context.active_souls

        if not registry:
            return

        online_set = set(self.online_owners)
        initial_count = len(registry)

        # Prune remote souls whose owners are no longer online
        # We keep local souls (where owner_id == instance_id)
        instance_id = getattr(context, "instance_id", None)
        if not instance_id and hasattr(context, "biology"):
            # If context is a Soul, we might need a way to identify 'local'
            # For now, we assume StateUpdateCommand logic where local_instance_id is set
            instance_id = getattr(context, "local_instance_id", None)

        if hasattr(registry, "remove"):  # If it's a list we can mutate
            to_keep = [
                s
                for s in registry
                if not (
                    (owner := getattr(s, "owner_id", None))
                    and owner != instance_id
                    and owner not in online_set
                )
            ]
            pruned_count = len(registry) - len(to_keep)
            if pruned_count > 0:
                registry[:] = to_keep
                log.info(
                    f"PresenceReconcile pruned {pruned_count} stale souls (from {initial_count})."
                )
