"""Square-plot territory layer (issue #19).

The world (SCREEN_BOUNDS) is divided into PLOT_SIZE square plots. The
origin plot at the grid center is the unclaimable origin Commons where
newborn Souls materialize. Whole rings divisible by 3 are road rings:
unclaimable and always traversable, so the grid can never partition
connectivity. Every other ring >= 1 is claimable.

Allocation is driven by a monotonic claim counter (`plot_claim_seq` in
the globals table, persisted, never recomputed). The nth successful
claim takes the next plot in deterministic ring order (ring 1's plots
in ascending (grid_x, grid_y) order, then ring 2, ...), skipping the
commons, road rings, and already-claimed plots. The pure function
plot_for_claim_seq(n) maps a sequence number to its plot.

plot_claim is a PAID intent: the ring-scaled fee
BASE_CLAIM_FEE * (ring + 1) is held in escrow at enqueue (estimated
from the plot the claim would take if adjudicated immediately) and
debited into the Essence Fund at adjudication -- a debit row on the
claimant plus a tax-style row to the fund, reusing the #17 ledger entry
types. Plot row update + counter increment + escrow apply + ledger rows
commit in ONE adjudication transaction; any failure rolls back,
releases the escrow, and rejects the intent.

Access policies: 'open' (anyone may enter) or 'closed' (owner and
operator only). Enforced in the movement path: move_to adjudication
rejects targets inside a closed plot the soul may not enter, and the
per-tick integration stops a soul at the boundary (position reverted,
velocity zeroed).
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import sqlite3
import time
from typing import Any

from . import database
from . import persistence
from .intents import _row_to_dict as _intent_row_to_dict
from .market import LEDGER_DEBIT, LEDGER_TAX

logger = logging.getLogger("soulscape_hub")

PLOT_SIZE = 120.0

KIND_PLOT_CLAIM = "plot_claim"
PLOT_KINDS = {KIND_PLOT_CLAIM}

BASE_CLAIM_FEE = 50.00

SEQ_GLOBAL_KEY = "plot_claim_seq"

KIND_COMMONS = "commons"
KIND_ROAD = "road"
KIND_CLAIMABLE = "claimable"

ACCESS_OPEN = "open"
ACCESS_CLOSED = "closed"
ACCESS_POLICIES = (ACCESS_OPEN, ACCESS_CLOSED)

OWNER_SOUL = "soul"

ESCROW_HELD = "held"

_WS_ERROR_CODES = {
    "claimant_not_found": "SOUL_NOT_FOUND",
    "insufficient_funds": "INSUFFICIENT_FUNDS",
    "no_plots_left": "NO_PLOTS_LEFT",
    "plot_taken": "PLOT_TAKEN",
    "custody": "CUSTODY_DENIED",
    "escrow_missing": "ESCROW_MISSING",
    "bad_payload": "BAD_PAYLOAD",
    "internal": "INTERNAL",
}


class PlotRefusal(Exception):
    """Business-logic refusal to enqueue or adjudicate a plot intent."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason

    @property
    def ws_code(self) -> str:
        return _WS_ERROR_CODES.get(self.reason, "INTERNAL")


def grid_dims(
    bounds: tuple[float, float] | None = None,
) -> tuple[int, int]:
    width, height = bounds or database.SCREEN_BOUNDS
    return max(1, math.ceil(width / PLOT_SIZE)), max(1, math.ceil(height / PLOT_SIZE))


def origin_plot(
    bounds: tuple[float, float] | None = None,
) -> tuple[int, int]:
    cols, rows = grid_dims(bounds)
    return cols // 2, rows // 2


def plot_id_for(grid_x: int, grid_y: int) -> str:
    return f"{grid_x}:{grid_y}"


def plot_center(grid_x: int, grid_y: int) -> tuple[float, float]:
    return ((grid_x + 0.5) * PLOT_SIZE, (grid_y + 0.5) * PLOT_SIZE)


