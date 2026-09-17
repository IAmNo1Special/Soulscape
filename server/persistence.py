"""Durable world persistence for the Hub (issue #16).

Write-behind dirty set, typed append-only event journal, compressed
periodic snapshots, and boot recovery with downtime catch-up.

RPO (recovery point objective)
------------------------------
* Soul positions: <= FLUSH_INTERVAL_S (5 s) plus WAL fsync. The 5 Hz tick
  integrates motion into an in-memory dirty set; the set is flushed to
  SQLite under BEGIN IMMEDIATE every 5 s or every 1000 dirty entries
  (whichever fires first), and on graceful stop().
* Adjudicated intents and journal events: RPO = 0. Adjudication commits
  the intent status, the resulting velocity/move_target, and the journal
  append in ONE transaction before the outcome is observable
  (commit-before-ack, consistent with issue #14).
* Snapshots: zlib-compressed full-state checkpoints every 5 min
  (1500 ticks at 5 Hz); the last 3 are kept.

Boot recovery: load the latest good snapshot, verify journal continuity
from its journal_seq against the journal head (no gaps; on gap fall back
to an older snapshot, else full rebuild from the DB), replay the journal
tail (idempotent), reconcile pending durable intents and open escrows,
then catch up downtime: gaps <= 60 s fast-forward tick-exact; longer
gaps use analytic closed-form integration capped at 24 h of simulated
time.

The tick loop owns all of this. No new threads, no network calls, no LLM
calls on any path here.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
import zlib

from . import database

logger = logging.getLogger("soulscape_hub")

FLUSH_INTERVAL_S = float(os.getenv("SOULSCAPE_FLUSH_INTERVAL_S", "5.0"))
FLUSH_MAX_DIRTY = 1000
SNAPSHOT_EVERY_TICKS = int(os.getenv("SOULSCAPE_SNAPSHOT_EVERY_TICKS", "1500"))
SNAPSHOT_KEEP = 3
CATCHUP_FAST_FORWARD_S = 60.0
CATCHUP_MAX_S = 24.0 * 3600.0
POSITION_MARGIN = 10.0
ARRIVAL_EPS = 2.0
_JOURNAL_TAIL_LIMIT = 200000

EVENT_INTENT_ADJUDICATED = "intent_adjudicated"
EVENT_INTENT_REJECTED = "intent_rejected"
EVENT_TELEPORT = "teleport"
EVENT_MODE_CHANGE = "mode_change"

_UNSET: object = object()


def integrate_soul(
    position: tuple[float, float],
    velocity: tuple[float, float],
    move_target: tuple[float, float] | None,
    tick_dt: float,
    bounds: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float] | None]:
    """One exact 5 Hz integration step for a single soul.

    Shared by the live tick and boot-recovery replay so both compute
    bit-identical trajectories from the same inputs.
    """
    x, y = position
    vx, vy = velocity
    if vx == 0.0 and vy == 0.0:
        return (x, y), (vx, vy), move_target
    width, height = bounds
    nx = min(max(x + vx * tick_dt, POSITION_MARGIN), width - POSITION_MARGIN)
    ny = min(max(y + vy * tick_dt, POSITION_MARGIN), height - POSITION_MARGIN)
    new_velocity = (vx, vy)
    new_target = move_target
    if move_target is not None:
        tx, ty = move_target
        remaining = math.hypot(tx - nx, ty - ny)
        passed = (tx - nx) * vx + (ty - ny) * vy <= 0.0
        if remaining <= ARRIVAL_EPS or passed:
            nx, ny = tx, ty
            new_velocity = (0.0, 0.0)
            new_target = None
    return (nx, ny), new_velocity, new_target


def analytic_advance(
    state: dict[str, dict],
    seconds: float,
    bounds: tuple[float, float],
) -> None:
    """Closed-form catch-up for long downtimes (mutates state in place).

    Constant-velocity straight-line motion makes this exact except for
    per-tick bound clamping, which is equivalent when applied once at the
    end of a straight segment. Arrival snaps to move_target; needs/stats
    decay is a no-op hook (the tick simulates no needs yet).
    """
    if seconds <= 0:
        return
    width, height = bounds
    for entry in state.values():
        x, y = entry["position"]
        vx, vy = entry["velocity"]
        target = entry["move_target"]
        if vx == 0.0 and vy == 0.0:
            continue
        if target is not None:
            tx, ty = target
            dist = math.hypot(tx - x, ty - y)
            speed = math.hypot(vx, vy)
            if dist <= ARRIVAL_EPS or speed <= 0.0:
                entry["position"] = [tx, ty]
                entry["velocity"] = [0.0, 0.0]
                entry["move_target"] = None
                continue
            if dist / speed <= seconds:
                entry["position"] = [tx, ty]
                entry["velocity"] = [0.0, 0.0]
                entry["move_target"] = None
                continue
            x += vx * seconds
            y += vy * seconds
        else:
            x += vx * seconds
            y += vy * seconds
        entry["position"] = [
            min(max(x, POSITION_MARGIN), width - POSITION_MARGIN),
            min(max(y, POSITION_MARGIN), height - POSITION_MARGIN),
        ]


class DirtySet:
    """Thread-safe in-memory write-behind map.

    Keys are soul_ids; values are complete triples
    {"position": [x, y], "velocity": [vx, vy], "move_target": [tx, ty] | None}.
    The tick loop is the only writer; viewport/REST readers overlay it.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}

    def mark(
        self,
        soul_id: str,
        position: list[float] | tuple[float, float] | None = None,
        velocity: list[float] | tuple[float, float] | None = None,
        move_target: list[float] | tuple[float, float] | None | object = _UNSET,
    ) -> None:
        with self._lock:
            entry = self._entries.setdefault(soul_id, {})
            if position is not None:
                entry["position"] = [float(position[0]), float(position[1])]
            if velocity is not None:
                entry["velocity"] = [float(velocity[0]), float(velocity[1])]
            if move_target is not _UNSET:
                entry["move_target"] = (
                    None
                    if move_target is None
                    else [float(move_target[0]), float(move_target[1])]
                )

    def get(self, soul_id: str) -> dict | None:
        with self._lock:
            entry = self._entries.get(soul_id)
            return dict(entry) if entry is not None else None

    def drop(self, soul_id: str) -> None:
        with self._lock:
            self._entries.pop(soul_id, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def drain(self) -> dict[str, dict]:
        with self._lock:
            entries = self._entries
            self._entries = {}
            return entries

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {soul_id: dict(entry) for soul_id, entry in self._entries.items()}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


dirty = DirtySet()


def _parse_pair_strict(raw: object) -> tuple[float, float]:
    if raw is None:
        return (0.0, 0.0)
    if isinstance(raw, str):
        raw = json.loads(raw)
    x, y = float(raw[0]), float(raw[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("non-finite pair")
    return (x, y)


def _parse_target(raw: object) -> list[float] | None:
    if not raw:
        return None
    try:
        x, y = _parse_pair_strict(raw)
    except (ValueError, TypeError, IndexError):
        return None
    return [x, y]


def flush_dirty(conn: sqlite3.Connection, tick_id: int) -> int:
    """Flush the dirty set to SQLite under BEGIN IMMEDIATE.

    Always refreshes the last-tick watermarks so boot can compute
    downtime even when nothing was dirty. Returns flushed soul count.
    """
    entries = dirty.drain()
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if entries:
            ids = list(entries.keys())
            placeholders = ",".join("?" for _ in ids)
            db_rows = {
                row["soul_id"]: row
                for row in conn.execute(
                    "SELECT soul_id, position, velocity, move_target "
                    f"FROM souls WHERE soul_id IN ({placeholders})",
                    ids,
                )
            }
            rows = []
            for soul_id in ids:
                db_row = db_rows.get(soul_id)
                if db_row is None:
                    continue
                entry = entries[soul_id]
                try:
                    pos = entry.get("position") or list(
                        _parse_pair_strict(db_row["position"])
                    )
                    vel = entry.get("velocity") or list(
                        _parse_pair_strict(db_row["velocity"])
                    )
                except (ValueError, TypeError, IndexError):
                    continue
                target = entry.get("move_target", _UNSET)
                if target is _UNSET:
                    target = _parse_target(db_row["move_target"])
                rows.append(
                    (
                        json.dumps(pos),
                        json.dumps(vel),
                        json.dumps(target) if target is not None else None,
                        soul_id,
                    )
                )
            if rows:
                conn.executemany(
                    "UPDATE souls SET position = ?, velocity = ?, "
                    "move_target = ? WHERE soul_id = ?",
                    rows,
                )
        conn.execute(
            "INSERT OR REPLACE INTO globals (key, value) VALUES "
            "('last_tick_at', ?), ('last_tick_id', ?)",
            (now, float(tick_id)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        for soul_id, entry in entries.items():
            dirty.mark(
                soul_id,
                position=entry.get("position"),
                velocity=entry.get("velocity"),
                move_target=entry.get("move_target", _UNSET),
            )
        raise
    return len(entries)


def last_tick_meta(conn: sqlite3.Connection) -> tuple[float | None, int]:
    """Return (last_tick_at, last_tick_id) watermarks, (None, 0) if unset."""
    rows = {
        row["key"]: row["value"]
        for row in conn.execute(
            "SELECT key, value FROM globals WHERE key IN "
            "('last_tick_at', 'last_tick_id')"
        )
    }
    at = rows.get("last_tick_at")
    tick_id = rows.get("last_tick_id")
    return (
        float(at) if at is not None else None,
        int(tick_id) if tick_id is not None else 0,
    )


def append_event(
    conn: sqlite3.Connection,
    tick_id: int,
    event_type: str,
    payload: dict,
) -> int:
    """Append one typed event to the journal inside the caller's transaction."""
    cursor = conn.execute(
        "INSERT INTO journal (tick_id, type, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (tick_id, event_type, json.dumps(payload), time.time()),
    )
    return int(cursor.lastrowid)


def journal_head(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS head FROM journal").fetchone()
    return int(row["head"])


def journal_continuous(
    conn: sqlite3.Connection, after_seq: int, head: int
) -> bool:
    """True when every seq in (after_seq, head] is present exactly once."""
    if head <= after_seq:
        return True
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM journal WHERE seq > ?", (after_seq,)
    ).fetchone()
    return int(row["n"]) == head - after_seq


def journal_tail(
    conn: sqlite3.Connection, after_seq: int, limit: int = _JOURNAL_TAIL_LIMIT
) -> list[dict]:
    rows = conn.execute(
        "SELECT seq, tick_id, type, payload, created_at FROM journal "
        "WHERE seq > ? ORDER BY seq ASC LIMIT ?",
        (after_seq, limit),
    ).fetchall()
    events = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("journal seq %d has corrupt payload; skipping", row["seq"])
            continue
        events.append(
            {
                "seq": int(row["seq"]),
                "tick_id": int(row["tick_id"]),
                "type": str(row["type"]),
                "payload": payload,
                "created_at": float(row["created_at"]),
            }
        )
    return events


def journal_tail_count(conn: sqlite3.Connection, after_seq: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM journal WHERE seq > ?", (after_seq,)
    ).fetchone()
    return int(row["n"])


def read_state_through(conn: sqlite3.Connection) -> dict[str, dict]:
    """Full soul kinematic state: DB rows overlaid with the dirty set."""
    state: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT soul_id, position, velocity, move_target FROM souls"
    ):
        try:
            pos = list(_parse_pair_strict(row["position"]))
            vel = list(_parse_pair_strict(row["velocity"]))
        except (ValueError, TypeError, IndexError):
            continue
        state[row["soul_id"]] = {
            "position": pos,
            "velocity": vel,
            "move_target": _parse_target(row["move_target"]),
        }
    for soul_id, entry in dirty.snapshot().items():
        if soul_id not in state:
            continue
        if entry.get("position") is not None:
            state[soul_id]["position"] = list(entry["position"])
        if entry.get("velocity") is not None:
            state[soul_id]["velocity"] = list(entry["velocity"])
        if "move_target" in entry:
            target = entry["move_target"]
            state[soul_id]["move_target"] = (
                None if target is None else [float(target[0]), float(target[1])]
            )
    return state


def take_snapshot(
    conn: sqlite3.Connection, tick_id: int, state: dict[str, dict] | None = None
) -> int:
    """Write a zlib-compressed world snapshot; returns snapshot_id."""
    if state is None:
        state = read_state_through(conn)
    blob = zlib.compress(
        json.dumps(
            {
                "v": 1,
                "tick_id": tick_id,
                "journal_seq": journal_head(conn),
                "created_at": time.time(),
                "bounds": list(database.SCREEN_BOUNDS),
                "souls": state,
            },
            separators=(",", ":"),
        ).encode("utf-8"),
        6,
    )
    cursor = conn.execute(
        "INSERT INTO snapshots (tick_id, journal_seq, blob, created_at) "
        "VALUES (?, ?, ?, ?)",
        (tick_id, journal_head(conn), blob, time.time()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def latest_snapshots(conn: sqlite3.Connection, limit: int = SNAPSHOT_KEEP) -> list[dict]:
    rows = conn.execute(
        "SELECT snapshot_id, tick_id, journal_seq, created_at FROM snapshots "
        "ORDER BY snapshot_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def load_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> dict | None:
    """Decompress and validate a snapshot blob; None when corrupt."""
    row = conn.execute(
        "SELECT blob FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        snap = json.loads(zlib.decompress(bytes(row["blob"])).decode("utf-8"))
        assert snap["v"] == 1 and isinstance(snap["souls"], dict)
        return snap
    except Exception:
        logger.warning("snapshot %d failed integrity check", snapshot_id)
        return None


def prune_snapshots(conn: sqlite3.Connection, keep: int = SNAPSHOT_KEEP) -> int:
    cursor = conn.execute(
        "DELETE FROM snapshots WHERE snapshot_id NOT IN "
        "(SELECT snapshot_id FROM snapshots ORDER BY snapshot_id DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    return cursor.rowcount


def apply_event(state: dict[str, dict], event: dict) -> None:
    """Re-apply one journal event to in-memory state. Idempotent: applying
    an already-applied outcome is a no-op because outcomes are absolute
    assignments, not deltas."""
    event_type = event["type"]
    payload = event["payload"]
    if event_type == EVENT_INTENT_ADJUDICATED:
        entry = state.get(payload.get("soul_id", ""))
        if entry is None:
            return
        entry["velocity"] = [float(payload["velocity"][0]), float(payload["velocity"][1])]
        target = payload.get("move_target")
        entry["move_target"] = (
            None if target is None else [float(target[0]), float(target[1])]
        )
    elif event_type == EVENT_TELEPORT:
        entry = state.get(payload.get("soul_id", ""))
        if entry is None:
            return
        entry["position"] = [float(payload["to"][0]), float(payload["to"][1])]
        entry["velocity"] = [0.0, 0.0]
        entry["move_target"] = None
    elif event_type == EVENT_MODE_CHANGE:
        entry = state.get(payload.get("soul_id", ""))
        if entry is None:
            return
        entry["mode"] = payload.get("mode")
    elif event_type == EVENT_INTENT_REJECTED:
        pass


def replay_tail(
    state: dict[str, dict],
    events: list[dict],
    base_tick: int,
    sim_seconds: float,
    tick_dt: float,
    bounds: tuple[float, float],
    exact: bool,
) -> int:
    """Replay journal tail over state from base_tick forward.

    Events apply at their recorded tick_id (absolute outcome assignment,
    so replay is deterministic and idempotent). exact=True integrates
    per tick; exact=False advances analytically in segments between
    event ticks. Returns the end tick_id.
    """
    end_tick = base_tick + int(round(sim_seconds / tick_dt))
    by_tick: dict[int, list[dict]] = {}
    for event in events:
        by_tick.setdefault(int(event["tick_id"]), []).append(event)
    for tick in sorted(t for t in by_tick if t < base_tick):
        for event in by_tick.pop(tick):
            apply_event(state, event)
    if exact:
        for tick in range(base_tick, end_tick):
            for event in by_tick.get(tick, []):
                apply_event(state, event)
            for entry in state.values():
                pos, vel, target = integrate_soul(
                    (entry["position"][0], entry["position"][1]),
                    (entry["velocity"][0], entry["velocity"][1]),
                    (
                        (entry["move_target"][0], entry["move_target"][1])
                        if entry["move_target"] is not None
                        else None
                    ),
                    tick_dt,
                    bounds,
                )
                entry["position"] = [pos[0], pos[1]]
                entry["velocity"] = [vel[0], vel[1]]
                entry["move_target"] = (
                    None if target is None else [target[0], target[1]]
                )
        for tick in sorted(t for t in by_tick if t >= end_tick):
            for event in by_tick[tick]:
                apply_event(state, event)
    else:
        prev_tick = base_tick
        for tick in sorted(t for t in by_tick if base_tick <= t <= end_tick):
            analytic_advance(state, (tick - prev_tick) * tick_dt, bounds)
            for event in by_tick[tick]:
                apply_event(state, event)
            prev_tick = tick
        analytic_advance(state, (end_tick - prev_tick) * tick_dt, bounds)
        for tick in sorted(t for t in by_tick if t > end_tick):
            for event in by_tick[tick]:
                apply_event(state, event)
    return end_tick


def reconcile_escrows(conn: sqlite3.Connection) -> int:
    """Boot sweep for open escrows.

    No escrow concept exists in the schema yet (marketplace escrows land
    with issue #17); this is the hook the boot reconciler calls. When a
    table named `escrows` exists, open rows are counted and logged so a
    future settlement pass has a defined entry point.
    """
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "escrows" not in tables:
        logger.info("reconcile_escrows: no escrows table yet (issue #17 hook)")
        return 0
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(escrows)")
    }
    if "status" not in cols:
        logger.warning("reconcile_escrows: escrows table has no status column")
        return 0
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM escrows WHERE status = 'open'"
    ).fetchone()
    count = int(row["n"])
    if count:
        logger.warning(
            "reconcile_escrows: %d open escrows found; settlement "
            "semantics belong to issue #17",
            count,
        )
    return count


def recover_world(tick, now: float | None = None) -> dict:
    """Boot recovery for authoritative mode. Restores the world into the
    DB, sets tick.tick_id to the recovered tick, and returns a report.

    1. Pick the newest snapshot whose blob verifies AND whose journal
       tail (snapshot.journal_seq -> head) is gap-free. Older snapshots
       are tried in turn; with none usable, fall back to a full rebuild
       from the DB rows (adjudication outcomes are DB-durable, RPO=0).
    2. Base state = snapshot souls, unioned with DB souls registered
       after the snapshot, minus souls deleted since.
    3. Replay the journal tail; catch up the downtime gap:
       <= 60 s -> tick-exact fast-forward; longer -> analytic segments,
       total simulated time capped at 24 h.
    4. Write the recovered state back under BEGIN IMMEDIATE, refresh the
       tick watermarks, reconcile escrows.
    """
    tick_dt = tick.tick_dt
    bounds = database.SCREEN_BOUNDS
    now = time.time() if now is None else now
    dirty.clear()
    with database.get_db() as conn:
        head = journal_head(conn)
        snapshot = None
        snap_blob = None
        for candidate in latest_snapshots(conn):
            blob = load_snapshot(conn, candidate["snapshot_id"])
            if blob is None:
                continue
            if not journal_continuous(conn, candidate["journal_seq"], head):
                logger.warning(
                    "snapshot %d has a journal gap after seq %d; trying older",
                    candidate["snapshot_id"],
                    candidate["journal_seq"],
                )
                continue
            if journal_tail_count(conn, candidate["journal_seq"]) >= (
                _JOURNAL_TAIL_LIMIT
            ):
                logger.warning(
                    "snapshot %d tail too long; trying older", candidate["snapshot_id"]
                )
                continue
            snapshot = candidate
            snap_blob = blob
            break
        if snapshot is not None:
            state = {
                soul_id: {
                    "position": [float(e["position"][0]), float(e["position"][1])],
                    "velocity": [float(e["velocity"][0]), float(e["velocity"][1])],
                    "move_target": (
                        None
                        if e.get("move_target") is None
                        else [float(e["move_target"][0]), float(e["move_target"][1])]
                    ),
                }
                for soul_id, e in snap_blob["souls"].items()
            }
            db_ids = {
                row["soul_id"]
                for row in conn.execute("SELECT soul_id FROM souls")
            }
            for soul_id in db_ids - set(state):
                row = conn.execute(
                    "SELECT position, velocity, move_target FROM souls "
                    "WHERE soul_id = ?",
                    (soul_id,),
                ).fetchone()
                try:
                    state[soul_id] = {
                        "position": list(_parse_pair_strict(row["position"])),
                        "velocity": list(_parse_pair_strict(row["velocity"])),
                        "move_target": _parse_target(row["move_target"]),
                    }
                except (ValueError, TypeError, IndexError):
                    continue
            for soul_id in set(state) - db_ids:
                del state[soul_id]
            base_tick = int(snapshot["tick_id"])
            base_time = float(snapshot["created_at"])
            tail = journal_tail(conn, int(snapshot["journal_seq"]))
            mode = "snapshot"
            snapshot_id = int(snapshot["snapshot_id"])
        else:
            state = read_state_through(conn)
            _, base_tick = last_tick_meta(conn)
            last_at, _ = last_tick_meta(conn)
            base_time = last_at if last_at is not None else now
            tail = []
            mode = "rebuild"
            snapshot_id = None
        gap = max(0.0, now - base_time)
        sim_seconds = min(gap, CATCHUP_MAX_S)
        exact = gap <= CATCHUP_FAST_FORWARD_S
        end_tick = replay_tail(
            state, tail, base_tick, sim_seconds, tick_dt, bounds, exact=exact
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = [
                (
                    json.dumps(entry["position"]),
                    json.dumps(entry["velocity"]),
                    json.dumps(entry["move_target"])
                    if entry["move_target"] is not None
                    else None,
                    soul_id,
                )
                for soul_id, entry in state.items()
            ]
            if rows:
                conn.executemany(
                    "UPDATE souls SET position = ?, velocity = ?, "
                    "move_target = ? WHERE soul_id = ?",
                    rows,
                )
            conn.execute(
                "INSERT OR REPLACE INTO globals (key, value) VALUES "
                "('last_tick_at', ?), ('last_tick_id', ?)",
                (now, float(end_tick)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        escrows = reconcile_escrows(conn)
    tick.tick_id = end_tick
    if hasattr(tick, "_last_snapshot_tick"):
        tick._last_snapshot_tick = end_tick
    report = {
        "mode": mode,
        "snapshot_id": snapshot_id,
        "regime": "fast_forward" if exact else "analytic",
        "gap_seconds": round(gap, 3),
        "sim_seconds": round(sim_seconds, 3),
        "journal_replayed": len(tail),
        "souls": len(state),
        "escrows_open": escrows,
        "tick_id": end_tick,
    }
    logger.info("boot recovery complete: %s", report)
    return report


def dirty_get(soul_id: str) -> dict | None:
    """Newest unflushed triple for a soul, or None."""
    return dirty.get(soul_id)


def invalidate(soul_id: str) -> None:
    """Drop a soul's dirty entry (REST upserts write the DB directly and
    are newer than anything unflushed)."""
    dirty.drop(soul_id)


def overlay_positions(
    base: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    """Overlay unflushed dirty positions onto a DB-read position map."""
    for soul_id, entry in dirty.snapshot().items():
        position = entry.get("position")
        if position is not None and soul_id in base:
            base[soul_id] = (float(position[0]), float(position[1]))
    return base


def read_positions_db() -> dict[str, tuple[float, float]]:
    """Position map straight from the DB (no dirty overlay)."""
    positions: dict[str, tuple[float, float]] = {}
    with database.get_db() as conn:
        rows = conn.execute("SELECT soul_id, position FROM souls").fetchall()
    for row in rows:
        try:
            positions[row["soul_id"]] = _parse_pair_strict(row["position"])
        except (ValueError, TypeError, IndexError):
            continue
    return positions


def read_positions_through() -> dict[str, tuple[float, float]]:
    """Position map with the read-through view: dirty set over DB."""
    return overlay_positions(read_positions_db())
