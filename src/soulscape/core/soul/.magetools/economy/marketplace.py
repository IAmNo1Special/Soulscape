import os
from typing import Any

from magetools import spell

from soulscape.core.interactions import Marketplace
from soulscape.system.logger import log
from soulscape.utils.helpers import action_guard


@action_guard
@spell
async def market_sell(self, item_index: int, price: float) -> dict[str, Any]:
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
    await Marketplace().initialize()
    listing_id = await Marketplace().add_listing(
        self.biology.soul_id, self.biology.name, item, price
    )

    if self.on_async_state_change:
        await self.on_async_state_change()
    elif self.on_state_change:
        self.on_state_change()

    return {
        "status": "success",
        "message": f"Listed {item.name} for {price:.2f} Essence. Listing ID: {listing_id}",
        "data": {"listing_id": listing_id},
    }


@action_guard
@spell
async def market_browse(
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
    await Marketplace().initialize()
    listings = Marketplace().filter_listings(item_name, max_price)

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


@action_guard
@spell
async def market_buy(self, listing_id: str) -> dict[str, Any]:
    """Buys an item from the marketplace using Essence.

    The soul exchanges hard-earned Essence for desired goods. This action
    instantly transfers the item to the soul's backpack and compensates
    the seller (minus a 2% marketplace tax(from seller to marketplace)).

    Args:
        listing_id: The unique ID of the market listing to purchase.

    Returns:
        A dictionary confirming the transaction and item delivery.
    """
    listing = Marketplace().get_listing(listing_id)
    if not listing:
        return {
            "status": "fail",
            "message": "Listing not found or already sold.",
        }

    # Validate Buyer != Seller (Self-Purchase Prevention)
    if listing.seller_id == self.biology.soul_id:
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
    # 1. Remove listing (atomic-ish Buy)
    if not await Marketplace().buy_listing(
        listing_id, self.biology.soul_id, self.biology.name
    ):
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

    # Add tax to marketplace fund (Only if NOT remote,
    # as Hub's buy_item already does it)
    if not os.getenv("SOULSCAPE_HUB_URL"):
        await Marketplace().add_funds(tax_amount)

    # 4. Transfer Item
    self.inventory.add_item(listing.item)

    # 5. Pay Seller
    seller = None
    if self.soul_registry:
        for s in self.soul_registry:
            if s.biology.soul_id == listing.seller_id:
                seller = s
                break

    if seller:
        seller.essence += seller_net
        seller.essence = round(seller.essence, 2)
        log.info(
            f"{self.biology.name} bought {listing.item.name} from {seller.biology.name} for {listing.price} Essence. Tax: {tax_amount}. Seller Net: {seller_net}"
        )
    else:
        log.warning(
            f"Seller {listing.seller_id} not found for payment. Essence burned."
        )

    if self.on_async_state_change:
        await self.on_async_state_change()
    elif self.on_state_change:
        self.on_state_change()

    return {
        "status": "success",
        "message": f"Bought {listing.item.name} for {listing.price} Essence. (Tax paid: {tax_amount:.2f})",
        "data": {
            "item": listing.item.to_dict(),
            "essence_left": self.essence,
        },
    }


@action_guard
@spell
async def market_cancel(self, listing_id: str) -> dict[str, Any]:
    """Removes a personal listing from the marketplace.

    Allows the soul to reclaim an item that hasn't been sold yet, returning
    it from the market stall back into its own inventory.

    Args:
        listing_id: The unique ID of the listing to reclaim.

    Returns:
        A dictionary status regarding the retrieval.
    """
    await Marketplace().initialize()
    listing = Marketplace().get_listing(listing_id)
    if not listing:
        return {"status": "fail", "message": "Listing not found."}

    # Validate Ownership
    if listing.seller_id != self.biology.soul_id:
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
    removed_listing = await Marketplace().remove_listing(listing_id)
    if not removed_listing:
        return {
            "status": "fail",
            "message": "Listing was just sold or removed.",
        }

    # Return item to inventory
    self.inventory.add_item(removed_listing.item)

    log.info(
        f"{self.biology.name} cancelled listing {listing_id} and retrieved {removed_listing.item.name}."
    )

    if self.on_async_state_change:
        await self.on_async_state_change()
    elif self.on_state_change:
        self.on_state_change()

    return {
        "status": "success",
        "message": f"Cancelled listing for {removed_listing.item.name} and retrieved item.",
        "data": {"item": removed_listing.item.to_dict()},
    }
