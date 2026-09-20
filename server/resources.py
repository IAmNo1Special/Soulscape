"""Resource nodes, gathering, inventory, and XP (issue #34).

Server-adjudicated foraging economy: seeded resource nodes spawn on the
plot grid, souls gather units into a per-soul inventory via the `gather`
intent, and `eat`/`drink` consume inventory to restore needs. Selling
flows through the #17 marketplace intents (market.py owns the
essence/escrow/ledger path; this module owns the item hold/transfer).
Activity completions accrue XP to souls.xp silently (telemetry only --
no UI, no bubbles, no client surface).

Rules (decided for #34; code is the source of truth):
- Density: 1 food node per 4 plots, 1 water node per 6 plots.
- Spacing: minimum 128 wu between any two nodes (Euclidean).
- Placement: seeded RNG (seed source: SOULSCAPE_RESOURCE_SEED env, else
  RESOURCE_NODE_SEED below; the used seed is stored in globals as
  `resource_node_seed`). Nodes sit near their plot center with a small
  seeded jitter. Claimed private plots are EXCLUDED: generation skips
  them, and a successful plot_claim deletes any nodes on the plot
  (plots.py hook). The origin Commons is included -- newborns forage
  where they spawn.
- Respawn: a node depleted to 0 regrows to full capacity RESPAWN_SECONDS
  (6 h) later. No partial regrow in v1. The world tick sweeps every
  RESPAWN_SWEEP_EVERY_TICKS (100 ticks, 0.05 Hz per the arch).
- Gather: 1 unit per `gather` intent; proximity gate GATHER_REACH_WU
  (12 wu) between soul and node, adjudicated server-side.
- Eat/drink: consume 1 inventory unit -> +EAT_SATIETY_GAIN satiety /
  +DRINK_HYDRATION_GAIN hydration (consume.py keeps the gain constants).
- XP (silent): gather +5, eat +2, drink +2, completed sale +10.
- Inventory items: {"food", "water"} only, stored in the existing
  soul_inventory table (one row per soul+item).
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import sqlite3
import time

from . import database
from . import dormancy
from . import inventory
from . import persistence

logger = logging.getLogger("soulscape_hub")

#: Intent kinds routed to adjudicate_gather by the tick pump.
KIND_GATHER = "gather"
RESOURCE_KINDS = frozenset({KIND_GATHER})

#: Node kinds.
KIND_FOOD = "food"
KIND_WATER = "water"
NODE_KINDS = (KIND_FOOD, KIND_WATER)

#: Inventory item names (subset of node kinds by design).
ITEM_FOOD = "food"
ITEM_WATER = "water"
ITEMS = (ITEM_FOOD, ITEM_WATER)

#: Density: one node per N plots.
FOOD_PER_PLOTS = 4
WATER_PER_PLOTS = 6

#: Minimum Euclidean spacing between any two nodes, in world units.
MIN_NODE_SPACING_WU = 128.0

#: Node capacity (units) and yield per gather.
NODE_CAPACITY = 10
GATHER_YIELD = 1

#: Seconds for a depleted node to regrow to full.
RESPAWN_SECONDS = 6.0 * 3600.0

#: Tick cadence for the respawn sweep (0.05 Hz at the 5 Hz world tick).
RESPAWN_SWEEP_EVERY_TICKS = 100

#: Adjudication proximity gate: soul must be within this distance (wu)
#: of the node to gather. The reflex emits gather inside its own
#: CONSUME_REACH_WU (8 wu); the gate is deliberately a little wider so
#: a valid emission never fails adjudication on float rounding.
GATHER_REACH_WU = 12.0

#: Seeded-RNG default for node placement. Override with the
#: SOULSCAPE_RESOURCE_SEED env var. The seed actually used is stored in
#: the globals table under SEED_GLOBAL_KEY.
RESOURCE_NODE_SEED = 20260917
SEED_GLOBAL_KEY = "resource_node_seed"
SEED_ENV_VAR = "SOULSCAPE_RESOURCE_SEED"

#: XP awards (silent telemetry on souls.xp).
XP_GATHER = 5
XP_EAT = 2
XP_DRINK = 2
XP_SELL = 10

#: Node lifecycle states.
STATE_READY = "ready"
STATE_DEPLETED = "depleted"

_WS_ERROR_CODES = {
    "soul_not_found": "SOUL_NOT_FOUND",
    "collapsed": "SOUL_COLLAPSED",
    "soul_dormant": "SOUL_DORMANT",
    "node_not_found": "NODE_NOT_FOUND",
    "node_depleted": "NODE_DEPLETED",
    "too_far": "TOO_FAR",
    "bad_payload": "BAD_PAYLOAD",
    "internal": "INTERNAL",
}


class ResourceRefusal(Exception):
    """Business-logic refusal to adjudicate a gather intent."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason

    @property
    def ws_code(self) -> str:
        return _WS_ERROR_CODES.get(self.reason, "REJECTED")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the resource_nodes table (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS resource_nodes (
            node_id TEXT PRIMARY KEY,
            plot_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('food', 'water')),
            x REAL NOT NULL,
            y REAL NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            capacity INTEGER NOT NULL DEFAULT 10,
            respawns_at REAL,
            state TEXT NOT NULL DEFAULT 'ready'
                CHECK (state IN ('ready', 'depleted')),
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_nodes_kind "
        "ON resource_nodes(kind, state)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_nodes_plot ON resource_nodes(plot_id)"
    )
    conn.execute(
        f"INSERT OR IGNORE INTO globals (key, value) VALUES ('{SEED_GLOBAL_KEY}', 0.0)"
    )


