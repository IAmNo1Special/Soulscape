"""Local implementation of the DataStore using JSON files."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ...system.logger import log
from ...utils.helpers import get_appdata_dir
from .base import DataStore


class LocalStore(DataStore):
    """Local JSON-backed storage implementation."""

    def __init__(self):
        self.appdata_dir = get_appdata_dir()

    def _get_file(self, filename: str) -> Path:
        return self.appdata_dir / filename

    async def _load_json(self, path: Path) -> Any:
        if not path.exists():
            return {}
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._read_json_sync, path)
        except Exception as e:
            log.error(f"Error loading local store from {path}: {e}")
            return {}

    def _read_json_sync(self, path: Path) -> Any:
        from ...system.persistence import _read_json_document

        status, data = _read_json_document(path)
        if status != "ok":
            return {}
        return data

    async def _save_json(self, path: Path, data: Any) -> bool:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_json_sync, path, data)
            return True
        except Exception as e:
            log.error(f"Error saving local store to {path}: {e}")
            return False

    def _write_json_sync(self, path: Path, data: Any) -> None:
        from ...system.persistence import _atomic_write_json

        if not _atomic_write_json(path, data):
            raise OSError(f"Failed to write {path}")

    # --- Marketplace ---
    async def load_marketplace(self) -> Any:
        return await self._load_json(self._get_file("marketplace.json"))

    async def save_marketplace(self, data: dict[str, Any]) -> bool:
        return await self._save_json(self._get_file("marketplace.json"), data)

    # --- Message Board ---
    async def load_messageboard(self) -> Any:
        return await self._load_json(self._get_file("messageboard.json"))

    async def save_messageboard(self, data: list[dict[str, Any]]) -> bool:
        return await self._save_json(self._get_file("messageboard.json"), data)
