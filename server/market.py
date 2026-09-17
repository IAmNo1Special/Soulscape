"""Hub marketplace through intent adjudication (issue #17).

Marketplace buy/list/cancel are durable intents adjudicated at tick
boundaries. Every essence movement is an append-only ledger row; the 2%
Essence Fund tax is a row, not arithmetic on the side.

Money flow for a buy:
  1. Ingress (WS or REST): the buy intent and the buyer's escrow hold are
     committed in ONE transaction (commit-before-ack). The cached
     `souls.essence` column is decremented here; the ledger row lands at
     adjudication.
  2. Tick pump: the adjudication claims the listing atomically
     (DELETE ... WHERE listing_id = ? + rowcount check), writes the
     debit/credit/tax ledger rows, credits the seller and the fund
     caches, and marks the escrow applied -- all in one BEGIN IMMEDIATE
     transaction together with the intent status and the journal event.
     RPO = 0, consistent with issues #14/#16.
  3. Rejection (listing gone, escrow short): the escrow is released
     (cached balance refunded) in the same transaction that marks the
     intent rejected. A crash between ack and settlement leaves
     (intent=pending, escrow=held); boot reconcile_escrows() + the pump
     settle it exactly once.

Conservation: buyer debit = full price, seller credit = price - tax,
fund tax row. verify_balances() recomputes the cached columns from the
ledger and reports (optionally repairs) drift.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from typing import Any

from . import database
from . import dormancy
from . import persistence
from . import resources
from .intents import _row_to_dict as _intent_row_to_dict

logger = logging.getLogger("soulscape_hub")

TAX_RATE = 0.02

KIND_MARKET_LIST = "market_list"
KIND_MARKET_BUY = "market_buy"
KIND_MARKET_CANCEL = "market_cancel"
MARKET_KINDS = {KIND_MARKET_LIST, KIND_MARKET_BUY, KIND_MARKET_CANCEL}

ESCROW_HELD = "held"
ESCROW_APPLIED = "applied"
ESCROW_RELEASED = "released"

LEDGER_DEBIT = "debit"
LEDGER_CREDIT = "credit"
LEDGER_TAX = "tax"
LEDGER_MEMO = "memo"

MAX_ITEM_SIZE = 100_000
MAX_JSON_DEPTH = 10

_WS_ERROR_CODES = {
    "listing_not_found": "LISTING_NOT_FOUND",
    "listing_gone": "LISTING_GONE",
    "insufficient_funds": "INSUFFICIENT_FUNDS",
    "buyer_not_found": "SOUL_NOT_FOUND",
    "seller_not_found": "SOUL_NOT_FOUND",
    "custody": "CUSTODY_DENIED",
    "escrow_short": "ESCROW_SHORT",
    "soul_dormant": "SOUL_DORMANT",
    "internal": "INTERNAL",
}


class MarketRefusal(Exception):
    """Business-logic refusal to enqueue or adjudicate a market intent."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason

    @property
    def ws_code(self) -> str:
        return _WS_ERROR_CODES.get(self.reason, "REJECTED")


def split_price(price: float) -> tuple[float, float, float]:
    """Return (buyer_debit, seller_net, tax) for a listing price."""
    price = round(float(price), 2)
    tax = round(price * TAX_RATE, 2)
    seller_net = round(price - tax, 2)
    return price, seller_net, tax


def validate_item_size(item: Any) -> None:
    raw = json.dumps(item)
    if len(raw) > MAX_ITEM_SIZE:
        raise MarketRefusal("item_too_large", "Item data too large")
    _check_depth(json.loads(raw), 0)