# ---------------------------------------------------------------------------
# Seeding (deterministic placement)
# ---------------------------------------------------------------------------


def node_seed() -> int:
    """Seed source: env override, else the module default."""
    raw = os.getenv(SEED_ENV_VAR, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("resources: ignoring unparsable %s=%r", SEED_ENV_VAR, raw)
    return RESOURCE_NODE_SEED


def seed_resource_nodes(conn: sqlite3.Connection, seed: int | None = None) -> int:
    """Idempotently place nodes per the density/spacing rules.

    Only seeds when the table is empty (placement is stable across
    reboots). Claimed private plots (owner_id NOT NULL) are excluded.
    Returns the number of nodes placed.
    """
    from . import plots as plots_module

    ensure_schema(conn)
    existing = conn.execute("SELECT COUNT(*) AS c FROM resource_nodes").fetchone()
    if existing and existing["c"] > 0:
        return 0
    seed = node_seed() if seed is None else seed
    rng = random.Random(seed)
    cols, rows = plots_module.grid_dims()
    candidates: list[tuple[str, int, int]] = []
    claimed = {
        r["plot_id"]
        for r in conn.execute(
            "SELECT plot_id FROM plots WHERE owner_id IS NOT NULL"
        ).fetchall()
    }
    for gx in range(cols):
        for gy in range(rows):
            plot_id = plots_module.plot_id_for(gx, gy)
            if plot_id in claimed:
                continue
            candidates.append((plot_id, gx, gy))
    rng.shuffle(candidates)
    placed: list[tuple[float, float]] = []
    now = time.time()
    total = 0
    for kind, per_plots in (
        (KIND_FOOD, FOOD_PER_PLOTS),
        (KIND_WATER, WATER_PER_PLOTS),
    ):
        target = max(1, len(candidates) // per_plots)
        made = 0
        for plot_id, gx, gy in candidates:
            if made >= target:
                break
            cx, cy = plots_module.plot_center(gx, gy)
            half = plots_module.PLOT_SIZE / 2.0 - 8.0
            x = cx + rng.uniform(-half, half)
            y = cy + rng.uniform(-half, half)
            if any(
                math.hypot(x - px, y - py) < MIN_NODE_SPACING_WU for px, py in placed
            ):
                continue
            node_id = f"node:{kind}:{plot_id}"
            conn.execute(
                "INSERT OR IGNORE INTO resource_nodes "
                "(node_id, plot_id, kind, x, y, amount, capacity, "
                "respawns_at, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'ready', ?)",
                (
                    node_id,
                    plot_id,
                    kind,
                    x,
                    y,
                    NODE_CAPACITY,
                    NODE_CAPACITY,
                    now,
                ),
            )
            placed.append((x, y))
            made += 1
            total += 1
        if made < target:
            logger.warning(
                "resources: placed %d/%d %s nodes (spacing-bound)",
                made,
                target,
                kind,
            )
    conn.execute(
        "UPDATE globals SET value = ? WHERE key = ?", (float(seed), SEED_GLOBAL_KEY)
    )
    logger.info("resources: seeded %d nodes (seed %d)", total, seed)
    return total


# ---------------------------------------------------------------------------
# Node reads
# ---------------------------------------------------------------------------


def _row_to_node(row: sqlite3.Row) -> dict:
    return {
        "node_id": row["node_id"],
        "plot_id": row["plot_id"],
        "kind": row["kind"],
        "x": float(row["x"]),
        "y": float(row["y"]),
        "amount": int(row["amount"]),
        "capacity": int(row["capacity"]),
        "respawns_at": row["respawns_at"],
        "state": row["state"],
    }


def get_node(conn: sqlite3.Connection, node_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM resource_nodes WHERE node_id = ?", (node_id,)
    ).fetchone()
    return _row_to_node(row) if row else None


def nearest_node(
    conn: sqlite3.Connection, kind: str, x: float, y: float
) -> dict | None:
    """Nearest READY node of kind with amount > 0, or None."""
    row = conn.execute(
        "SELECT *, ((x - ?) * (x - ?) + (y - ?) * (y - ?)) AS d2 "
        "FROM resource_nodes "
        "WHERE kind = ? AND state = 'ready' AND amount > 0 "
        "ORDER BY d2 ASC LIMIT 1",
        (x, x, y, y, kind),
    ).fetchone()
    return _row_to_node(row) if row else None


def live_nodes(
    conn: sqlite3.Connection, kind: str, x: float, y: float
) -> list[tuple[float, float]]:
    """Positions of ready nodes of kind, nearest-first (provider view)."""
    rows = conn.execute(
        "SELECT x, y, ((x - ?) * (x - ?) + (y - ?) * (y - ?)) AS d2 "
        "FROM resource_nodes "
        "WHERE kind = ? AND state = 'ready' AND amount > 0 "
        "ORDER BY d2 ASC",
        (x, x, y, y, kind),
    ).fetchall()
    return [(float(r["x"]), float(r["y"])) for r in rows]


def node_at(
    conn: sqlite3.Connection, kind: str, x: float, y: float, tol: float
) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM resource_nodes "
            "WHERE kind = ? AND state = 'ready' AND amount > 0 "
            "AND ((x - ?) * (x - ?) + (y - ?) * (y - ?)) <= ? "
            "LIMIT 1",
            (kind, x, x, y, y, tol * tol),
        ).fetchone()
        is not None
    )


