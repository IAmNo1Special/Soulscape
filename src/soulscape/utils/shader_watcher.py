"""Shader file watcher for hot-reloading."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from soulscape.system.logger import log


class ShaderWatcher:
    """Manages file system watching for shader files to support hot-reloading.

    Attributes:
        observer: The watchdog observer instance.
        watched_files: Dictionary mapping file paths to their last modified time.
        callbacks: Dictionary mapping file paths to sets of reload callback functions.
        watched_paths: Set of directory paths currently being watched.
        disabled: Whether the watcher is currently disabled (e.g. in production).
        running: Whether the watcher is currently running.
    """

    def __init__(self) -> None:
        """Initializes the ShaderWatcher."""
        self.observer = Observer()
        self.watched_files: dict[str, float] = {}  # path -> last modified time
        self.callbacks: dict[str, set[Callable[[], None]]] = (
            {}
        )  # path -> set of callbacks
        self.watched_paths: set[str] = (
            set()
        )  # Track which directories we're watching

        # Disable watcher if frozen (packaged) or explicitly disabled via env var
        self.disabled = (
            getattr(sys, "frozen", False)
            or os.environ.get("SOULSCAPE_PRODUCTION", "0") == "1"
        )

        if not self.disabled:
            self.observer.start()
            log.debug("ShaderWatcher started (Dev Mode).")
        else:
            log.debug("ShaderWatcher disabled (Production/Frozen Mode).")

        self.running = True

    def watch_file(self, file_path: str, callback: Callable[[], None]) -> None:
        """Start watching a shader file and call callback when it changes.

        Args:
            file_path: The path to the shader file to watch.
            callback: The callback function to execute on change.
        """
        if self.disabled:
            return

        path = Path(file_path).absolute()
        if not path.exists():
            log.warning("Warning: Shader file %s does not exist", path)
            return

        # Add to watched files
        str_path = str(path)
        self.watched_files[str_path] = path.stat().st_mtime

        # Add callback
        if str_path not in self.callbacks:
            self.callbacks[str_path] = set()
        self.callbacks[str_path].add(callback)

        # Watch the parent directory if not already watching
        parent = str(path.parent)
        if parent not in self.watched_paths:
            handler = ShaderFileHandler(self)
            self.observer.schedule(handler, parent, recursive=False)
            self.watched_paths.add(parent)
            log.debug("Watching directory for changes: %s", parent)

    def remove_watch(
        self, file_path: str, callback: Callable[[], None]
    ) -> None:
        """Stop watching a file.

        Args:
            file_path: The path to the shader file.
            callback: The callback function to unregister.
        """
        if self.disabled:
            return

        str_path = str(Path(file_path).absolute())
        if str_path in self.callbacks and callback in self.callbacks[str_path]:
            self.callbacks[str_path].remove(callback)
            if not self.callbacks[str_path]:
                del self.callbacks[str_path]
                self.watched_files.pop(str_path, None)

    def check_modified(self, path: str) -> bool:
        """Check if a file has been modified since last check.

        Args:
            path: The path to the file to check.

        Returns:
            True if modified, False otherwise.
        """
        path_obj = Path(path)
        if not path_obj.exists():
            return False

        last_modified = path_obj.stat().st_mtime
        if (
            path in self.watched_files
            and self.watched_files[path] < last_modified
        ):
            self.watched_files[path] = last_modified
            return True
        return False

    def stop(self) -> None:
        """Stop the file watcher."""
        if self.running and not self.disabled:
            self.observer.stop()
            self.observer.join()
            self.running = False


class ShaderFileHandler(FileSystemEventHandler):
    """Event handler for shader file changes.

    Attributes:
        watcher: The ShaderWatcher instance associated with this handler.
    """

    def __init__(self, watcher: ShaderWatcher) -> None:
        """Initializes the ShaderFileHandler with a watcher."""
        self.watcher = watcher

    def on_modified(self, event: Any) -> None:
        """Handles the file modified event."""
        if not event.is_directory and event.src_path in self.watcher.callbacks:
            if self.watcher.check_modified(event.src_path):
                log.debug("Reloading shader: %s", event.src_path)
                for callback in list(self.watcher.callbacks[event.src_path]):
                    try:
                        callback()
                    except Exception as e:
                        log.error("Error in shader reload callback: %s", e)


# Global shader watcher instance
shader_watcher = ShaderWatcher()
