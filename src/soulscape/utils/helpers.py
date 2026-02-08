"""Utility helper functions."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller.

    Args:
        relative_path: The relative path to the resource.

    Returns:
        The absolute path to the resource.
    """
    if hasattr(sys, "_MEIPASS"):
        # PyInstaller path
        base_path = sys._MEIPASS
    else:
        # Resolve path relative to this file's location
        # This ensures it works even if run from a different CWD
        # Go up one level from utils to soulscape package root
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    return os.path.join(base_path, relative_path)


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
