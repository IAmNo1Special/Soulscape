"""Hub world tick (issue #7).

Fixed-timestep 5 Hz simulation loop that owns Soul positions and movement
physics. Active only behind the `hub_authoritative` feature flag
(`HUB_AUTHORITATIVE=1`). Flag off: the tick is never started and the Hub
behaves exactly as before.

The hot path touches only SQLite. No network calls, no LLM calls.

Intent adjudication (issue #14): each tick pumps the durable intent queue
before integrating motion. A move_to intent re-validates custody against
live state, then clamps the target to the reachable radius
`INTENT_MOVE_SPEED * INTENT_HORIZON_SECONDS` and steers the Soul toward it
at INTENT_MOVE_SPEED. The clamp choice: the tick runs at 5 Hz and steering
needs several ticks to converge, so the horizon is deliberately generous —
any target a Tamer could plausibly click (within 3000 px) is accepted
verbatim; only far-away forged targets are pulled back along the original
direction, so a clamped move still heads the way the client asked. Arrival
stops within INTENT_ARRIVAL_EPS of the target.
"""

import asyncio
import json
import logging
import math
import os
import time

from . import database
from . import intents

logger = logging.getLogger("soulscape_hub")

TICK_HZ = 5
TICK_DT = 1.0 / TICK_HZ
WORLD_EPOCH = 0.0
_POSITION_MARGIN = 10.0

INTENT_MOVE_SPEED = 600.0
INTENT_HORIZON_SECONDS = 5.0
INTENT_ARRIVAL_EPS = 2.0


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

    def pump_intents(self) -> int:
        done = 0
        for intent in intents.pending_intents():
            try:
                if intent["kind"] == "move_to":
                    self._adjudicate_move_to(intent)
                else:
                    intents.mark_rejected(
                        intent["intent_id"], {"reason": "unknown_kind"}
                    )
            except Exception:
                logger.exception("Intent adjudication failed: %s", intent["intent_id"])
                intents.mark_rejected(intent["intent_id"], {"reason": "internal"})
            done += 1
        return done

    def _adjudicate_move_to(self, intent: dict) -> None:
        intent_id = intent["intent_id"]
        soul_id = intent["soul_id"]
        payload = intent["payload"] or {}
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT position, custodian_id, owner_id FROM souls WHERE soul_id = ?",
                (soul_id,),
            )
            row = cursor.fetchone()
            if row is None:
                intents.mark_rejected(intent_id, {"reason": "soul_not_found"})
                return
            custodian = row["custodian_id"] or row["owner_id"]
            if intent["custodian_id"] is not None and (
                custodian != intent["custodian_id"]
            ):
                intents.mark_rejected(intent_id, {"reason": "custody"})
                return
            x, y = _parse_pair(row["position"])
            tx = min(
                max(float(payload["x"]), _POSITION_MARGIN),
                database.SCREEN_BOUNDS[0] - _POSITION_MARGIN,
            )
            ty = min(
                max(float(payload["y"]), _POSITION_MARGIN),
                database.SCREEN_BOUNDS[1] - _POSITION_MARGIN,
            )
            dx, dy = tx - x, ty - y
            dist = math.hypot(dx, dy)
            clamped = False
            max_reach = INTENT_MOVE_SPEED * INTENT_HORIZON_SECONDS
            if dist > max_reach:
                ratio = max_reach / dist
                tx, ty = x + dx * ratio, y + dy * ratio
                clamped = True
                logger.warning(
                    "Intent %s target clamped to reachable radius", intent_id
                )
            if math.hypot(tx - x, ty - y) <= INTENT_ARRIVAL_EPS:
                cursor.execute(
                    "UPDATE souls SET velocity = ?, move_target = NULL "
                    "WHERE soul_id = ?",
                    (json.dumps([0.0, 0.0]), soul_id),
                )
            else:
                dist = math.hypot(tx - x, ty - y)
                vx, vy = (
                    (tx - x) / dist * INTENT_MOVE_SPEED,
                    (ty - y) / dist * INTENT_MOVE_SPEED,
                )
                cursor.execute(
                    "UPDATE souls SET velocity = ?, move_target = ? WHERE soul_id = ?",
                    (json.dumps([vx, vy]), json.dumps([tx, ty]), soul_id),
                )
            conn.commit()
        intents.mark_adjudicated(
            intent_id,
            {
                "x": tx,
                "y": ty,
                "clamped": clamped,
                "speed": INTENT_MOVE_SPEED,
            },
        )

    def step(self) -> int:
        self.pump_intents()
        moved: list[tuple[str, str]] = []
        width, height = database.SCREEN_BOUNDS
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()
            cursor.execute("SELECT soul_id, position, velocity, move_target FROM souls")
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
                target = None
                raw_target = row["move_target"]
                if raw_target:
                    try:
                        target = _parse_pair(raw_target)
                    except (ValueError, TypeError, IndexError):
                        target = None
                if target is not None:
                    tx, ty = target
                    remaining = math.hypot(tx - nx, ty - ny)
                    passed = (tx - nx) * vx + (ty - ny) * vy <= 0.0
                    if remaining <= INTENT_ARRIVAL_EPS or passed:
                        nx, ny = tx, ty
                        cursor.execute(
                            "UPDATE souls SET velocity = ?, move_target = "
                            "NULL WHERE soul_id = ?",
                            (json.dumps([0.0, 0.0]), row["soul_id"]),
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