def respawn_sweep(conn: sqlite3.Connection, now: float) -> int:
    """Regrow depleted nodes whose respawns_at has passed. Returns count."""
    cursor = conn.execute(
        "UPDATE resource_nodes SET amount = capacity, state = 'ready', "
        "respawns_at = NULL "
        "WHERE state = 'depleted' AND respawns_at IS NOT NULL "
        "AND respawns_at <= ?",
        (now,),
    )
    if cursor.rowcount:
        logger.info("resources: respawned %d nodes", cursor.rowcount)
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Inventory (per-soul, items in {food, water})
# ---------------------------------------------------------------------------


def _check_item(item: str) -> None:
    if item not in ITEMS:
        raise ValueError(f"unknown inventory item: {item!r}")


def inventory_for(conn: sqlite3.Connection, soul_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT item_name, quantity FROM soul_inventory WHERE soul_id = ?",
        (soul_id,),
    ).fetchall()
    return {r["item_name"]: int(r["quantity"]) for r in rows}


def inventory_qty(conn: sqlite3.Connection, soul_id: str, item: str) -> int:
    _check_item(item)
    return inventory.qty(conn, database.ACTOR_SOUL, soul_id, item)


def add_item(conn: sqlite3.Connection, soul_id: str, item: str, qty: int) -> int:
    """Add qty units; returns the new total. qty must be positive."""
    _check_item(item)
    return inventory.add(conn, database.ACTOR_SOUL, soul_id, item, qty)


def remove_item(conn: sqlite3.Connection, soul_id: str, item: str, qty: int) -> bool:
    """Remove qty units; False (no write) when the balance is short."""
    _check_item(item)
    return inventory.remove(conn, database.ACTOR_SOUL, soul_id, item, qty)


# ---------------------------------------------------------------------------
# XP (silent telemetry on souls.xp)
# ---------------------------------------------------------------------------


def award_xp(conn: sqlite3.Connection, soul_id: str, amount: int) -> None:
    """Add XP. NULL-safe for legacy rows; no UI, no journal, no bubbles."""
    conn.execute(
        "UPDATE souls SET xp = COALESCE(xp, 0) + ? WHERE soul_id = ?",
        (int(amount), soul_id),
    )


# ---------------------------------------------------------------------------
# Marketplace bridge: resource listings hold/transfer/return inventory
# ---------------------------------------------------------------------------


def resource_listing(item: dict | None) -> tuple[str, int] | None:
    """Parse a marketplace item payload as a resource listing.

    Shape: {"type": "resource", "item": "food"|"water", "qty": int >= 1}.
    Returns (item_name, qty) or None when the item is not a resource.
    """
    if not isinstance(item, dict) or item.get("type") != "resource":
        return None
    name = item.get("item")
    qty = item.get("qty")
    if name not in ITEMS:
        return None
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return None
    if qty < 1:
        return None
    return name, qty


# ---------------------------------------------------------------------------
# Production FoodWaterProvider backed by the node table
# ---------------------------------------------------------------------------


