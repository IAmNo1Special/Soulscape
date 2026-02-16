"""Persistence layer for saving and loading soul configurations."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from ..utils.helpers import get_appdata_dir
from .logger import log


def get_souls_file() -> Path:
    """Get the path to souls.json."""
    return get_appdata_dir() / "souls.json"


async def async_save_souls(
    souls: list[dict[str, Any]], owner_id: str = ""
) -> bool:
    """Async version of save_souls. Passes through to Hub if configured.

    Args:
        souls: List of serialized soul dictionaries.
        owner_id: The instance_id of this client, used for owner-scoped sync.

    Returns:
        True if save succeeded, False otherwise.
    """
    if os.getenv("HUB_URL"):
        from .network.client import NetworkClient

        try:
            log.debug("Async saving souls to Hub...")
            res = await NetworkClient().post_souls(souls, owner_id=owner_id)
            is_saved = res is not None and res.get("status") == "success"
            log.debug(f"Souls saved to Hub: {is_saved}")
            return is_saved
        except Exception as e:
            log.error(f"Error async saving souls to Hub: {e}")
            return False

    # Local save is already fast and synchronous, but we can run it in a thread if needed.
    # For now, just call the sync version.
    return save_souls(souls)


def save_souls(souls: list[dict[str, Any]]) -> bool:
    """Save soul configurations to souls.json or Hub.
    NOTE: Synchronous Hub saves will block the current thread.
    If called from the main thread, this can freeze the GUI.
    """
    if os.getenv("HUB_URL"):
        from .system.network.client import NetworkClient

        from ..utils.helpers import safe_run_async

        try:
            is_main = threading.current_thread() is threading.main_thread()
            thread_type = "main thread" if is_main else "worker thread"
            log.debug(f"Saving souls to Hub ({thread_type} sync)...")

            res = safe_run_async(NetworkClient().post_souls(souls))

            # Handle Future if returned (when loop is already running)
            if hasattr(res, "result") and callable(res.result):
                res = res.result()
            is_saved = res is not None and res.get("status") == "success"
            log.debug(f"Souls saved to Hub: {is_saved}")
            return is_saved
        except Exception as e:
            log.error(f"Error saving souls to Hub: {e}")
            return False

    try:
        souls_file = get_souls_file()
        # ... existing local save logic ...
        fd, temp_path = tempfile.mkstemp(dir=souls_file.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(souls, f, indent=2)
            os.replace(temp_path, souls_file)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

        log.debug(f"Saved {len(souls)} souls to {souls_file}")
        return True

    except Exception as e:
        log.error(f"Error saving souls: {e}")
        return False


async def async_load_souls() -> list[dict[str, Any]]:
    """Async version of load_souls."""
    if os.getenv("HUB_URL"):
        from .network.client import NetworkClient

        try:
            log.debug("Async loading souls from Hub...")
            data = await NetworkClient().get_souls()
            if isinstance(data, list):
                log.debug(f"Loaded {len(data)} souls from Hub")
                return data
            return []
        except Exception as e:
            log.error(f"Error async loading souls from Hub: {e}")
            return []
    return load_souls()


def load_souls() -> list[dict[str, Any]]:
    """Load soul configurations from souls.json or Hub.

    Returns:
        List of raw soul dictionaries, or empty list if none exist.
    """
    if os.getenv("HUB_URL"):
        from ..utils.helpers import safe_run_async
        from .network.client import NetworkClient

        try:
            is_main = threading.current_thread() is threading.main_thread()
            thread_type = "main thread" if is_main else "worker thread"
            log.debug(f"Loading souls from Hub ({thread_type} sync)...")

            data = safe_run_async(NetworkClient().get_souls())

            # Handle Future if returned
            if hasattr(data, "result") and callable(data.result):
                data = data.result()

            if isinstance(data, list):
                log.debug(f"Loaded {len(data)} souls from Hub")
                return data
            return []
        except Exception as e:
            log.error(f"Error loading souls from Hub: {e}")
            return []
    else:

        try:
            souls_file = get_souls_file()

            if not souls_file.exists():
                log.debug(f"No souls file found at {souls_file}")
                return []

            with open(souls_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, list):
                log.warning(f"Invalid data format in {souls_file}")
                return []

            log.debug(f"Loaded {len(data)} raw souls from {souls_file}")
            return data

        except json.JSONDecodeError as e:
            log.error(f"Error parsing souls.json: {e}")
            return []
        except Exception as e:
            log.error(f"Error loading souls: {e}")
            return []


def _delete_souls_file() -> bool:
    """Delete the souls.json file (for testing/reset)."""
    try:
        souls_file = get_souls_file()
        if souls_file.exists():
            souls_file.unlink()
            log.debug(f"Deleted {souls_file}")
        return True
    except Exception as e:
        log.error(f"Error deleting souls file: {e}")
        return False


def get_settings_file() -> Path:
    """Get the path to settings.json."""
    return get_appdata_dir() / "settings.json"


def save_settings(settings_data: dict[str, Any]) -> bool:
    """Save global settings to settings.json using atomic write.

    Args:
        settings_data: Dictionary of settings to save (e.g. {'opacity': 80}).

    Returns:
        True if save succeeded, False otherwise.
    """
    try:
        settings_file = get_settings_file()

        # Load existing first to merge? Or just overwrite?
        # Overwrite is safer for now to ensure clean state.

        fd, temp_path = tempfile.mkstemp(dir=settings_file.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings_data, f, indent=2)
            os.replace(temp_path, settings_file)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

        log.debug(f"Settings saved to {settings_file}")
        return True
    except Exception as e:
        log.error(f"Error saving settings: {e}")
        return False


def load_settings() -> dict[str, Any]:
    """Load global settings from settings.json.

    Handles potential corruption/nested dicts.

    Returns:
        Dict with settings, defaults used for missing keys.
    """
    import uuid

    defaults = {
        "opacity": 100,
        "spawn_hotkey": "ctrl+shift+s",
        "instance_id": uuid.uuid4().hex,
    }

    try:
        settings_file = get_settings_file()
        if not settings_file.exists():
            # Save defaults immediately to persist instance_id
            save_settings(defaults)
            return defaults

        with open(settings_file, "r", encoding="utf-8") as f:
            settings = json.load(f)

        # Sanitize opacity if it's corrupted (nested dict)
        opacity = settings.get("opacity")
        if isinstance(opacity, dict):
            # Try to recover from nested dict
            opacity = opacity.get("opacity", defaults["opacity"])

        # Ensure opacity is int
        if not isinstance(opacity, (int, float)):
            opacity = defaults["opacity"]

        settings["opacity"] = int(opacity)

        # Merge with defaults for any missing keys
        needs_save = False
        for key, value in defaults.items():
            if key not in settings:
                settings[key] = value
                needs_save = True

        if needs_save:
            save_settings(settings)

        return settings
    except Exception as e:
        log.error(f"Error loading settings: {e}")
        return defaults
