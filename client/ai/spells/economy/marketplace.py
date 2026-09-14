from typing import Any

from magetools import spell

from client.core.commands import (
    BuyItemCommand,
    CancelListingCommand,
    SellItemCommand,
)
from client.core.interactions import Marketplace
from client.utils.helpers import action_guard
from client.utils.security import sanitize_content


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

    # Atomic Sell: Enqueue the command and return early.
    # The actual mutation happens on the main thread.
    self.command_queue.put(SellItemCommand(item_index=item_index, price=price))

    return {
        "status": "success",
        "message": f"Queued selling item at index {item_index} for {price} essence.",
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
                "item": sanitize_content(listing.item.name, is_untrusted=True),
                "price": listing.price,
                "seller": sanitize_content(
                    listing.seller_name, is_untrusted=True
                ),
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
    await Marketplace().initialize()
    # Atomic Buy: Enqueue the command and return early.
    self.command_queue.put(BuyItemCommand(listing_id=listing_id))

    return {
        "status": "success",
        "message": f"Queued buying listing {listing_id}.",
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

    # Atomic Cancel: Enqueue the command and return early.
    self.command_queue.put(CancelListingCommand(listing_id=listing_id))

    return {
        "status": "success",
        "message": f"Queued cancelling listing {listing_id}.",
    }
