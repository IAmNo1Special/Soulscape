"""Global Marketplace system for Soulscape.

This module defines the Marketplace class (Singleton) and the MarketListing data structure.
It manages the global registry of items for sale, allowing souls to list and buy items
asynchronously.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from soulscape.system.logger import log

if TYPE_CHECKING:
    from ..interactions.inventory import Item
    from ..stores import DataStore


@dataclass
class MarketListing:
    """Represents an item listed for sale in the marketplace."""

    listing_id: str
    seller_id: int
    seller_name: str
    item: Item
    price: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serializes the listing to a dictionary.

        Returns:
            A dictionary containing listing details.
        """
        return {
            "listing_id": self.listing_id,
            "seller_id": self.seller_id,
            "seller_name": self.seller_name,
            "item": self.item.to_dict(),
            "price": self.price,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarketListing:
        """Deserializes a listing from a dictionary.

        Args:
            data: Serialization dictionary.

        Returns:
            A new MarketListing instance.
        """
        from .inventory import Item  # Local import to avoid circular dep

        item = Item.from_dict(data["item"])
        return cls(
            listing_id=data["listing_id"],
            seller_id=data["seller_id"],
            seller_name=data["seller_name"],
            item=item,
            price=data["price"],
            timestamp=data["timestamp"],
        )


class Marketplace:
    """Global registry of active sell offers (Singleton pattern)."""

    _instance = None

    def __new__(cls, store: DataStore | None = None) -> Marketplace:
        """Creates or returns the singleton instance."""
        if cls._instance is None:
            cls._instance = super(Marketplace, cls).__new__(cls)
            cls._instance.listings = {}  # type: dict[str, MarketListing]
            cls._instance.essence_fund = 0.0  # Accumulated tax

            # Default to LocalStore if none provided
            if store is None:
                from ..stores import get_default_store

                store = get_default_store()
            cls._instance.store = store
            # Need to call initialize() to load data
            cls._instance.initialized = False
        return cls._instance

    async def initialize(self) -> None:
        """Asynchronously loads initial data if not already done.

        Use this for the first load. For subsequent forced reloads,
        use refresh().
        """
        if not self.initialized:
            await self._load_data()
            self.initialized = True

    async def refresh(self) -> None:
        """Forces a reload of data from the store."""
        await self._load_data()
        self.initialized = True

    async def _load_data(self) -> None:
        """Loads listings and essence fund from the data store asynchronously."""
        try:
            data = await self.store.load_marketplace()
            if not data:
                return

            # Load Essence Fund
            try:
                self.essence_fund = float(data.get("essence_fund", 0.0))
            except (ValueError, TypeError):
                log.warning(
                    "Invalid essence_fund in data store. Defaulting to 0.0"
                )
                self.essence_fund = 0.0

            # Load Listings
            listings_data = data.get("listings", [])

            for listing_data in listings_data:
                try:
                    listing = MarketListing.from_dict(listing_data)
                    self.listings[listing.listing_id] = listing
                except Exception as e:
                    log.error(f"Failed to load listing: {e}")

            log.info(
                f"Loaded {len(self.listings)} active listings. Market Fund: {self.essence_fund:.2f}"
            )
        except Exception as e:
            log.error(f"Failed to load marketplace data from store: {e}")

    async def _save_data(self) -> None:
        """Saves active listings and essence fund to the data store asynchronously."""
        try:
            data = {
                "essence_fund": self.essence_fund,
                "listings": [
                    listing.to_dict() for listing in self.listings.values()
                ],
            }
            await self.store.save_marketplace(data)
        except Exception as e:
            log.error(f"Failed to save marketplace data to store: {e}")

    async def add_funds(self, amount: float) -> None:
        """Adds essence to the market fund (e.g. from taxes)."""
        self.essence_fund += amount
        # Granular Hub update
        await self.store.update_funds(amount)
        await self._save_data()

    async def add_listing(
        self,
        seller_id: int,
        seller_name: str,
        item: Item,
        price: float,
        token: str | None = None,
    ) -> str:
        """Creates a new listing and adds it to the marketplace.

        Args:
            seller_id: The ID of the soul selling the item.
            seller_name: The name of the soul (for display).
            item: The item object being sold.
            price: The cost in Essence.

        Returns:
            The unique listing_id as a string.
        """
        listing_id = str(uuid.uuid4())[
            :8
        ]  # Short UUID for easier typing/display
        listing = MarketListing(
            listing_id=listing_id,
            seller_id=seller_id,
            seller_name=seller_name,
            item=item,
            price=price,
        )
        self.listings[listing_id] = listing
        # Granular Hub update
        await self.store.add_listing(listing.to_dict(), token=token)
        # For local fallback, we still usually save everything
        await self._save_data()
        return listing_id

    async def remove_listing(
        self, listing_id: str, token: str | None = None
    ) -> MarketListing | None:
        """Removes a listing from the marketplace (Cancellation)."""
        listing = self.listings.pop(listing_id, None)
        if listing:
            # Granular Hub update (pure delete)
            await self.store.delete_listing(listing_id, token=token)
            await self._save_data()
        return listing

    async def buy_listing(
        self,
        listing_id: str,
        buyer_id: int,
        buyer_name: str,
        token: str | None = None,
    ) -> MarketListing | None:
        """Executes a purchase of a listing (Buying)."""
        listing = self.listings.pop(listing_id, None)
        if listing:
            # Granular Hub update (trigger tax)
            await self.store.buy_listing(
                listing_id,
                {"buyer_id": buyer_id, "buyer_name": buyer_name},
                token=token,
            )
            await self._save_data()
        return listing

    def get_listing(self, listing_id: str) -> MarketListing | None:
        """Retrieves a specific listing by ID.

        Args:
            listing_id: The listing ID.

        Returns:
            The MarketListing if found, otherwise None.
        """
        return self.listings.get(listing_id)

    def get_all_listings(self) -> list[MarketListing]:
        """Returns all active listings.

        Returns:
            A list of all active MarketListing objects.
        """
        return list(self.listings.values())

    def filter_listings(
        self, item_name: str | None = None, max_price: float | None = None
    ) -> list[MarketListing]:
        """Returns listings matching specific criteria.

        Args:
            item_name: Optional substring to match in item name.
            max_price: Optional maximum price filter.

        Returns:
            A list of matching MarketListing objects.
        """
        results = []
        for listing in self.listings.values():
            if item_name and item_name.lower() not in listing.item.name.lower():
                continue
            if max_price is not None and listing.price > max_price:
                continue
            results.append(listing)
        return results
