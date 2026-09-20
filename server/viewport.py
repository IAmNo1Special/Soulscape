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

# Issue #37: the API process no longer shares the sim's in-memory dirty
# set. The API wires a provider (the sim gateway's read-through
# positions query) at startup; the sim process leaves this None and
# uses the local dirty set as before.
_positions_provider: Callable[[], dict[str, tuple[float, float]]] | None = None


def set_positions_provider(
    fn: Callable[[], dict[str, tuple[float, float]]] | None,
) -> None:
    global _positions_provider
    _positions_provider = fn


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


def _state_op(soul_id: str, state: str) -> dict:
    """Statue render state stream (issue #21): a collapsed soul's state
    rides the viewport as a priority op so clients render it distinctly."""
    return {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "domain": "state",
        "state": {"soul_id": soul_id, "state": state},
    }


def _dormancy_op(soul_id: str, dormant: bool) -> dict:
    """Dormancy stream (issue #22): a wallet-derived freeze rides the
    viewport as a priority op on its own domain, orthogonal to the
    lifecycle state stream above. Clients render dormant souls as
    statues, tinted distinctly from collapsed ones."""
    return {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "domain": "dormancy",
        "state": {"soul_id": soul_id, "dormant": dormant},
    }


def _biology_op(
    soul_id: str, satiety: float, hydration: float, hp: float, max_hp: float
) -> dict:
    """Biology stream (issue #30, step 0 of #29): satiety/hydration/hp
    ride the viewport as priority ops on their own domain so online
    clients render shader uniforms from authoritative values instead of
    healthy local defaults."""
    return {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "domain": "biology",
        "state": {
            "soul_id": soul_id,
            "satiety": satiety,
            "hydration": hydration,
            "hp": hp,
            "max_hp": max_hp,
        },
    }


def _identity_op(
    soul_id: str, name: str, species: str, level: int, activity: str
) -> dict:
    """Identity stream (issue #31): name/species/level/activity ride the
    viewport as priority ops on their own domain so the hover nameplate
    and info card track the Hub without waiting for a re-snapshot."""
    return {
        "op": protocol.EntityOpKind.UPSERT.value,
        "soul_id": soul_id,
        "domain": "identity",
        "state": {
            "soul_id": soul_id,
            "name": name,
            "species": species,
            "level": level,
            "activity": activity,
        },
    }


def _bubble_op(
    soul_id: str, text: str, kind: str, solicited: bool = False, payload=None
) -> dict:
    """Transient speech-bubble op (issue #30): not part of the entity
    state model -- the client renders it above the soul's orb and drops
    it after the kind's auto-dismiss duration. Never diffed, never in
    snapshots, never replayed on resume.

    payload is opaque handler data (issue #32: {"question_id": ...} on
    mailbag question bubbles, so the client's tap handler knows which
    question was tapped).
    """
    op = {
        "op": "bubble",
        "soul_id": soul_id,
        "text": text,
        "kind": kind,
        "solicited": solicited,
    }
    if isinstance(payload, dict):
        op["payload"] = dict(payload)
    return op


def _abroad_op(summary: dict) -> dict:
    """Wire op for the abroad channel (issue #35): the op discriminator
    plus exactly the 4-key allowlist summary -- no position, no
    viewport, no biology."""
    return {
        "op": "abroad",
        "soul_id": summary["entity_id"],
        "entity_id": summary["entity_id"],
        "state": summary["state"],
        "activity_label": summary["activity_label"],
        "plot": summary["plot"],
    }


def _abroad_end_op(soul_id: str) -> dict:
    """Channel-exit op: the soul is back on the normal stream; the
    client drops its away state (the snap move op in the same frame
    re-creates the track)."""
    return {"op": "abroad_end", "soul_id": soul_id, "entity_id": soul_id}


