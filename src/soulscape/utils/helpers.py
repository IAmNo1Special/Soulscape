import os
import sys


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    if hasattr(sys, "_MEIPASS"):
        # PyInstaller path
        base_path = sys._MEIPASS
    else:
        # Resolve path relative to this file's location
        # This ensures it works even if run from a different CWD
        # Go up one level from utils to soulscape package root
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    return os.path.join(base_path, relative_path)
