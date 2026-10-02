"""Headless simulation process for offline mode.

Runs the authoritative local simulation without any window: loads souls
from disk, ticks biology/physics/AI, and persists every SAVE_INTERVAL.
Godot renders this state by polling the same souls.json file.

In online mode there is nothing to simulate locally (the Hub owns the
truth), so owned souls are pushed to the Hub once and the process exits.
"""

from __future__ import annotations

import asyncio
import random
import sys
from collections.abc import Coroutine
from typing import Any

from .constants import SOUL_HEIGHT, SOUL_WIDTH
from .core import Soul
from .core.soul import physics as soul_physics
from .system.logger import log, setup_logging
from .system.persistence import (
    MODE_ONLINE,
    async_load_souls,
    async_save_souls,
    get_client_mode,
    load_settings,
    load_souls,
)

SAVE_INTERVAL_SECONDS = 5.0
TICK_SECONDS = 1.0 / 60.0
DEFAULT_SCREEN_WIDTH = 1920
DEFAULT_SCREEN_HEIGHT = 1080


def resolve_screen_size(settings: dict[str, Any]) -> tuple[int, int]:
    """Screen bounds for roaming, without requiring a display server."""
    width = settings.get("display_width")
    height = settings.get("display_height")
    if isinstance(width, int) and isinstance(height, int):
        return width, height
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            size = (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
            return int(size[0]), int(size[1])
        except Exception as e:
            log.debug(f"System metrics unavailable: {e}")
    return DEFAULT_SCREEN_WIDTH, DEFAULT_SCREEN_HEIGHT


async def load_owned_souls(
    instance_id: str,
    screen_width: int,
    screen_height: int,
    task_scheduler: Any,
    from_hub: bool,
) -> list[Soul]:
    """Rebuild souls, ensuring one locally-owned soul exists."""
    if from_hub:
        raw_souls = await async_load_souls()
    else:
        raw_souls = load_souls()
    souls: list[Soul] = []
    for soul_data in raw_souls:
        souls.append(
            Soul.from_dict(
                data=soul_data,
                soul_registry=souls,
                screen_width=screen_width,
                screen_height=screen_height,
                local_instance_id=instance_id,
                task_scheduler=task_scheduler,
            )
        )
    if not any(s.owner_id == instance_id for s in souls):
        orb_color = (random.random(), random.random(), random.random())
        aura_color = (random.random(), random.random(), random.random())
        souls.append(
            Soul(
                name=f"Soul {len(souls) + 1}",
                orb_color_rgb=orb_color,
                aura_color_rgb=aura_color,
                initial_position=(
                    screen_width // 2 - SOUL_WIDTH // 2,
                    screen_height // 2 - SOUL_HEIGHT // 2,
                ),
                soul_registry=souls,
                screen_width=screen_width,
                screen_height=screen_height,
                local_instance_id=instance_id,
                task_scheduler=task_scheduler,
            )
        )
    return souls


async def persist_owned(souls: list[Soul], instance_id: str) -> None:
    """Save locally-owned souls to disk."""
    owned = [s for s in souls if s.owner_id == instance_id]
    data = [s.to_dict(include_secret=True) for s in owned]
    await async_save_souls(data, owner_id=instance_id)


async def sync_online(instance_id: str, souls: list[Soul]) -> int:
    """Push owned souls to the Hub once, then exit."""
    from .system.network.client import NetworkClient

    owned = [s for s in souls if s.owner_id == instance_id]
    if not owned:
        log.info("Online mode: no owned souls to sync.")
        return 0
    secure_data = [s.to_dict(include_secret=True) for s in owned]
    try:
        res = await NetworkClient().post_souls(
            souls_data=secure_data, owner_id=instance_id
        )
    except Exception as e:
        log.error(f"Online sync failed: {e}")
        return 1
    if res is None:
        log.error("Online sync failed: Hub unreachable.")
        return 1
    if res.get("status") == "success":
        log.info(f"Online mode: synced {len(owned)} souls to Hub.")
        return 0
    log.error(f"Online sync rejected: {res}")
    return 1


async def run_offline(instance_id: str, souls: list[Soul]) -> None:
    """Tick the local simulation until interrupted."""
    loop = asyncio.get_running_loop()
    last_save = loop.time()
    try:
        while True:
            tick_start = loop.time()
            soul_physics.begin_separation_frame()
            for soul in souls:
                soul.update(TICK_SECONDS)
            if loop.time() - last_save >= SAVE_INTERVAL_SECONDS:
                await persist_owned(souls, instance_id)
                last_save = loop.time()
            elapsed = loop.time() - tick_start
            await asyncio.sleep(max(0.0, TICK_SECONDS - elapsed))
    finally:
        await persist_owned(souls, instance_id)
        for soul in souls:
            soul.cleanup()


async def amain() -> int:
    """Entry coroutine: offline loop or one-shot online sync."""
    settings = load_settings()
    instance_id = str(settings.get("instance_id", "unknown"))
    screen_width, screen_height = resolve_screen_size(settings)
    loop = asyncio.get_running_loop()

    def scheduler(coro: Coroutine[Any, Any, Any]) -> None:
        asyncio.run_coroutine_threadsafe(coro, loop)

    online = get_client_mode() == MODE_ONLINE
    souls = await load_owned_souls(
        instance_id, screen_width, screen_height, scheduler, online
    )
    if online:
        for soul in souls:
            soul.cleanup()
        return await sync_online(instance_id, souls)
    log.info(f"Offline sim: {len(souls)} souls ({screen_width}x{screen_height}).")
    try:
        await run_offline(instance_id, souls)
    except KeyboardInterrupt:
        log.info("Offline sim stopping.")
    return 0


def main() -> int:
    """Run the headless simulation process."""
    setup_logging()
    try:
        return asyncio.run(amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
