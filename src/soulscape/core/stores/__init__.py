import os

from .base import DataStore
from .local_store import LocalStore
from .remote_store import RemoteStore


def get_default_store() -> DataStore:
    """Selects the appropriate store based on environment configuration."""
    if os.getenv("HUB_URL"):
        return RemoteStore()
    return LocalStore()


__all__ = ["DataStore", "LocalStore", "RemoteStore", "get_default_store"]
