"""Global Marketplace system for Soulscape.

This module defines the Marketplace class (Singleton) and the MarketListing data structure.
It manages the global registry of items for sale, allowing souls to list and buy items
asynchronously.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from soulscape.system.logger import log

if TYPE_CHECKING:
    from soulscape.core.items import Item


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
        """Serializes the listing to a dictionary."""
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
        """Deserializes a listing from a dictionary."""
        from soulscape.core.items import (
            Item,
        )  # Local import to avoid circular dep

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

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Marketplace, cls).__new__(cls)
            cls._instance.listings = {}  # type: dict[str, MarketListing]
            cls._instance.essence_fund = 0.0  # Accumulated tax
            cls._instance.db_path = os.path.join(
                os.getcwd(), "data", "marketplace.json"
            )
            cls._instance._load_data()
        return cls._instance

    def _load_data(self):
        """Loads listings and essence fund from JSON file."""
        if not os.path.exists(self.db_path):
            return

        try:
            with open(self.db_path, "r") as f:
                data = json.load(f)

                # Load Essence Fund (handle legacy files)
                self.essence_fund = float(data.get("essence_fund", 0.0))

                # Load Listings
                listings_data = (
                    data.get("listings", []) if isinstance(data, dict) else data
                )
                # Support old format where root was list
                if isinstance(data, list):
                    listings_data = data

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
            log.error(f"Failed to load marketplace data: {e}")

    def _save_data(self):
        """Saves active listings and essence fund to JSON file."""
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            data = {
                "essence_fund": self.essence_fund,
                "listings": [
                    listing.to_dict() for listing in self.listings.values()
                ],
            }
            with open(self.db_path, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            log.error(f"Failed to save marketplace data: {e}")

    def add_funds(self, amount: float):
        """Adds essence to the market fund (e.g. from taxes)."""
        self.essence_fund += amount
        self._save_data()

    def add_listing(
        self, seller_id: int, seller_name: str, item: Item, price: float
    ) -> str:
        """Creates a new listing and adds it to the marketplace.

        Args:
            seller_id: The ID of the soul selling the item.
            seller_name: The name of the soul (for display).
            item: The item object being sold.
            price: The cost in Essence.

        Returns:
            The unique listing_id.
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
        self._save_data()
        return listing_id

    def remove_listing(self, listing_id: str) -> MarketListing | None:
        """Removes a listing from the marketplace (e.g., bought or cancelled)."""
        listing = self.listings.pop(listing_id, None)
        if listing:
            self._save_data()
        return listing

    def get_listing(self, listing_id: str) -> MarketListing | None:
        """Retrieves a specific listing by ID."""
        return self.listings.get(listing_id)

    def get_all_listings(self) -> list[MarketListing]:
        """Returns all active listings."""
        return list(self.listings.values())

    def filter_listings(
        self, item_name: str | None = None, max_price: int | None = None
    ) -> list[MarketListing]:
        """Returns listings matching specific criteria."""
        results = []
        for listing in self.listings.values():
            if item_name and item_name.lower() not in listing.item.name.lower():
                continue
            if max_price is not None and listing.price > max_price:
                continue
            results.append(listing)
        return results