def _check_depth(obj: Any, depth: int) -> None:
    if depth > MAX_JSON_DEPTH:
        raise MarketRefusal("item_too_deep", "Item nesting too deep")
    if isinstance(obj, dict):
        for value in obj.values():
            _check_depth(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            _check_depth(value, depth + 1)


def _check_actor(payload: dict[str, Any], soul_id: str, field: str) -> None:
    if payload.get(field) != soul_id:
        raise MarketRefusal("custody", f"{field} does not match intent soul")


def _hold_escrow(
    conn: sqlite3.Connection,
    intent_id: str,
    buyer_soul_id: str,
    listing_id: str,
) -> float:
    """Hold the buyer's funds for a market_buy.

    Runs inside the enqueue transaction: the listing price is read, the
    cached souls.essence is decremented, and the escrow row is written.
    Raises MarketRefusal when the listing is gone or funds are short.
    Returns the held price.
    """
    row = conn.execute(
        "SELECT price FROM marketplace WHERE listing_id = ?", (listing_id,)
    ).fetchone()
    if row is None:
        raise MarketRefusal("listing_not_found", "Listing not found")
    price = round(float(row["price"]), 2)
    essence_before = dormancy.cached_essence(conn, buyer_soul_id)
    cursor = conn.execute(
        "UPDATE souls SET essence = essence - ? WHERE soul_id = ? AND essence >= ?",
        (price, buyer_soul_id, price),
    )
    if cursor.rowcount == 0:
        exists = conn.execute(
            "SELECT 1 FROM souls WHERE soul_id = ?", (buyer_soul_id,)
        ).fetchone()
        if exists is None:
            raise MarketRefusal("buyer_not_found", "Buyer soul not found")
        raise MarketRefusal(
            "insufficient_funds",
            f"Insufficient essence to hold {price} for listing {listing_id}",
        )
    # A hold that drains the buyer to 0 freezes it (dormancy is
    # derived); the flip is journaled.
    dormancy.note_essence_change(conn, buyer_soul_id, essence_before or 0.0)
    conn.execute(
        "INSERT INTO escrows "
        "(escrow_id, intent_id, soul_id, amount, status, created_at) "
        "VALUES (?, ?, ?, ?, 'held', ?)",
        (
            "esc_" + secrets.token_urlsafe(12),
            intent_id,
            buyer_soul_id,
            price,
            time.time(),
        ),
    )
    return price


def _release_escrow(conn: sqlite3.Connection, intent_id: str) -> bool:
    """Refund a held escrow to the cached balance. Returns True if one
    was released. A refund is a funding refresh: it can wake a dormant
    soul (soul_woke journaled)."""
    row = conn.execute(
        "SELECT soul_id, amount FROM escrows WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    ).fetchone()
    if row is None:
        return False
    essence_before = dormancy.cached_essence(conn, row["soul_id"])
    conn.execute(
        "UPDATE souls SET essence = essence + ? WHERE soul_id = ?",
        (float(row["amount"]), row["soul_id"]),
    )
    dormancy.note_essence_change(conn, row["soul_id"], essence_before or 0.0)
    conn.execute(
        "UPDATE escrows SET status = 'released' "
        "WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    )
    return True


def enqueue_market_intent(
    session_id: str,
    nonce: str,
    custodian_id: str | None,
    soul_id: str,
    kind: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Enqueue a market intent with commit-before-ack.

    The intent insert and, for market_buy, the escrow hold commit in ONE
    transaction: the ack is only ever sent for durable state. Returns
    (record, created); a duplicate (session_id, nonce) returns the
    existing record with created=False and never double-holds funds.
    Raises MarketRefusal on preface failures (no intent row is written).
    """
    if kind not in MARKET_KINDS:
        raise ValueError(f"not a market intent kind: {kind}")
    if kind == KIND_MARKET_LIST:
        if custodian_id is not None:
            _check_actor(payload, soul_id, "seller_soul_id")
        validate_item_size(payload["item"])
    elif kind == KIND_MARKET_BUY:
        if custodian_id is not None:
            _check_actor(payload, soul_id, "buyer_soul_id")
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
                # Dormancy (issue #22): dormant souls cannot author market
                # intents -- statues don't act. The one deliberate
                # exception: a dormant soul's listings REMAIN BUYABLE,
                # because the buyer acts, not the soul; the sale proceeds
                # are a funding refresh that wakes the seller (soul_woke
                # journaled at adjudication).
                if kind == KIND_MARKET_BUY:
                    if dormancy.soul_is_dormant(conn, soul_id):
                        raise MarketRefusal(
                            "soul_dormant",
                            "A dormant (unfunded) soul cannot buy",
                        )
                    _hold_escrow(conn, intent_id, soul_id, payload["listing_id"])
                elif kind == KIND_MARKET_LIST:
                    seller_soul_id = payload["seller_soul_id"]
                    seller = conn.execute(
                        "SELECT 1 FROM souls WHERE soul_id = ?",
                        (seller_soul_id,),
                    ).fetchone()
                    if seller is None:
                        raise MarketRefusal("seller_not_found", "Seller soul not found")
                    if dormancy.soul_is_dormant(conn, seller_soul_id):
                        raise MarketRefusal(
                            "soul_dormant",
                            "A dormant (unfunded) soul cannot list items",
                        )
                elif kind == KIND_MARKET_CANCEL:
                    if dormancy.soul_is_dormant(conn, soul_id):
                        raise MarketRefusal(
                            "soul_dormant",
                            "A dormant (unfunded) soul cannot cancel listings",
                        )
                    _preface_cancel(conn, custodian_id, soul_id, payload["listing_id"])
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


def _preface_cancel(
    conn: sqlite3.Connection,
    custodian_id: str | None,
    soul_id: str,
    listing_id: str,
) -> None:
    row = conn.execute(
        "SELECT seller_id FROM marketplace WHERE listing_id = ?", (listing_id,)
    ).fetchone()
    if row is None:
        raise MarketRefusal("listing_not_found", "Listing not found")
    _check_cancel_custody(conn, custodian_id, soul_id, row["seller_id"])


def _check_cancel_custody(
    conn: sqlite3.Connection,
    custodian_id: str | None,
    soul_id: str,
    seller_id: str,
) -> None:
    if custodian_id is None:
        return
    if soul_id == seller_id:
        return
    seller = conn.execute(
        "SELECT custodian_id, owner_id FROM souls WHERE soul_id = ?",
        (seller_id,),
    ).fetchone()
    if seller is not None and (seller["custodian_id"] or seller["owner_id"]) == (
        custodian_id
    ):
        return
    raise MarketRefusal("custody", "Only the seller (or operator) can cancel")


def _write_ledger(
    conn: sqlite3.Connection,
    tick_id: int,
    intent_id: str,
    rows: list[tuple[str, str | None, float]],
) -> None:
    now = time.time()
    conn.executemany(
        "INSERT INTO ledger "
        "(tick_id, intent_id, entry_type, soul_id, amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (tick_id, intent_id, entry_type, soul_id, amount, now)
            for entry_type, soul_id, amount in rows
        ],
    )


def _apply_list(
    conn: sqlite3.Connection, tick_id: int, intent: dict[str, Any]
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    seller_soul_id = payload["seller_soul_id"]
    if (
        intent["custodian_id"] is not None
        and payload.get("seller_soul_id") != intent["soul_id"]
    ):
        raise MarketRefusal("custody", "seller does not match intent soul")
    seller = conn.execute(
        "SELECT 1 FROM souls WHERE soul_id = ?", (seller_soul_id,)
    ).fetchone()
    if seller is None:
        raise MarketRefusal("seller_not_found", "Seller soul not found")
    listing_id = payload.get("listing_id") or "lst_" + secrets.token_urlsafe(6)
    # Issue #34: a resource listing escrows the items out of the
    # seller's inventory at list time (the qty rides in the item JSON).
    listed = resources.resource_listing(payload.get("item"))
    if listed is not None:
        item_name, qty = listed
        if not resources.remove_item(conn, seller_soul_id, item_name, qty):
            raise MarketRefusal(
                "insufficient_inventory",
                f"Seller holds less than {qty} {item_name}",
            )
    conn.execute(
        "INSERT INTO marketplace "
        "(listing_id, seller_id, seller_name, item, price, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            listing_id,
            seller_soul_id,
            payload.get("seller_name", ""),
            json.dumps(payload["item"]),
            round(float(payload["price"]), 2),
            time.time(),
        ),
    )
    _write_ledger(
        conn, tick_id, intent["intent_id"], [(LEDGER_MEMO, seller_soul_id, 0.0)]
    )
    return {"listing_id": listing_id}


def _apply_buy(
    conn: sqlite3.Connection, tick_id: int, intent: dict[str, Any]
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    intent_id = intent["intent_id"]
    buyer_soul_id = intent["soul_id"]
    listing_id = payload["listing_id"]
    if intent["custodian_id"] is not None and payload.get("buyer_soul_id") != (
        buyer_soul_id
    ):
        _release_escrow(conn, intent_id)
        raise MarketRefusal("custody", "buyer does not match intent soul")
    row = conn.execute(
        "SELECT * FROM marketplace WHERE listing_id = ?", (listing_id,)
    ).fetchone()
    if row is None:
        _release_escrow(conn, intent_id)
        raise MarketRefusal("listing_gone", "Listing already sold or removed")
    price = round(float(row["price"]), 2)
    seller_id = row["seller_id"]
    item_raw = row["item"]
    cursor = conn.execute("DELETE FROM marketplace WHERE listing_id = ?", (listing_id,))
    if cursor.rowcount == 0:
        _release_escrow(conn, intent_id)
        raise MarketRefusal("listing_gone", "Listing already sold or removed")
    escrow = conn.execute(
        "SELECT amount, status FROM escrows WHERE intent_id = ?", (intent_id,)
    ).fetchone()
    if (
        escrow is None
        or escrow["status"] != ESCROW_HELD
        or float(escrow["amount"]) < price - 1e-9
    ):
        _release_escrow(conn, intent_id)
        raise MarketRefusal("escrow_short", "Held escrow does not cover the price")
    debit, seller_net, tax = split_price(price)
    # Sale proceeds are a funding refresh: buying a dormant soul's
    # listing wakes the seller (issue #22 -- soul_woke journaled).
    seller_before = dormancy.cached_essence(conn, seller_id)
    seller_row = conn.execute(
        "UPDATE souls SET essence = essence + ?, "
        "xp = COALESCE(xp, 0) + ? WHERE soul_id = ?",
        (seller_net, resources.XP_SELL, seller_id),
    )
    # Issue #34: a completed resource sale moves the escrowed items
    # into the buyer's inventory. The XP_SELL award above is silent
    # telemetry (no ledger row, no UI).
    sold = resources.resource_listing(json.loads(item_raw))
    if sold is not None:
        item_name, qty = sold
        resources.add_item(conn, buyer_soul_id, item_name, qty)
    if seller_row.rowcount:
        dormancy.note_essence_change(conn, seller_id, seller_before or 0.0, tick_id)
    if seller_row.rowcount == 0:
        logger.warning(
            "buy %s: seller soul %s gone; credit kept in ledger only",
            intent_id,
            seller_id,
        )
    conn.execute(
        "UPDATE globals SET value = value + ? WHERE key = 'essence_fund'", (tax,)
    )
    conn.execute(
        "UPDATE escrows SET status = 'applied' WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    )
    _write_ledger(
        conn,
        tick_id,
        intent_id,
        [
            (LEDGER_DEBIT, buyer_soul_id, debit),
            (LEDGER_CREDIT, seller_id, seller_net),
            (LEDGER_TAX, None, tax),
        ],
    )
    return {
        "listing_id": listing_id,
        "price": price,
        "seller_id": seller_id,
        "seller_credited": seller_net,
        "tax_collected": tax,
        "item": json.loads(item_raw),
    }


def _apply_cancel(
    conn: sqlite3.Connection, tick_id: int, intent: dict[str, Any]
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    listing_id = payload["listing_id"]
    row = conn.execute(
        "SELECT seller_id, item FROM marketplace WHERE listing_id = ?",
        (listing_id,),
    ).fetchone()
    if row is None:
        raise MarketRefusal("listing_not_found", "Listing not found")
    _check_cancel_custody(
        conn, intent["custodian_id"], intent["soul_id"], row["seller_id"]
    )
    cursor = conn.execute("DELETE FROM marketplace WHERE listing_id = ?", (listing_id,))
    if cursor.rowcount == 0:
        raise MarketRefusal("listing_not_found", "Listing not found")
    # Issue #34: a cancelled resource listing returns the escrowed
    # items to the seller's inventory.
    cancelled = resources.resource_listing(json.loads(row["item"]))
    if cancelled is not None:
        item_name, qty = cancelled
        resources.add_item(conn, row["seller_id"], item_name, qty)
    _write_ledger(
        conn, tick_id, intent["intent_id"], [(LEDGER_MEMO, intent["soul_id"], 0.0)]
    )
    return {"listing_id": listing_id, "seller_id": row["seller_id"]}


def _settle_once(tick: Any, intent: dict[str, Any]) -> None:
    """Adjudicate one market intent in a single BEGIN IMMEDIATE
    transaction: listing claim, escrow apply/release, cached balance
    updates, ledger rows, intent status, and journal event all commit
    together (RPO = 0).

    The intent row is re-read inside the write transaction and settled
    only when still pending, so two racing pumps can never
    double-adjudicate the same intent.
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
                if kind == KIND_MARKET_LIST:
                    result = _apply_list(conn, tick.tick_id, intent)
                elif kind == KIND_MARKET_BUY:
                    result = _apply_buy(conn, tick.tick_id, intent)
                elif kind == KIND_MARKET_CANCEL:
                    result = _apply_cancel(conn, tick.tick_id, intent)
                else:
                    raise MarketRefusal("unknown_kind", kind)
                status, event = "adjudicated", persistence.EVENT_INTENT_ADJUDICATED
            except MarketRefusal as refusal:
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
                payload["result"] = {k: v for k, v in result.items() if k != "item"}
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
            if intent["kind"] == KIND_MARKET_BUY:
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
                "market compensate failed for %s; boot reconcile is the net",
                intent_id,
            )


def adjudicate_market_intent(tick: Any, intent: dict[str, Any]) -> None:
    """Tick-pump entry point for market intents. Never raises: an
    unexpected failure is compensated (escrow released, intent rejected)
    and otherwise left for boot reconciliation."""
    try:
        _settle_once(tick, intent)
    except Exception:
        logger.exception("market adjudication failed: %s", intent["intent_id"])
        _compensate_reject(tick, intent)


def _baseline_key(soul_id: str) -> str:
    return f"ledger_base:{soul_id}"


def verify_balances(conn: sqlite3.Connection, repair: bool = False) -> dict[str, Any]:
    """Recompute cached balances from the ledger and compare.

    souls.essence and the essence_fund global are caches; the ledger is
    the truth. Per-soul baselines (essence value before any ledger row)
    are captured once into globals and reused, so non-market essence
    movements (e.g. social charges) do not report false drift.

    Returns {"ok", "soul_drifts", "fund", "repaired"}. With repair=True,
    drifted caches are overwritten with the ledger-derived values.
    Baseline bookkeeping rows are committed in both modes; only the
    drift repairs are gated on repair=True.
    """
    report: dict[str, Any] = {
        "ok": True,
        "soul_drifts": [],
        "fund": None,
        "repaired": False,
    }
    wrote_baselines = False
    tax_sum = float(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0.0) FROM ledger WHERE entry_type = 'tax'"
        ).fetchone()[0]
    )
    fund_row = conn.execute(
        "SELECT value FROM globals WHERE key = 'essence_fund'"
    ).fetchone()
    fund_value = float(fund_row["value"]) if fund_row else 0.0
    base_row = conn.execute(
        "SELECT value FROM globals WHERE key = 'ledger_fund_baseline'"
    ).fetchone()
    if base_row is None:
        fund_baseline = fund_value - tax_sum
        conn.execute(
            "INSERT INTO globals (key, value) VALUES ('ledger_fund_baseline', ?)",
            (fund_baseline,),
        )
        wrote_baselines = True
    else:
        fund_baseline = float(base_row["value"])
    fund_expected = fund_baseline + tax_sum
    fund_drift = fund_value - fund_expected
    report["fund"] = {
        "value": fund_value,
        "expected": fund_expected,
        "drift": fund_drift,
        "tax_rows_sum": tax_sum,
    }
    if abs(fund_drift) > 1e-6:
        report["ok"] = False
        if repair:
            conn.execute(
                "UPDATE globals SET value = ? WHERE key = 'essence_fund'",
                (fund_expected,),
            )
            report["fund"]["value"] = fund_expected
            report["fund"]["drift"] = 0.0
            report["repaired"] = True
    deltas = conn.execute(
        "SELECT soul_id, "
        "SUM(CASE WHEN entry_type = 'debit' THEN -amount "
        "WHEN entry_type = 'credit' THEN amount "
        # Newborn starter grants (issue #22) are conservation-explicit:
        # a mint is created essence, counted like a credit.
        "WHEN entry_type = 'mint' THEN amount "
        "ELSE 0.0 END) AS delta "
        "FROM ledger WHERE soul_id IS NOT NULL GROUP BY soul_id"
    ).fetchall()
    for row in deltas:
        soul_id = row["soul_id"]
        delta = float(row["delta"])
        soul = conn.execute(
            "SELECT essence FROM souls WHERE soul_id = ?", (soul_id,)
        ).fetchone()
        if soul is None:
            continue
        essence = float(soul["essence"] or 0.0)
        key = _baseline_key(soul_id)
        base = conn.execute(
            "SELECT value FROM globals WHERE key = ?", (key,)
        ).fetchone()
        if base is None:
            baseline = essence - delta
            conn.execute(
                "INSERT INTO globals (key, value) VALUES (?, ?)", (key, baseline)
            )
            wrote_baselines = True
        else:
            baseline = float(base["value"])
        expected = baseline + delta
        drift = essence - expected
        if abs(drift) > 1e-6:
            report["ok"] = False
            report["soul_drifts"].append(
                {
                    "soul_id": soul_id,
                    "essence": essence,
                    "expected": expected,
                    "drift": drift,
                }
            )
            if repair:
                conn.execute(
                    "UPDATE souls SET essence = ? WHERE soul_id = ?",
                    (expected, soul_id),
                )
                report["repaired"] = True
    if wrote_baselines and not repair:
        conn.commit()
    if repair:
        conn.commit()
        report["ok"] = True
        report["soul_drifts"] = []
    return report
