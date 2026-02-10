import asyncio
import functools
import os
import sys
from pathlib import Path
from typing import Any, Callable, Coroutine


def action_guard(func: Callable) -> Callable:
    """Decorator to prevent parallel execution of exclusive tools.

    Requires the decorated method's instance to have an `_active_actions` set.
    """

    @functools.wraps(func)
    async def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        # 1. Check if busy
        if func.__name__ in self._active_actions:
            return {
                "status": "busy",
                "message": f"Already busy with: {func.__name__}",
                "current_action": func.__name__,
            }

        # 2. Set busy state (except for long-running move_to)
        if func.__name__ != "move_to":
            self._active_actions.add(func.__name__)

        try:
            # 3. Execute
            if asyncio.iscoroutinefunction(func):
                return await func(self, *args, **kwargs)
            else:
                return func(self, *args, **kwargs)
        finally:
            # 4. Clear busy state
            if func.__name__ != "move_to":
                self._active_actions.discard(func.__name__)

    return wrapper


def safe_run_async(coro: Coroutine) -> Any:
    """Safely runs an async coroutine from a synchronous context."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if loop.is_running():
        # If the loop is already running (unlikely for main GUI thread),
        # schedule it as a task.
        return asyncio.run_coroutine_threadsafe(coro, loop)
    else:
        return loop.run_until_complete(coro)


def resource_path(relative_path: str) -> Path:
    """Get absolute path to resource, works for dev and for PyInstaller.

    Args:
        relative_path: The relative path to the resource.

    Returns:
        The absolute path to the resource as a Path object.
    """
    if hasattr(sys, "_MEIPASS"):
        # PyInstaller path
        base_path = Path(sys._MEIPASS)
    else:
        # Resolve path relative to this file's location
        # This ensures it works even if run from a different CWD
        # Go up one level from utils to soulscape package root
        base_path = Path(__file__).resolve().parent.parent

    return base_path / relative_path


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