def read_abroad() -> dict[str, dict]:
    """Abroad-channel summaries (issue #35) keyed by soul_id.

    Only souls in the abroad/returning expedition phases stream here;
    their fine position, lifecycle state, dormancy, biology, and
    identity deltas are withheld from the normal stream instead.
    """
    from . import expeditions

    return expeditions.abroad_summaries()


def diff_abroad(
    current: dict[str, dict],
    session: ViewportSession,
    now: float,
) -> list[tuple[dict, str]]:
    """Diff abroad summaries into 1 Hz ops (issue #35).

    A summary is emitted when it changes or every ABROAD_CADENCE_S as a
    heartbeat. Souls that left the abroad channel are pruned from the
    committed set; their return rides the normal position stream.
    """
    from . import expeditions

    ops: list[tuple[dict, str]] = []
    for soul_id, summary in current.items():
        committed = session.committed_abroad.get(soul_id)
        last = session.abroad_last_sent.get(soul_id, 0.0)
        if committed != summary or now - last >= expeditions.ABROAD_CADENCE_S:
            ops.append((_abroad_op(summary), _DOMAIN_PRIORITY))
            session.committed_abroad[soul_id] = dict(summary)
            session.abroad_last_sent[soul_id] = now
    for soul_id in list(session.committed_abroad):
        if soul_id not in current:
            ops.append((_abroad_end_op(soul_id), _DOMAIN_PRIORITY))
            del session.committed_abroad[soul_id]
            session.abroad_last_sent.pop(soul_id, None)
    return ops


def read_positions() -> dict[str, tuple[float, float]]:
    """Position map with the read-through view: the tick's unflushed dirty
    set overlaid on the DB, so the viewport never lags a flush."""
    if _positions_provider is not None:
        return _positions_provider()
    return persistence.read_positions_through()


def read_soul_states() -> dict[str, str]:
    """Per-soul lifecycle state (normal|traveling|collapsed) for the
    viewport stream. Missing/NULL reads as 'normal'."""
    states: dict[str, str] = {}
    with database.get_db() as conn:
        rows = conn.execute("SELECT soul_id, state FROM souls").fetchall()
    for row in rows:
        states[row["soul_id"]] = row["state"] or "normal"
    return states


def read_dormancy() -> dict[str, bool]:
    """Per-soul dormancy (issue #22) for the viewport stream: derived
    from the cached essence, orthogonal to the lifecycle state."""
    from . import dormancy

    dormant: dict[str, bool] = {}
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT soul_id, COALESCE(essence, 0.0) AS essence FROM souls"
        ).fetchall()
    for row in rows:
        dormant[row["soul_id"]] = dormancy.is_dormant(row["essence"])
    return dormant


def read_biology() -> dict[str, tuple[float, float, float, float]]:
    """Per-soul biology (issue #30, step 0 of #29) for the viewport
    stream: (satiety, hydration, hp, max_hp). Missing/NULL reads as the
    healthy defaults the client used to assume locally."""
    bio: dict[str, tuple[float, float, float, float]] = {}
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT soul_id, COALESCE(satiety, 100.0) AS satiety, "
            "COALESCE(hydration, 100.0) AS hydration, "
            "COALESCE(hp, 100.0) AS hp, COALESCE(max_hp, 100.0) AS max_hp "
            "FROM souls"
        ).fetchall()
    for row in rows:
        bio[row["soul_id"]] = (
            float(row["satiety"]),
            float(row["hydration"]),
            float(row["hp"]),
            float(row["max_hp"]),
        )
    return bio


def read_identities() -> dict[str, dict[str, object]]:
    """Per-soul identity for the viewport stream (issue #31): name,
    species, level, activity -- the hover nameplate and the info card
    read these from viewport state, never from local guesses."""
    identities: dict[str, dict[str, object]] = {}
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT soul_id, name, species, level, activity FROM souls"
        ).fetchall()
    for row in rows:
        identities[row["soul_id"]] = {
            "name": row["name"] or row["soul_id"][:8],
            "species": row["species"] or "Unknown",
            "level": int(row["level"] or 1),
            "activity": row["activity"] or "idle",
        }
    return identities


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


