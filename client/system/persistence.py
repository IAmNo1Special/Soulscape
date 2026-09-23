"""Persistence layer for saving and loading soul configurations."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ..utils.helpers import get_appdata_dir
from .logger import log

MODE_OFFLINE = "offline"
MODE_ONLINE = "online"
_MODES = (MODE_OFFLINE, MODE_ONLINE)

_DEFAULT_HUB_URL = "http://localhost:9785"


def get_souls_file() -> Path:
    """Get the path to souls.json."""
    return get_appdata_dir() / "souls.json"


def get_settings_file() -> Path:
    """Get the path to settings.json."""
    return get_appdata_dir() / "settings.json"


def get_client_mode() -> str:
    """Explicit client mode from settings: "offline" or "online".

    Never inferred from environment variables. Invalid or missing values
    fall back to offline, the safe default that keeps the local game.
    """
    try:
        mode = load_settings().get("mode", MODE_OFFLINE)
    except Exception:
        return MODE_OFFLINE
    if mode not in _MODES:
        log.warning(f"Unknown client mode {mode!r}; using offline.")
        return MODE_OFFLINE
    return mode


def resolve_hub_url() -> str:
    """Hub address for online mode: HUB_URL env, then hub_url setting."""
    env_url = os.getenv("HUB_URL", "").strip()
    if env_url:
        return env_url
    try:
        return str(load_settings().get("hub_url", "") or "").strip()
    except Exception:
        return ""


def _atomic_write_json(path: Path, data: Any) -> bool:
    """Write JSON atomically, keeping a .bak of the last good file."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            bak_path = path.with_name(path.name + ".bak")
            bak_path.write_bytes(path.read_bytes())
        fd, temp_path = tempfile.mkstemp(dir=path.parent, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, path)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise
        return True
    except Exception as e:
        log.error(f"Error writing {path}: {e}")
        return False


def _read_json_document(path: Path) -> tuple[str, Any]:
    """Read a JSON document, failing soft on corruption.

    Returns (status, data) with status in "ok", "missing", "corrupt".
    Corrupt files are moved aside as .corrupt-<timestamp> so the caller
    can continue from defaults without losing the original bytes.
    """
    if not path.exists():
        return "missing", None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return "ok", json.load(f)
    except json.JSONDecodeError as e:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        corrupt_path = path.with_name(f"{path.name}.corrupt-{stamp}")
        try:
            path.rename(corrupt_path)
        except Exception as rename_error:
            log.error(f"Could not quarantine corrupt {path}: {rename_error}")
            return "corrupt", None
        log.error(
            f"Corrupt JSON in {path} ({e}); moved to {corrupt_path}, "
            "continuing from defaults."
        )
        return "corrupt", None
    except Exception as e:
        log.error(f"Error reading {path}: {e}")
        return "corrupt", None


def _strip_secrets(souls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return soul dicts with authentication secrets removed for disk."""
    return [{k: v for k, v in soul.items() if k != "secret"} for soul in souls]


async def async_save_souls(souls: list[dict[str, Any]], owner_id: str = "") -> bool:
    """Async version of save_souls. Passes through to Hub in online mode.

    Args:
        souls: List of serialized soul dictionaries.
        owner_id: The instance_id of this client, used for owner-scoped sync.

    Returns:
        True if save succeeded, False otherwise.
    """
    if get_client_mode() == MODE_ONLINE:
        if not souls:
            return True
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

    return save_souls(souls)


def save_souls(souls: list[dict[str, Any]]) -> bool:
    """Save soul configurations to the Hub (online) or souls.json (offline).

    Secrets are never written to disk: the offline branch strips them
    before serialization. They are re-generated on load when absent.
    NOTE: Synchronous Hub saves will block the current thread.
    If called from the main thread, this can freeze the GUI.
    """
    if get_client_mode() == MODE_ONLINE:
        if not souls:
            return True
        from .network.client import NetworkClient

        from ..utils.helpers import safe_run_async

        try:
            is_main = threading.current_thread() is threading.main_thread()
            thread_type = "main thread" if is_main else "worker thread"
            log.debug(f"Saving souls to Hub ({thread_type} sync)...")

            res = safe_run_async(NetworkClient().post_souls(souls))

            if hasattr(res, "result") and callable(res.result):
                res = res.result()
            is_saved = res is not None and res.get("status") == "success"
            log.debug(f"Souls saved to Hub: {is_saved}")
            return is_saved
        except Exception as e:
            log.error(f"Error saving souls to Hub: {e}")
            return False

    ok = _atomic_write_json(get_souls_file(), _strip_secrets(souls))
    if ok:
        log.debug(f"Saved {len(souls)} souls to {get_souls_file()}")
    return ok


async def async_load_souls() -> list[dict[str, Any]]:
    """Async version of load_souls."""
    if get_client_mode() == MODE_ONLINE:
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
    """Load soul configurations from the Hub (online) or souls.json (offline).

    Returns:
        List of raw soul dictionaries, or empty list if none exist.
    """
    if get_client_mode() == MODE_ONLINE:
        from ..utils.helpers import safe_run_async
        from .network.client import NetworkClient

        try:
            is_main = threading.current_thread() is threading.main_thread()
            thread_type = "main thread" if is_main else "worker thread"
            log.debug(f"Loading souls from Hub ({thread_type} sync)...")

            data = safe_run_async(NetworkClient().get_souls())

            if hasattr(data, "result") and callable(data.result):
                data = data.result()

            if isinstance(data, list):
                log.debug(f"Loaded {len(data)} souls from Hub")
                return data
            return []
        except Exception as e:
            log.error(f"Error loading souls from Hub: {e}")
            return []

    souls_file = get_souls_file()
    status, data = _read_json_document(souls_file)
    if status != "ok":
        return []
    if not isinstance(data, list):
        log.warning(f"Invalid data format in {souls_file}")
        return []
    log.debug(f"Loaded {len(data)} raw souls from {souls_file}")
    return data


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


def save_settings(settings_data: dict[str, Any]) -> bool:
    """Save global settings to settings.json using atomic write.

    Args:
        settings_data: Dictionary of settings to save (e.g. {'opacity': 80}).

    Returns:
        True if save succeeded, False otherwise.
    """
    ok = _atomic_write_json(get_settings_file(), settings_data)
    if ok:
        log.debug(f"Settings saved to {get_settings_file()}")
    return ok


def load_settings() -> dict[str, Any]:
    """Load global settings from settings.json.

    Corrupt files are quarantined as .corrupt-<timestamp> and defaults
    are used, so a hand-edited settings file can never brick the client.

    Returns:
        Dict with settings, defaults used for missing keys.
    """
    import uuid

    defaults = {
        "opacity": 100,
        "spawn_hotkey": "ctrl+shift+s",
        "instance_id": uuid.uuid4().hex,
        "hub_url": _DEFAULT_HUB_URL,
        "mode": MODE_OFFLINE,
    }

    try:
        settings_file = get_settings_file()
        status, settings = _read_json_document(settings_file)
        if status != "ok":
            save_settings(defaults)
            return defaults

        opacity = settings.get("opacity")
        if isinstance(opacity, dict):
            opacity = opacity.get("opacity", defaults["opacity"])

        if not isinstance(opacity, (int, float)):
            opacity = defaults["opacity"]

        settings["opacity"] = int(opacity)

        mode = settings.get("mode", MODE_OFFLINE)
        if mode not in _MODES:
            log.warning(f"Unknown mode {mode!r} in settings; using offline.")
            settings["mode"] = MODE_OFFLINE

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