def commons_center(
    bounds: tuple[float, float] | None = None,
) -> tuple[float, float]:
    return plot_center(*origin_plot(bounds))


def plot_at(
    x: float, y: float, bounds: tuple[float, float] | None = None
) -> tuple[int, int]:
    cols, rows = grid_dims(bounds)
    gx = min(max(int(x // PLOT_SIZE), 0), cols - 1)
    gy = min(max(int(y // PLOT_SIZE), 0), rows - 1)
    return gx, gy


def ring_of(
    grid_x: int, grid_y: int, bounds: tuple[float, float] | None = None
) -> int:
    ox, oy = origin_plot(bounds)
    return max(abs(grid_x - ox), abs(grid_y - oy))


def is_road_ring(ring: int) -> bool:
    return ring > 0 and ring % 3 == 0


def kind_of(
    grid_x: int, grid_y: int, bounds: tuple[float, float] | None = None
) -> str:
    ring = ring_of(grid_x, grid_y, bounds)
    if ring == 0:
        return KIND_COMMONS
    if is_road_ring(ring):
        return KIND_ROAD
    return KIND_CLAIMABLE


def max_ring(bounds: tuple[float, float] | None = None) -> int:
    ox, oy = origin_plot(bounds)
    cols, rows = grid_dims(bounds)
    return max(ox, oy, cols - 1 - ox, rows - 1 - oy)


def allocation_order(
    bounds: tuple[float, float] | None = None,
) -> list[str]:
    """Claimable plot ids in allocation order: ring 1, ring 2, ...,
    skipping the commons and road rings; ascending (grid_x, grid_y)
    within a ring. Pure and deterministic."""
    cols, rows = grid_dims(bounds)
    ox, oy = origin_plot(bounds)
    order: list[str] = []
    for ring in range(1, max_ring(bounds) + 1):
        if is_road_ring(ring):
            continue
        cells = [
            (gx, gy)
            for gx in range(ox - ring, ox + ring + 1)
            for gy in range(oy - ring, oy + ring + 1)
            if max(abs(gx - ox), abs(gy - oy)) == ring
            and 0 <= gx < cols
            and 0 <= gy < rows
        ]
        cells.sort()
        order.extend(plot_id_for(gx, gy) for gx, gy in cells)
    return order


def plot_for_claim_seq(
    n: int, bounds: tuple[float, float] | None = None
) -> str | None:
    """Pure allocation formula: the plot the nth claim takes. None when
    n is out of range (no plots left)."""
    if n < 1:
        return None
    order = allocation_order(bounds)
    if n > len(order):
        return None
    return order[n - 1]


def claim_fee(ring: int) -> float:
    """Ring-scaled claim fee debited into the Essence Fund."""
    return round(BASE_CLAIM_FEE * (ring + 1), 2)


def seed_plots(conn: sqlite3.Connection) -> int:
    """Idempotently seed the plots table for the current grid."""
    cols, rows = grid_dims()
    ox, oy = origin_plot()
    seeded = 0
    for gx in range(cols):
        for gy in range(rows):
            ring = max(abs(gx - ox), abs(gy - oy))
            kind = kind_of(gx, gy)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO plots "
                "(plot_id, grid_x, grid_y, ring, kind, owner_type, owner_id, "
                "access_policy, claimed_at, claim_seq) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, 'open', NULL, NULL)",
                (plot_id_for(gx, gy), gx, gy, ring, kind),
            )
            seeded += cursor.rowcount
    if seeded:
        logger.info("seed_plots: seeded %d plots (%dx%d)", seeded, cols, rows)
    return seeded


def get_plot(conn: sqlite3.Connection, plot_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM plots WHERE plot_id = ?", (plot_id,)
    ).fetchone()
    return dict(row) if row else None


def has_closed_plots(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM plots WHERE access_policy = 'closed' LIMIT 1"
        ).fetchone()
        is not None
    )


def can_enter_plot(
    conn: sqlite3.Connection,
    soul_id: str,
    x: float,
    y: float,
    is_operator: bool = False,
) -> bool:
    """Access check for the movement path. Commons and road plots are
    always traversable; open plots admit anyone; closed plots admit
    only their owner and the operator."""
    gx, gy = plot_at(x, y)
    row = conn.execute(
        "SELECT kind, owner_type, owner_id, access_policy FROM plots "
        "WHERE plot_id = ?",
        (plot_id_for(gx, gy),),
    ).fetchone()
    if row is None:
        return True
    if row["kind"] in (KIND_COMMONS, KIND_ROAD):
        return True
    if row["access_policy"] == ACCESS_OPEN:
        return True
    if is_operator:
        return True
    return row["owner_type"] == OWNER_SOUL and row["owner_id"] == soul_id


def _claim_counter(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM globals WHERE key = ?", (SEQ_GLOBAL_KEY,)
    ).fetchone()
    return int(float(row["value"])) if row else 0


def _next_available_plot(
    conn: sqlite3.Connection,
) -> tuple[str, int, int] | None:
    """Scan forward from the counter for the next unclaimed claimable
    plot. Returns (plot_id, ring, seq) or None when exhausted. Pure
    read; the counter is only advanced on a successful claim."""
    order = allocation_order()
    counter = _claim_counter(conn)
    for n in range(counter + 1, len(order) + 1):
        plot_id = order[n - 1]
        row = conn.execute(
            "SELECT ring, kind, owner_id FROM plots WHERE plot_id = ?",
            (plot_id,),
        ).fetchone()
        if (
            row is not None
            and row["kind"] == KIND_CLAIMABLE
            and row["owner_id"] is None
        ):
            return plot_id, int(row["ring"]), n
    return None


def estimate_claim_fee(conn: sqlite3.Connection) -> float:
    """Fee for the plot a claim would take if adjudicated right now.
    Held in escrow at enqueue; the fee is non-decreasing in seq, so the
    adjudicated fee is never below this estimate."""
    found = _next_available_plot(conn)
    if found is None:
        raise PlotRefusal("no_plots_left", "No claimable plots remain")
    _, ring, _ = found
    return claim_fee(ring)


def _check_claimant(
    conn: sqlite3.Connection,
    custodian_id: str | None,
    soul_id: str,
    claimant_soul_id: str,
) -> None:
    if custodian_id is not None and claimant_soul_id != soul_id:
        raise PlotRefusal("custody", "Claimant does not match intent soul")
    if custodian_id is not None:
        row = conn.execute(
            "SELECT custodian_id, owner_id FROM souls WHERE soul_id = ?",
            (claimant_soul_id,),
        ).fetchone()
        if row is None or (row["custodian_id"] or row["owner_id"]) != custodian_id:
            raise PlotRefusal("custody", "Claimant soul not in custody")


def _hold_claim_escrow(
    conn: sqlite3.Connection,
    intent_id: str,
    claimant_soul_id: str,
    amount: float,
) -> None:
    """Hold the estimated claim fee. Runs inside the enqueue
    transaction. Raises PlotRefusal when the soul is missing or short."""
    cursor = conn.execute(
        "UPDATE souls SET essence = essence - ? WHERE soul_id = ? AND essence >= ?",
        (amount, claimant_soul_id, amount),
    )
    if cursor.rowcount == 0:
        exists = conn.execute(
            "SELECT 1 FROM souls WHERE soul_id = ?", (claimant_soul_id,)
        ).fetchone()
        if exists is None:
            raise PlotRefusal(
                "claimant_not_found", f"Claimant soul {claimant_soul_id} not found"
            )
        raise PlotRefusal(
            "insufficient_funds",
            f"Insufficient essence to hold claim fee {amount}",
        )
    conn.execute(
        "INSERT INTO escrows "
        "(escrow_id, intent_id, soul_id, amount, status, created_at) "
        "VALUES (?, ?, ?, ?, 'held', ?)",
        (
            "esc_" + secrets.token_urlsafe(12),
            intent_id,
            claimant_soul_id,
            amount,
            time.time(),
        ),
    )


def _release_escrow(conn: sqlite3.Connection, intent_id: str) -> bool:
    row = conn.execute(
        "SELECT soul_id, amount FROM escrows "
        "WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE souls SET essence = essence + ? WHERE soul_id = ?",
        (float(row["amount"]), row["soul_id"]),
    )
    conn.execute(
        "UPDATE escrows SET status = 'released' "
        "WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    )
    return True


def _held_escrow_amount(conn: sqlite3.Connection, intent_id: str) -> float:
    row = conn.execute(
        "SELECT amount FROM escrows WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    ).fetchone()
    if row is None:
        raise PlotRefusal("escrow_missing", "No held escrow for plot claim")
    return float(row["amount"])


def enqueue_plot_intent(
    session_id: str,
    nonce: str,
    custodian_id: str | None,
    soul_id: str,
    kind: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Enqueue a plot_claim intent with commit-before-ack.

    The intent insert and the estimated-fee escrow hold commit in ONE
    transaction. Returns (record, created); a duplicate (session_id,
    nonce) returns the existing record with created=False and never
    double-holds funds. Raises PlotRefusal on preface failures (no
    intent row is written).
    """
    if kind != KIND_PLOT_CLAIM:
        raise ValueError(f"not a plot intent kind: {kind}")
    claimant_soul_id = payload["claimant_soul_id"]
    intent_id = "int_" + secrets.token_urlsafe(16)
    now = time.time()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                conn.execute(
                    "INSERT INTO intents "
                    "(intent_id, session_id, nonce, custodian_id, soul_id, "
                    "kind, payload, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (
                        intent_id,
                        session_id,
                        nonce,
                        custodian_id,
                        soul_id,
                        kind,
                        json.dumps(payload),
                        now,
                    ),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            if created:
                _check_claimant(conn, custodian_id, soul_id, claimant_soul_id)
                estimate = estimate_claim_fee(conn)
                _hold_claim_escrow(conn, intent_id, claimant_soul_id, estimate)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        row = conn.execute(
            "SELECT * FROM intents WHERE session_id = ? AND nonce = ?",
            (session_id, nonce),
        ).fetchone()
        assert row is not None
        return _intent_row_to_dict(row), created


def _apply_claim(
    conn: sqlite3.Connection, tick_id: int, intent: dict[str, Any]
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    intent_id = intent["intent_id"]
    claimant_soul_id = payload["claimant_soul_id"]
    access_policy = payload.get("access_policy", ACCESS_OPEN)
    _check_claimant(conn, intent["custodian_id"], intent["soul_id"],
                    claimant_soul_id)
    claimant = conn.execute(
        "SELECT 1 FROM souls WHERE soul_id = ?", (claimant_soul_id,)
    ).fetchone()
    if claimant is None:
        _release_escrow(conn, intent_id)
        raise PlotRefusal("claimant_not_found", "Claimant soul not found")
    held = _held_escrow_amount(conn, intent_id)
    found = _next_available_plot(conn)
    if found is None:
        _release_escrow(conn, intent_id)
        raise PlotRefusal("no_plots_left", "No claimable plots remain")
    plot_id, ring, seq = found
    fee = claim_fee(ring)
    if fee > held + 1e-9:
        extra = round(fee - held, 2)
        cursor = conn.execute(
            "UPDATE souls SET essence = essence - ? "
            "WHERE soul_id = ? AND essence >= ?",
            (extra, claimant_soul_id, extra),
        )
        if cursor.rowcount == 0:
            _release_escrow(conn, intent_id)
            raise PlotRefusal(
                "insufficient_funds",
                f"Claim fee rose to {fee}; claimant cannot cover it",
            )
    now = time.time()
    cursor = conn.execute(
        "UPDATE plots SET owner_type = ?, owner_id = ?, access_policy = ?, "
        "claimed_at = ?, claim_seq = ? "
        "WHERE plot_id = ? AND owner_id IS NULL",
        (OWNER_SOUL, claimant_soul_id, access_policy, now, seq, plot_id),
    )
    if cursor.rowcount == 0:
        _release_escrow(conn, intent_id)
        raise PlotRefusal("plot_taken", f"Plot {plot_id} was just claimed")
    conn.execute(
        "UPDATE globals SET value = ? WHERE key = ?", (float(seq), SEQ_GLOBAL_KEY)
    )
    conn.execute(
        "UPDATE escrows SET status = 'applied' "
        "WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    )
    conn.execute(
        "UPDATE globals SET value = value + ? WHERE key = 'essence_fund'", (fee,)
    )
    conn.executemany(
        "INSERT INTO ledger "
        "(tick_id, intent_id, entry_type, soul_id, amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (tick_id, intent_id, LEDGER_DEBIT, claimant_soul_id, fee, now),
            (tick_id, intent_id, LEDGER_TAX, None, fee, now),
        ],
    )
    return {
        "plot_id": plot_id,
        "ring": ring,
        "fee": fee,
        "claim_seq": seq,
        "access_policy": access_policy,
    }


def _settle_once(tick: Any, intent: dict[str, Any]) -> None:
    """Adjudicate one plot intent in a single BEGIN IMMEDIATE
    transaction: plot row claim, counter increment, escrow apply (or
    release), cached balance updates, fund credit, ledger rows, intent
    status, and journal event all commit together (RPO = 0).

    The intent row is re-read inside the write transaction and settled
    only when still pending, so two racing pumps can never
    double-adjudicate the same intent. Refusals release the escrow and
    write no plot or ledger rows.
    """
    kind = intent["kind"]
    intent_id = intent["intent_id"]
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            status_row = conn.execute(
                "SELECT status FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if status_row is None or status_row["status"] != "pending":
                conn.rollback()
                return
            try:
                if kind == KIND_PLOT_CLAIM:
                    result = _apply_claim(conn, tick.tick_id, intent)
                else:
                    raise PlotRefusal("unknown_kind", kind)
                status, event = "adjudicated", persistence.EVENT_INTENT_ADJUDICATED
            except PlotRefusal as refusal:
                result = {"reason": refusal.reason, "detail": refusal.detail}
                status, event = "rejected", persistence.EVENT_INTENT_REJECTED
            conn.execute(
                "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                (status, json.dumps(result), intent_id),
            )
            payload = {
                "intent_id": intent_id,
                "kind": kind,
                "soul_id": intent["soul_id"],
            }
            if status == "adjudicated":
                payload["result"] = result
            else:
                payload["reason"] = result["reason"]
            persistence.append_event(conn, tick.tick_id, event, payload)
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _compensate_reject(tick: Any, intent: dict[str, Any]) -> None:
    """Last-resort settlement when adjudication raised unexpectedly:
    release any held escrow and mark the intent rejected in one
    transaction. Boot reconcile_escrows() is the final safety net if
    even this fails."""
    intent_id = intent["intent_id"]
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                conn.rollback()
                return
            if intent["kind"] in PLOT_KINDS:
                _release_escrow(conn, intent_id)
            conn.execute(
                "UPDATE intents SET status = 'rejected', result = ? "
                "WHERE intent_id = ?",
                (json.dumps({"reason": "internal"}), intent_id),
            )
            persistence.append_event(
                conn,
                tick.tick_id,
                persistence.EVENT_INTENT_REJECTED,
                {
                    "intent_id": intent_id,
                    "kind": intent["kind"],
                    "soul_id": intent["soul_id"],
                    "reason": "internal",
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception(
                "plot compensate failed for %s; boot reconcile is the net",
                intent_id,
            )


def adjudicate_plot_intent(tick: Any, intent: dict[str, Any]) -> None:
    """Tick-pump entry point for plot intents. Never raises."""
    try:
        _settle_once(tick, intent)
    except Exception:
        logger.exception("plot adjudication failed: %s", intent["intent_id"])
        _compensate_reject(tick, intent)