def diff_states(
    current: dict[str, str],
    committed: dict[str, str],
) -> list[tuple[dict, str]]:
    """Diff per-soul lifecycle states; changed/new states become priority
    ops so the statue visual streams promptly."""
    ops: list[tuple[dict, str]] = []
    for soul_id, state in current.items():
        if committed.get(soul_id) != state:
            ops.append((_state_op(soul_id, state), _DOMAIN_PRIORITY))
    return ops


def diff_dormancy(
    current: dict[str, bool],
    committed: dict[str, bool],
) -> list[tuple[dict, str]]:
    """Diff per-soul dormancy (issue #22); flips become priority ops on
    the dormancy domain so the frozen-statue visual streams promptly."""
    ops: list[tuple[dict, str]] = []
    for soul_id, dormant in current.items():
        if committed.get(soul_id) != dormant:
            ops.append((_dormancy_op(soul_id, dormant), _DOMAIN_PRIORITY))
    return ops


def diff_biology(
    current: dict[str, tuple[float, float, float, float]],
    committed: dict[str, tuple[float, float, float, float]],
) -> list[tuple[dict, str]]:
    """Diff per-soul biology (issue #30); changed values become priority
    ops on the biology domain so online shader uniforms track the Hub.
    Values round to 0.1 to avoid chattering on float noise."""
    ops: list[tuple[dict, str]] = []
    for soul_id, vals in current.items():
        rounded = tuple(round(v, 1) for v in vals)
        if committed.get(soul_id) != rounded:
            sat, hyd, hp, max_hp = rounded
            ops.append((_biology_op(soul_id, sat, hyd, hp, max_hp), _DOMAIN_PRIORITY))
    return ops


def diff_identities(
    current: dict[str, tuple[str, str, int, str]],
    committed: dict[str, tuple[str, str, int, str]],
) -> list[tuple[dict, str]]:
    """Diff per-soul identity (issue #31); changed name/species/level/
    activity become priority ops on the identity domain so the
    nameplate and info card track the Hub mid-session."""
    ops: list[tuple[dict, str]] = []
    for soul_id, vals in current.items():
        if committed.get(soul_id) != vals:
            name, species, level, activity = vals
            ops.append(
                (
                    _identity_op(soul_id, name, species, level, activity),
                    _DOMAIN_PRIORITY,
                )
            )
    return ops


