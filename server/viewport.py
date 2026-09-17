"""Viewport protocol v2 downstream (issue #12).

Per-connection downstream state for authenticated WebSocket clients:
initial snapshot, sequenced delta frames, resume via a per-connection
ring buffer, and backpressure (move coalescing, send-rate downgrade,
resumable close).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable

from shared import protocol

from . import database
from . import persistence

logger = logging.getLogger("soulscape_hub")

PUMP_INTERVAL_SECONDS = 0.2
RING_BUFFER_SIZE = 64
SNAP_JUMP_PX = 250.0
MOVE_EPSILON_PX = 0.5
HIGH_WATER_OPS = 256
MAX_SLOW_FLUSHES = 5
MAX_FLUSH_INTERVAL_SECONDS = 5.0
RESUME_TTL_SECONDS = 300.0

_DOMAIN_MOVE = "move"
_DOMAIN_PRIORITY = "priority"

SendFn = Callable[[dict], Awaitable[None]]


def _move_op(soul_id: str, x: float, y: float, snap: bool = False) -> dict:
    op = {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "state": {"soul_id": soul_id, "x": x, "y": y},
    }
    if snap:
        op["snap"] = True
    return op


def _remove_op(soul_id: str) -> dict:
    return {
        "op": protocol.EntityOpKind.REMOVE.value,
        "soul_id": soul_id,
        "state": None,
    }


def _economy_op(soul_id: str, essence: float) -> dict:
    return {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "domain": "economy",
        "state": {"soul_id": soul_id, "essence": essence},
    }


def read_positions() -> dict[str, tuple[float, float]]:
    """Position map with the read-through view: the tick's unflushed dirty
    set overlaid on the DB, so the viewport never lags a flush."""
    return persistence.read_positions_through()


def read_wallets() -> list[dict]:
    wallets: list[dict] = []
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT soul_id, COALESCE(essence, 0.0) AS essence FROM souls"
        ).fetchall()
    for row in rows:
        wallets.append({"soul_id": row["soul_id"], "essence": float(row["essence"])})
    return wallets


def read_region() -> dict:
    width, height = database.SCREEN_BOUNDS
    return {"x": 0.0, "y": 0.0, "w": float(width), "h": float(height)}


def diff_positions(
    current: dict[str, tuple[float, float]],
    committed: dict[str, tuple[float, float]],
) -> list[tuple[dict, str]]:
    ops: list[tuple[dict, str]] = []
    for soul_id, (x, y) in current.items():
        prev = committed.get(soul_id)
        if prev is None:
            ops.append((_move_op(soul_id, x, y, snap=True), _DOMAIN_MOVE))
            continue
        dist = math.hypot(x - prev[0], y - prev[1])
        if dist > SNAP_JUMP_PX:
            ops.append((_move_op(soul_id, x, y, snap=True), _DOMAIN_MOVE))
        elif dist > MOVE_EPSILON_PX:
            ops.append((_move_op(soul_id, x, y), _DOMAIN_MOVE))
    for soul_id in committed:
        if soul_id not in current:
            ops.append((_remove_op(soul_id), _DOMAIN_PRIORITY))
    return ops


class ViewportSession:
    def __init__(self, owner_id: str, ring_size: int = RING_BUFFER_SIZE) -> None:
        self.conn_id = uuid.uuid4().hex
        self.owner_id = owner_id
        self.next_seq = 0
        self.ring: collections.deque = collections.deque(maxlen=ring_size)
        self.committed: dict[str, tuple[float, float]] = {}
        self.pending_moves: dict[str, dict] = {}
        self.pending_priority: list[dict] = []
        self.flush_interval = PUMP_INTERVAL_SECONDS
        self.slow_flushes = 0
        self.last_activity = time.monotonic()
        self.live = True
        self._lock = asyncio.Lock()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def expired(self) -> bool:
        return (time.monotonic() - self.last_activity) > RESUME_TTL_SECONDS

    def enqueue(self, op: dict, domain: str = _DOMAIN_MOVE) -> None:
        self.touch()
        if domain == _DOMAIN_MOVE:
            soul_id = op["soul_id"]
            prev = self.pending_moves.get(soul_id)
            if prev is not None and prev.get("snap"):
                op["snap"] = True
            self.pending_moves[soul_id] = op
        else:
            self.pending_priority.append(op)

    def pending_count(self) -> int:
        return len(self.pending_moves) + len(self.pending_priority)

    def queue_full(self) -> bool:
        return self.pending_count() > HIGH_WATER_OPS

    def _take_pending(self) -> list[dict]:
        ops = self.pending_priority + list(self.pending_moves.values())
        self.pending_priority = []
        self.pending_moves = {}
        return ops


def build_snapshot(
    session: ViewportSession,
    reason: protocol.SnapReason,
    tick_id: int,
) -> dict:
    positions = read_positions()
    souls = [
        {"soul_id": soul_id, "x": x, "y": y} for soul_id, (x, y) in positions.items()
    ]
    protocol.Snapshot(
        reason=reason,
        tick_id=tick_id,
        souls=[protocol.SoulWireState(**s) for s in souls],
    )
    frame = protocol.envelope(
        protocol.MessageType.SNAPSHOT,
        conn_id=session.conn_id,
        seq=session.next_seq,
        reason=reason.value,
        tick_id=tick_id,
        souls=souls,
        region=read_region(),
        plots=[],
        wallets=read_wallets(),
    )
    session.next_seq += 1
    session.committed = dict(positions)
    session.touch()
    return frame


async def flush(
    session: ViewportSession,
    positions: dict[str, tuple[float, float]],
    tick_id: int,
    send: SendFn,
) -> str:
    async with session._lock:
        if session.pending_count() == 0:
            session.slow_flushes = 0
            session.flush_interval = PUMP_INTERVAL_SECONDS
            return "ok"
        if session.queue_full():
            session.slow_flushes += 1
            session.flush_interval = min(
                session.flush_interval * 2.0, MAX_FLUSH_INTERVAL_SECONDS
            )
            if session.slow_flushes >= MAX_SLOW_FLUSHES:
                return "closed"
            return "saturated"
        ops = session._take_pending()
        frame = protocol.envelope(
            protocol.MessageType.DELTA,
            seq=session.next_seq,
            base_seq=session.next_seq - 1 if session.next_seq > 0 else 0,
            tick_id=tick_id,
            ops=ops,
        )
        await send(frame)
        session.ring.append(
            {
                "seq": frame["seq"],
                "base_seq": frame["base_seq"],
                "tick_id": tick_id,
                "ops": ops,
            }
        )
        session.next_seq += 1
        session.committed = dict(positions)
        session.slow_flushes = 0
        session.flush_interval = PUMP_INTERVAL_SECONDS
        session.touch()
        return "ok"


class ViewportManager:
    def __init__(self) -> None:
        self.sessions: dict[str, ViewportSession] = {}

    def create(
        self, owner_id: str, ring_size: int = RING_BUFFER_SIZE
    ) -> ViewportSession:
        self._evict_expired()
        session = ViewportSession(owner_id, ring_size=ring_size)
        self.sessions[session.conn_id] = session
        return session

    def get(self, conn_id: str | None) -> ViewportSession | None:
        if not conn_id:
            return None
        session = self.sessions.get(conn_id)
        if session is None or session.expired():
            self.sessions.pop(conn_id, None)
            return None
        return session

    def drop(self, conn_id: str) -> None:
        self.sessions.pop(conn_id, None)

    def _evict_expired(self) -> None:
        stale = [c for c, s in self.sessions.items() if s.expired()]
        for conn_id in stale:
            self.sessions.pop(conn_id, None)

    def sessions_for_owner(self, owner_id: str) -> list[ViewportSession]:
        self._evict_expired()
        return [s for s in self.sessions.values() if s.owner_id == owner_id and s.live]

    def notify_economy(self, owner_id: str, soul_id: str, essence: float) -> int:
        count = 0
        for session in self.sessions_for_owner(owner_id):
            session.enqueue(_economy_op(soul_id, essence), _DOMAIN_PRIORITY)
            count += 1
        return count

    def notify_economy_soul(self, soul_id: str) -> int:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT owner_id, COALESCE(essence, 0.0) AS essence"
                " FROM souls WHERE soul_id = ?",
                (soul_id,),
            ).fetchone()
        if row is None:
            return 0
        return self.notify_economy(row["owner_id"], soul_id, float(row["essence"]))

    async def resume(
        self,
        session: ViewportSession,
        conn_id: str | None,
        last_seq: int | None,
        tick_id: int,
        send: SendFn,
    ) -> str:
        old = self.get(conn_id)
        if old is None or old.owner_id != session.owner_id:
            return "snapshot"
        if not isinstance(last_seq, int):
            return "snapshot"
        if not old.ring:
            return "replayed" if last_seq == old.next_seq - 1 else "snapshot"
        if last_seq < old.ring[0]["seq"] - 1 or last_seq >= old.next_seq:
            return "snapshot"
        frames = [f for f in old.ring if f["seq"] > last_seq]
        async with session._lock:
            for frame in frames:
                fresh = protocol.envelope(
                    protocol.MessageType.DELTA,
                    seq=session.next_seq,
                    base_seq=(session.next_seq - 1 if session.next_seq > 0 else 0),
                    tick_id=frame["tick_id"],
                    ops=frame["ops"],
                )
                await send(fresh)
                session.ring.append(
                    {
                        "seq": fresh["seq"],
                        "base_seq": fresh["base_seq"],
                        "tick_id": frame["tick_id"],
                        "ops": frame["ops"],
                    }
                )
                session.next_seq += 1
            session.committed = dict(old.committed)
            session.touch()
        if old is not session:
            self.drop(old.conn_id)
        return "replayed"


viewport = ViewportManager()
