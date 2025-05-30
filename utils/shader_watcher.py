import os
import time
from pathlib import Path
from typing import Dict, Callable, Set, Optional, List
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent

class ShaderWatcher:
    def __init__(self):
        self.observer = Observer()
        self.watched_files: Dict[str, float] = {}  # path -> last modified time
        self.callbacks: Dict[str, Set[Callable[[], None]]] = {}  # path -> set of callbacks
        self.watched_paths: Set[str] = set()  # Track which directories we're watching
        self.observer.start()
        self.running = True

    def watch_file(self, file_path: str, callback: Callable[[], None]) -> None:
        """Start watching a shader file and call callback when it changes"""
        path = Path(file_path).absolute()
        if not path.exists():
            print(f"Warning: Shader file {path} does not exist")
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
            print(f"Watching directory for changes: {parent}")

    def remove_watch(self, file_path: str, callback: Callable[[], None]) -> None:
        """Stop watching a file"""
        str_path = str(Path(file_path).absolute())
        if str_path in self.callbacks and callback in self.callbacks[str_path]:
            self.callbacks[str_path].remove(callback)
            if not self.callbacks[str_path]:
                del self.callbacks[str_path]
                self.watched_files.pop(str_path, None)

    def check_modified(self, path: str) -> bool:
        """Check if a file has been modified since last check"""
        path_obj = Path(path)
        if not path_obj.exists():
            return False
            
        last_modified = path_obj.stat().st_mtime
        if path in self.watched_files and self.watched_files[path] < last_modified:
            self.watched_files[path] = last_modified
            return True
        return False

    def stop(self):
        """Stop the file watcher"""
        if self.running:
            self.observer.stop()
            self.observer.join()
            self.running = False

class ShaderFileHandler(FileSystemEventHandler):
    def __init__(self, watcher: ShaderWatcher):
        self.watcher = watcher

    def on_modified(self, event):
        if not event.is_directory and event.src_path in self.watcher.callbacks:
            if self.watcher.check_modified(event.src_path):
                print(f"Reloading shader: {event.src_path}")
                for callback in list(self.watcher.callbacks[event.src_path]):
                    try:
                        callback()
                    except Exception as e:
                        print(f"Error in shader reload callback: {e}")

# Global shader watcher instance
shader_watcher = ShaderWatcher()