class ViewportSession:
    def __init__(self, owner_id: str, ring_size: int = RING_BUFFER_SIZE) -> None:
        self.conn_id = uuid.uuid4().hex
        self.owner_id = owner_id
        self.next_seq = 0
        self.ring: collections.deque = collections.deque(maxlen=ring_size)
        self.committed: dict[str, tuple[float, float]] = {}
        self.committed_states: dict[str, str] = {}
        self.committed_dormancy: dict[str, bool] = {}
        self.committed_biology: dict[str, tuple[float, float, float, float]] = {}
        self.committed_identities: dict[str, tuple[str, str, int, str]] = {}
        self.committed_abroad: dict[str, dict] = {}
        self.abroad_last_sent: dict[str, float] = {}
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
    states = read_soul_states()
    dormant = read_dormancy()
    biology = read_biology()
    identities = read_identities()
    # Issue #35: souls on the abroad channel stream summaries, not
    # positions; their fine state is withheld from the snapshot.
    abroad = read_abroad()
    away = set(abroad)
    souls = [
        {
            "soul_id": soul_id,
            "x": x,
            "y": y,
            "state": states.get(soul_id, "normal"),
            # Dormancy (issue #22) rides the snapshot so a fresh client
            # renders frozen statues immediately, without waiting for a
            # delta. SoulWireState allows extra fields.
            "dormant": dormant.get(soul_id, False),
            # Biology (issue #30, step 0 of #29) rides the snapshot so a
            # fresh client renders uniforms from authoritative values
            # immediately.
            "satiety": biology.get(soul_id, (100.0, 100.0, 100.0, 100.0))[0],
            "hydration": biology.get(soul_id, (100.0, 100.0, 100.0, 100.0))[1],
            "hp": biology.get(soul_id, (100.0, 100.0, 100.0, 100.0))[2],
            "max_hp": biology.get(soul_id, (100.0, 100.0, 100.0, 100.0))[3],
            # Identity (issue #31): the hover nameplate and info card
            # read these from viewport state.
            "name": identities.get(soul_id, {}).get("name", soul_id[:8]),
            "species": identities.get(soul_id, {}).get("species", "Unknown"),
            "level": identities.get(soul_id, {}).get("level", 1),
            "activity": identities.get(soul_id, {}).get("activity", "idle"),
        }
        for soul_id, (x, y) in positions.items()
        if soul_id not in away
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
        abroad=[abroad[sid] for sid in sorted(abroad)],
    )
    session.next_seq += 1
    session.committed = {sid: p for sid, p in positions.items() if sid not in away}
    session.committed_abroad = {sid: dict(s) for sid, s in abroad.items()}
    for sid in abroad:
        session.abroad_last_sent[sid] = time.time()
    session.committed_states = dict(states)
    session.committed_dormancy = dict(dormant)
    session.committed_biology = {
        sid: tuple(round(v, 1) for v in vals) for sid, vals in biology.items()
    }
    session.committed_identities = {
        sid: (
            str(vals.get("name", sid[:8])),
            str(vals.get("species", "Unknown")),
            int(vals.get("level", 1)),
            str(vals.get("activity", "idle")),
        )
        for sid, vals in identities.items()
    }
    session.touch()
    return frame


async def flush(
    session: ViewportSession,
    positions: dict[str, tuple[float, float]],
    tick_id: int,
    send: SendFn,
    states: dict[str, str] | None = None,
    dormant: dict[str, bool] | None = None,
    biology: dict[str, tuple[float, float, float, float]] | None = None,
    identities: dict[str, tuple[str, str, int, str]] | None = None,
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
        # Bubble ops are fire-and-forget: they go out on the wire but
        # are stripped from the ring copy so resume never replays a
        # stale bubble (see _bubble_op / notify_bubble).
        ring_ops = [op for op in ops if op.get("op") != "bubble"]
        session.ring.append(
            {
                "seq": frame["seq"],
                "base_seq": frame["base_seq"],
                "tick_id": tick_id,
                "ops": ring_ops,
            }
        )
        session.next_seq += 1
        session.committed = dict(positions)
        if states is not None:
            session.committed_states = dict(states)
        if dormant is not None:
            session.committed_dormancy = dict(dormant)
        if biology is not None:
            session.committed_biology = {
                sid: tuple(round(v, 1) for v in vals) for sid, vals in biology.items()
            }
        if identities is not None:
            session.committed_identities = dict(identities)
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

    def notify_bubble(
        self,
        owner_id: str,
        soul_id: str,
        text: str,
        kind: str = "speech",
        solicited: bool = False,
        payload: dict | None = None,
    ) -> int:
        """Fan a transient speech-bubble op (issue #30) to an owner's
        sessions. Bubbles are fire-and-forget: never diffed, never in
        snapshots, never replayed on resume."""
        count = 0
        for session in self.sessions_for_owner(owner_id):
            session.enqueue(
                _bubble_op(soul_id, text, kind, solicited, payload),
                _DOMAIN_PRIORITY,
            )
            count += 1
        return count

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
            session.committed_states = dict(old.committed_states)
            session.committed_dormancy = dict(old.committed_dormancy)
            session.committed_biology = dict(old.committed_biology)
            session.committed_identities = dict(old.committed_identities)
            session.touch()
        if old is not session:
            self.drop(old.conn_id)
        return "replayed"


viewport = ViewportManager()
