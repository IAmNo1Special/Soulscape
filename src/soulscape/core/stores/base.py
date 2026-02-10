"""Base storage definitions for Soulscape interaction systems."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DataStore(ABC):
    """Abstract interface for storing and retrieving interaction data."""

    # --- Marketplace ---
    @abstractmethod
    async def load_marketplace(self) -> Any:
        """Loads all marketplace data."""
        pass

    @abstractmethod
    async def save_marketplace(self, data: dict[str, Any]) -> bool:
        """Saves all marketplace data."""
        pass

    # --- Message Board ---
    @abstractmethod
    async def load_messageboard(self) -> Any:
        """Loads all message board data."""
        pass

    @abstractmethod
    async def save_messageboard(self, data: dict[str, Any]) -> bool:
        """Saves all message board data."""
        pass

    # --- Hub-specific Granular Operations (Optional for LocalStore) ---
    async def add_listing(self, listing_data: dict[str, Any]) -> bool:
        """Sends a new listing to the store."""
        return True

    async def delete_listing(self, listing_id: str) -> bool:
        """Removes a listing from the store."""
        return True

    async def add_post(self, post_data: dict[str, Any]) -> bool:
        """Sends a new social post to the store."""
        return True

    async def add_reply(self, reply_data: dict[str, Any]) -> bool:
        """Sends a social reply to the store."""
        return True
