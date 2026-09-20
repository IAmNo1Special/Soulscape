from .base import DataStore
from .local_store import LocalStore
from .remote_store import RemoteStore


def get_default_store() -> DataStore:
    """Selects the store for the explicit client mode.

    Online mode is a viewport to Hub authority: persistence goes through
    the Hub-backed RemoteStore. Offline mode keeps the hand-editable local
    JSON files via LocalStore. The two never mix or sync.
    """
    from ...system.persistence import MODE_ONLINE, get_client_mode

    if get_client_mode() == MODE_ONLINE:
        return RemoteStore()
    return LocalStore()


__all__ = ["DataStore", "LocalStore", "RemoteStore", "get_default_store"]
