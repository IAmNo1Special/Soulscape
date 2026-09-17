"""Hub world tick (issue #7).

Fixed-timestep 5 Hz simulation loop that owns Soul positions and movement
physics. Active only behind the `hub_authoritative` feature flag
(`HUB_AUTHORITATIVE=1`). Flag off: the tick is never started and the Hub
behaves exactly as before.

The hot path touches only SQLite. No network calls, no LLM calls.
"""

import asyncio
import json
import logging
import math
import os
import time

from . import database

logger = logging.getLogger("soulscape_hub")

TICK_HZ = 5
TICK_DT = 1.0 / TICK_HZ
WORLD_EPOCH = 0.0
_POSITION_MARGIN = 10.0


def hub_authoritative_enabled() -> bool:
    return os.getenv("HUB_AUTHORITATIVE", "").lower() in ("1", "true", "yes")


def _parse_pair(raw) -> tuple[float, float]:
    if raw is None:
        return (0.0, 0.0)
    if isinstance(raw, str):
        raw = json.loads(raw)
    x, y = float(raw[0]), float(raw[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("non-finite pair")
    return (x, y)


class WorldTick:
    def __init__(self, tick_dt: float = TICK_DT):
        self.tick_dt = tick_dt
        self.tick_id = 0
        self.enabled = False
        self.running = False
        self.last_duration_ms = 0.0
        self.avg_duration_ms = 0.0
        self.max_duration_ms = 0.0
        self.souls_moved_last_tick = 0
        self.skipped_ticks = 0
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.running:
            return
        self.enabled = True
        self.running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.enabled = False

    async def _run_loop(self) -> None:
        next_deadline = time.monotonic() + self.tick_dt
        while self.running:
            now = time.monotonic()
            delay = next_deadline - now
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                missed = int(-delay // self.tick_dt)
                if missed:
                    self.skipped_ticks += missed
                    self.tick_id += missed
                    next_deadline += missed * self.tick_dt
            next_deadline += self.tick_dt
            started = time.perf_counter()
            await asyncio.to_thread(self.step)
            self._record_duration((time.perf_counter() - started) * 1000.0)

    def _record_duration(self, duration_ms: float) -> None:
        self.last_duration_ms = duration_ms
        if self.avg_duration_ms == 0.0:
            self.avg_duration_ms = duration_ms
        else:
            self.avg_duration_ms += (duration_ms - self.avg_duration_ms) * 0.1
        self.max_duration_ms = max(self.max_duration_ms, duration_ms)

    def step(self) -> int:
        moved: list[tuple[str, str]] = []
        width, height = database.SCREEN_BOUNDS
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute("SELECT soul_id, position, velocity FROM souls")
            for row in cursor.fetchall():
                try:
                    x, y = _parse_pair(row["position"])
                    vx, vy = _parse_pair(row["velocity"])
                except (ValueError, TypeError, KeyError, IndexError):
                    continue
                if vx == 0.0 and vy == 0.0:
                    continue
                nx = min(
                    max(x + vx * self.tick_dt, _POSITION_MARGIN),
                    width - _POSITION_MARGIN,
                )
                ny = min(
                    max(y + vy * self.tick_dt, _POSITION_MARGIN),
                    height - _POSITION_MARGIN,
                )
                moved.append((json.dumps([nx, ny]), row["soul_id"]))
            if moved:
                cursor.executemany(
                    "UPDATE souls SET position = ? WHERE soul_id = ?", moved
                )
            conn.commit()
        self.tick_id += 1
        self.souls_moved_last_tick = len(moved)
        return len(moved)

    def snapshot(self) -> dict:
        positions = []
        with database.get_db() as conn:
            for row in conn.execute("SELECT soul_id, position FROM souls"):
                try:
                    x, y = _parse_pair(row["position"])
                except (ValueError, TypeError, KeyError, IndexError):
                    x, y = 0.0, 0.0
                positions.append({"soul_id": row["soul_id"], "x": x, "y": y})
        return {
            "enabled": self.enabled,
            "running": self.running,
            "tick_hz": round(1.0 / self.tick_dt, 3),
            "tick_id": self.tick_id,
            "world_time": WORLD_EPOCH + self.tick_id * self.tick_dt,
            "last_tick_duration_ms": round(self.last_duration_ms, 3),
            "avg_tick_duration_ms": round(self.avg_duration_ms, 3),
            "max_tick_duration_ms": round(self.max_duration_ms, 3),
            "souls_moved_last_tick": self.souls_moved_last_tick,
            "skipped_ticks": self.skipped_ticks,
            "soul_count": len(positions),
            "positions": positions,
        }
