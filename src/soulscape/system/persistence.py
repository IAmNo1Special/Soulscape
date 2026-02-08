# persistence.py
"""Persistence layer for saving and loading soul configurations."""

import json
import os
import tempfile
from pathlib import Path

from soulscape.system.logger import log


def get_appdata_dir() -> Path:
    """Get the Soulscape data directory in %APPDATA%."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        soulscape_dir = Path(appdata) / "Soulscape"
    else:
        # Fallback to current directory
        soulscape_dir = Path.cwd() / ".soulscape"

    soulscape_dir.mkdir(parents=True, exist_ok=True)
    return soulscape_dir


def get_souls_file() -> Path:
    """Get the path to souls.json."""
    return get_appdata_dir() / "souls.json"


def save_souls(souls: list[dict]) -> bool:
    """
    Save soul configurations to souls.json using atomic write.

    Args:
        souls: List of serialized soul dictionaries.

    Returns:
        True if save succeeded, False otherwise
    """
    try:
        souls_file = get_souls_file()

        # Atomic write: write to temp, then replace
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


def load_souls() -> list[dict]:
    """
    Load soul configurations from souls.json.

    Returns:
        List of raw soul dictionaries, or empty list if file doesn't exist or is invalid
    """
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


def delete_souls_file() -> bool:
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


def save_settings(settings_data: dict) -> bool:
    """
    Save global settings to settings.json using atomic write.

    Args:
        settings_data: Dictionary of settings to save (e.g. {'opacity': 80})

    Returns:
        True if save succeeded, False otherwise
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


def load_settings() -> dict:
    """
    Load global settings from settings.json.
    Handles potential corruption/nested dicts.

    Returns:
        Dict with settings, defaults used for missing keys.
    """
    defaults = {"opacity": 100, "spawn_hotkey": "ctrl+shift+s"}

    try:
        settings_file = get_settings_file()
        if not settings_file.exists():
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
        for key, value in defaults.items():
            if key not in settings:
                settings[key] = value

        return settings
    except Exception as e:
        log.error(f"Error loading settings: {e}")
        return defaults