class NodeProvider:
    """#24 FoodWaterProvider backed by live resource nodes (#34).

    This is the production provider: the reflex layer forages real nodes.
    """

    def find_food(self, x: float, y: float) -> list[tuple[float, float]]:
        with database.get_db() as conn:
            return live_nodes(conn, KIND_FOOD, x, y)

    def find_water(self, x: float, y: float) -> list[tuple[float, float]]:
        with database.get_db() as conn:
            return live_nodes(conn, KIND_WATER, x, y)

    def is_food_at(self, x: float, y: float, tol: float = 4.0) -> bool:
        with database.get_db() as conn:
            return node_at(conn, KIND_FOOD, x, y, tol)

    def is_water_at(self, x: float, y: float, tol: float = 4.0) -> bool:
        with database.get_db() as conn:
            return node_at(conn, KIND_WATER, x, y, tol)

    def nearest_food_node(self, x: float, y: float) -> dict | None:
        with database.get_db() as conn:
            return nearest_node(conn, KIND_FOOD, x, y)

    def nearest_water_node(self, x: float, y: float) -> dict | None:
        with database.get_db() as conn:
            return nearest_node(conn, KIND_WATER, x, y)

    def node_by_id(self, node_id: str) -> dict | None:
        with database.get_db() as conn:
            return get_node(conn, node_id)


def node_provider() -> NodeProvider:
    return NodeProvider()


# ---------------------------------------------------------------------------
# Gather adjudication (tick pump)
# ---------------------------------------------------------------------------


def _parse_pair(raw) -> tuple[float, float]:
    if raw is None:
        return (0.0, 0.0)
    if isinstance(raw, str):
        raw = json.loads(raw)
    x, y = float(raw[0]), float(raw[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("non-finite pair")
    return (x, y)


def adjudicate_gather(tick, intent: dict, now: float | None = None) -> None:
    """Adjudicate one gather intent inside the tick pump.

    Server-side validation of every step (no client trust): the soul
    must exist, be awake and uncollapsed; the node must exist, be ready
    with amount > 0; the soul must be within GATHER_REACH_WU of the node.
    On success one unit moves node -> inventory and XP_GATHER accrues,
    all in one BEGIN IMMEDIATE commit with the intent status + journal.
    `now` is injectable for accelerated-clock tests.
    """
    from . import biology as _biology

    now = time.time() if now is None else now
    intent_id = intent["intent_id"]
    soul_id = intent["soul_id"]
    payload = intent["payload"] or {}
    node_id = payload.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        with database.get_db() as conn:
            tick._reject(conn, intent, "bad_payload")
        return
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT position, state, COALESCE(essence, 0.0) AS essence "
            "FROM souls WHERE soul_id = ?",
            (soul_id,),
        ).fetchone()
        if row is None:
            tick._reject(conn, intent, "soul_not_found")
            return
        if (row["state"] or _biology.STATE_NORMAL) == _biology.STATE_COLLAPSED:
            tick._reject(conn, intent, "collapsed")
            return
        if dormancy.is_dormant(row["essence"]):
            tick._reject(conn, intent, "soul_dormant")
            return
        node = get_node(conn, node_id)
        if node is None:
            tick._reject(conn, intent, "node_not_found")
            return
        if node["state"] != STATE_READY or node["amount"] <= 0:
            tick._reject(conn, intent, "node_depleted")
            return
        try:
            sx, sy = _parse_pair(row["position"])
        except (ValueError, TypeError, IndexError):
            tick._reject(conn, intent, "bad_payload")
            return
        if math.hypot(node["x"] - sx, node["y"] - sy) > GATHER_REACH_WU:
            tick._reject(conn, intent, "too_far")
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            fresh = get_node(conn, node_id)
            if fresh is None or fresh["state"] != STATE_READY or fresh["amount"] <= 0:
                conn.rollback()
                tick._reject(conn, intent, "node_depleted")
                return
            new_amount = fresh["amount"] - GATHER_YIELD
            if new_amount <= 0:
                conn.execute(
                    "UPDATE resource_nodes SET amount = 0, state = 'depleted', "
                    "respawns_at = ? WHERE node_id = ?",
                    (now + RESPAWN_SECONDS, node_id),
                )
                depleted = True
            else:
                conn.execute(
                    "UPDATE resource_nodes SET amount = ? WHERE node_id = ?",
                    (new_amount, node_id),
                )
                depleted = False
            add_item(conn, soul_id, fresh["kind"], GATHER_YIELD)
            award_xp(conn, soul_id, XP_GATHER)
            result = {
                "node_id": node_id,
                "kind": fresh["kind"],
                "yield": GATHER_YIELD,
                "node_amount": max(0, new_amount),
                "depleted": depleted,
                "xp": XP_GATHER,
            }
            conn.execute(
                "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                ("adjudicated", json.dumps(result), intent_id),
            )
            persistence.append_event(
                conn,
                tick.tick_id,
                persistence.EVENT_INTENT_ADJUDICATED,
                {
                    "intent_id": intent_id,
                    "kind": KIND_GATHER,
                    "soul_id": soul_id,
                    "params": result,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
