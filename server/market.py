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

Issue #40: tamers are first-class market actors. Listings carry the
seller's actor type; escrows and ledger rows carry the buyer's. A
tamer lists from their own account inventory or a custodied soul's,
and buys into their account inventory; the tamer wallet backs escrow
exactly like a soul wallet. Listings are inventory-backed: the item
is reserved out of the seller's inventory at list time and returned
on cancel, credited to the buyer on sale.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from typing import Any

from . import database
from . import determinism
from . import dormancy
from . import inventory
from . import persistence
from . import resources
from . import wallets
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
    "insufficient_inventory": "INSUFFICIENT_INVENTORY",
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


def _market_actor_type(payload: dict[str, Any], key: str) -> str:
    actor_type = payload.get(key, database.ACTOR_SOUL)
    if actor_type not in (database.ACTOR_SOUL, database.ACTOR_TAMER):
        raise MarketRefusal("bad_payload", f"Unknown actor type {actor_type!r}")
    return actor_type


def _check_market_actor(
    conn: sqlite3.Connection,
    custodian_id: str | None,
    intent_actor_id: str,
    actor_type: str,
    actor_id: str,
    *,
    role: str,
) -> None:
    """Validate that the intent's identity may act as (actor_type, actor_id).

    Operators (custodian_id None) may name any existing soul or tamer.
    A soul acts as itself. A tamer acts as itself, or as a soul whose
    custodian/owner is the tamer; the named actor's wallet and
    inventory back the trade.
    """
    if actor_type == database.ACTOR_TAMER:
        exists = conn.execute(
            "SELECT 1 FROM tamers WHERE tamer_id = ?", (actor_id,)
        ).fetchone()
        if exists is None:
            raise MarketRefusal(f"{role}_not_found", f"{role} tamer not found")
        if custodian_id is None:
            return
        if actor_id != intent_actor_id:
            raise MarketRefusal("custody", "A tamer acts as itself")
        return
    row = conn.execute(
        "SELECT custodian_id, owner_id FROM souls WHERE soul_id = ?", (actor_id,)
    ).fetchone()
    if row is None:
        raise MarketRefusal(f"{role}_not_found", f"{role} soul not found")
    if custodian_id is None:
        return
    if actor_id == intent_actor_id:
        return
    if (row["custodian_id"] or row["owner_id"]) == custodian_id:
        return
    raise MarketRefusal("custody", f"No custody of the {role} soul")


def _parse_listing_item(item: Any) -> tuple[str, int, str | None]:
    """Split a listing item into (item_name, qty, metadata_json).

    Resource listings ({"type": "resource", "item": "food"|"water",
    "qty": N}) keep their quantity semantics; every other item is a
    single unit and any extra keys ride along as a metadata JSON blob.
    """
    if not isinstance(item, dict):
        raise MarketRefusal("item_unnamed", "Listing item must be an object")
    listed = resources.resource_listing(item)
    if listed is not None:
        name, qty = listed
        metadata = {
            key: value
            for key, value in item.items()
            if key not in ("type", "item", "qty")
        }
    else:
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise MarketRefusal("item_unnamed", "Listing item must name an item")
        qty = 1
        metadata = {key: value for key, value in item.items() if key != "name"}
    return name, qty, json.dumps(metadata) if metadata else None


def _hold_escrow(
    conn: sqlite3.Connection,
    intent_id: str,
    buyer_type: str,
    buyer_id: str,
    listing_id: str,
) -> float:
    """Hold the buyer's funds for a market_buy.

    Runs inside the enqueue transaction: the listing price is read,
    the buyer's cached wallet is decremented, and the escrow row is
    written. Raises MarketRefusal when the listing is gone or funds
    are short. Returns the held price.
    """
    row = conn.execute(
        "SELECT price FROM marketplace WHERE listing_id = ?", (listing_id,)
    ).fetchone()
    if row is None:
        raise MarketRefusal("listing_not_found", "Listing not found")
    price = round(float(row["price"]), 2)
    essence_before = wallets.cached_balance(conn, buyer_type, buyer_id)
    if not wallets.debit(conn, buyer_type, buyer_id, price):
        if essence_before is None:
            raise MarketRefusal("buyer_not_found", "Buyer not found")
        raise MarketRefusal(
            "insufficient_funds",
            f"Insufficient essence to hold {price} for listing {listing_id}",
        )
    if buyer_type == database.ACTOR_SOUL:
        # A hold that drains the buyer to 0 freezes it (dormancy is
        # derived); the flip is journaled.
        dormancy.note_essence_change(conn, buyer_id, essence_before or 0.0)
    conn.execute(
        "INSERT INTO escrows "
        "(escrow_id, intent_id, actor_type, soul_id, amount, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'held', ?)",
        (
            "esc_" + secrets.token_urlsafe(12),
            intent_id,
            buyer_type,
            buyer_id,
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
        "SELECT actor_type, soul_id, amount FROM escrows "
        "WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    ).fetchone()
    if row is None:
        return False
    actor_type = row["actor_type"] or database.ACTOR_SOUL
    actor_id = row["soul_id"]
    amount = float(row["amount"])
    essence_before = (
        wallets.cached_balance(conn, actor_type, actor_id)
        if actor_type == database.ACTOR_SOUL
        else None
    )
    wallets.credit(conn, actor_type, actor_id, amount)
    if actor_type == database.ACTOR_SOUL:
        dormancy.note_essence_change(conn, actor_id, essence_before or 0.0)
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
        validate_item_size(payload["item"])
        _market_actor_type(payload, "seller_type")
    elif kind == KIND_MARKET_BUY:
        _market_actor_type(payload, "buyer_type")
    elif kind == KIND_MARKET_CANCEL:
        _market_actor_type(payload, "actor_type")
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
                    buyer_type = payload.get("buyer_type", database.ACTOR_SOUL)
                    buyer_id = payload["buyer_soul_id"]
                    _check_market_actor(
                        conn, custodian_id, soul_id, buyer_type, buyer_id,
                        role="buyer",
                    )
                    if buyer_type == database.ACTOR_SOUL and (
                        dormancy.soul_is_dormant(conn, buyer_id)
                    ):
                        raise MarketRefusal(
                            "soul_dormant",
                            "A dormant (unfunded) soul cannot buy",
                        )
                    _hold_escrow(conn, intent_id, buyer_type, buyer_id, payload["listing_id"])
                elif kind == KIND_MARKET_LIST:
                    seller_type = payload.get("seller_type", database.ACTOR_SOUL)
                    seller_id = payload["seller_soul_id"]
                    _check_market_actor(
                        conn, custodian_id, soul_id, seller_type, seller_id,
                        role="seller",
                    )
                    if seller_type == database.ACTOR_SOUL and (
                        dormancy.soul_is_dormant(conn, seller_id)
                    ):
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
    rows: list[tuple[str, str, str | None, float]],
    now: float | None = None,
) -> None:
    # Issue #38: ledger created_at rides the adjudication clock so a
    # seeded replay writes identical rows.
    now = time.time() if now is None else now
    conn.executemany(
        "INSERT INTO ledger "
        "(tick_id, intent_id, entry_type, actor_type, soul_id, amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (tick_id, intent_id, entry_type, actor_type, actor_id, amount, now)
            for entry_type, actor_type, actor_id, amount in rows
        ],
    )


def _apply_list(
    conn: sqlite3.Connection,
    tick: Any,
    intent: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    seller_type = payload.get("seller_type", database.ACTOR_SOUL)
    seller_id = payload["seller_soul_id"]
    _check_market_actor(
        conn, intent["custodian_id"], intent["soul_id"], seller_type, seller_id,
        role="seller",
    )
    # Issue #38: a client-supplied listing_id is authoritative; a minted
    # one is record-and-replayed in the replay CLI (it is part of the
    # recorded result) and secrets-based live.
    listing_id = payload.get("listing_id") or determinism.tick_gen_id(
        tick,
        intent["intent_id"],
        "listing_id",
        lambda: "lst_" + secrets.token_urlsafe(6),
    )
    now = determinism.tick_now(tick) if now is None else now
    # Issue #40: listings are inventory-backed. The item is reserved
    # out of the seller's inventory at list time; resource listings
    # keep their quantity semantics, everything else lists one unit.
    item_name, qty, item_metadata = _parse_listing_item(payload.get("item"))
    if not inventory.remove(conn, seller_type, seller_id, item_name, qty):
        raise MarketRefusal(
            "insufficient_inventory",
            f"Seller holds less than {qty} x {item_name}",
        )
    conn.execute(
        "INSERT INTO marketplace "
        "(listing_id, seller_id, seller_type, seller_name, item, price, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            listing_id,
            seller_id,
            seller_type,
            payload.get("seller_name", ""),
            json.dumps(payload.get("item")),
            round(float(payload["price"]), 2),
            now,
        ),
    )
    _write_ledger(
        conn,
        tick.tick_id,
        intent["intent_id"],
        [(LEDGER_MEMO, seller_type, seller_id, 0.0)],
        now=now,
    )
    return {"listing_id": listing_id}


def _apply_buy(
    conn: sqlite3.Connection,
    tick: Any,
    intent: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    intent_id = intent["intent_id"]
    buyer_type = payload.get("buyer_type", database.ACTOR_SOUL)
    buyer_id = payload["buyer_soul_id"]
    _check_market_actor(
        conn, intent["custodian_id"], intent["soul_id"], buyer_type, buyer_id,
        role="buyer",
    )
    listing_id = payload["listing_id"]
    now = determinism.tick_now(tick) if now is None else now
    row = conn.execute(
        "SELECT * FROM marketplace WHERE listing_id = ?", (listing_id,)
    ).fetchone()
    if row is None:
        _release_escrow(conn, intent_id)
        raise MarketRefusal("listing_gone", "Listing already sold or removed")
    price = round(float(row["price"]), 2)
    seller_id = row["seller_id"]
    seller_type = row["seller_type"] or database.ACTOR_SOUL
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
    seller_before = wallets.cached_balance(conn, seller_type, seller_id)
    credited = wallets.credit(conn, seller_type, seller_id, seller_net)
    # Issue #40: the sold item leaves escrow into the buyer's
    # inventory. The XP_SELL award is silent telemetry for souls
    # (no ledger row, no UI).
    item_name, qty, item_metadata = _parse_listing_item(json.loads(item_raw))
    inventory.add(conn, buyer_type, buyer_id, item_name, qty, item_metadata)
    if credited:
        if seller_type == database.ACTOR_SOUL:
            conn.execute(
                "UPDATE souls SET xp = COALESCE(xp, 0) + ? WHERE soul_id = ?",
                (resources.XP_SELL, seller_id),
            )
            dormancy.note_essence_change(
                conn, seller_id, seller_before or 0.0, tick.tick_id, now=now
            )
    else:
        logger.warning(
            "buy %s: seller %s gone; credit kept in ledger only",
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
        tick.tick_id,
        intent_id,
        [
            (LEDGER_DEBIT, buyer_type, buyer_id, debit),
            (LEDGER_CREDIT, seller_type, seller_id, seller_net),
            (LEDGER_TAX, buyer_type, None, tax),
        ],
        now=now,
    )
    return {
        "listing_id": listing_id,
        "price": price,
        "seller_id": seller_id,
        "seller_type": seller_type,
        "seller_credited": seller_net,
        "tax_collected": tax,
        "item": json.loads(item_raw),
    }


def _apply_cancel(
    conn: sqlite3.Connection,
    tick: Any,
    intent: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    listing_id = payload["listing_id"]
    now = determinism.tick_now(tick) if now is None else now
    row = conn.execute(
        "SELECT seller_id, seller_type, item FROM marketplace WHERE listing_id = ?",
        (listing_id,),
    ).fetchone()
    if row is None:
        raise MarketRefusal("listing_not_found", "Listing not found")
    _check_cancel_custody(
        conn, intent["custodian_id"], intent["soul_id"], row["seller_id"]
    )
    cursor = conn.execute(
        "DELETE FROM marketplace WHERE listing_id = ?", (listing_id,)
    )
    if cursor.rowcount == 0:
        raise MarketRefusal("listing_not_found", "Listing not found")
    # Issue #40: a cancelled listing returns the reserved item to the
    # seller's inventory, metadata carried through.
    seller_type = row["seller_type"] or database.ACTOR_SOUL
    item_name, qty, item_metadata = _parse_listing_item(json.loads(row["item"]))
    inventory.add(conn, seller_type, row["seller_id"], item_name, qty, item_metadata)
    canceler_type = payload.get("actor_type", database.ACTOR_SOUL)
    _write_ledger(
        conn,
        tick.tick_id,
        intent["intent_id"],
        [(LEDGER_MEMO, canceler_type, intent["soul_id"], 0.0)],
        now=now,
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
                # Issue #38: one adjudication clock per intent; helpers
                # take it explicitly so replay is bit-identical.
                now = determinism.tick_now(tick)
                if kind == KIND_MARKET_LIST:
                    result = _apply_list(conn, tick, intent, now=now)
                elif kind == KIND_MARKET_BUY:
                    result = _apply_buy(conn, tick, intent, now=now)
                elif kind == KIND_MARKET_CANCEL:
                    result = _apply_cancel(conn, tick, intent, now=now)
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

    Returns {"ok", "soul_drifts", "tamer_drifts", "fund", "repaired"}.
    With repair=True, drifted caches are overwritten with the
    ledger-derived values. Baseline bookkeeping rows are committed in
    both modes; only the drift repairs are gated on repair=True.
    """
    report: dict[str, Any] = {
        "ok": True,
        "soul_drifts": [],
        "tamer_drifts": [],
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
        "SELECT actor_type, soul_id, "
        "SUM(CASE WHEN entry_type = 'debit' THEN -amount "
        "WHEN entry_type = 'credit' THEN amount "
        # Newborn starter grants (issue #22) are conservation-explicit:
        # a mint is created essence, counted like a credit.
        "WHEN entry_type = 'mint' THEN amount "
        "ELSE 0.0 END) AS delta "
        "FROM ledger WHERE soul_id IS NOT NULL GROUP BY actor_type, soul_id"
    ).fetchall()
    for row in deltas:
        actor_type = row["actor_type"] or database.ACTOR_SOUL
        actor_id = row["soul_id"]
        delta = float(row["delta"])
        if actor_type == database.ACTOR_TAMER:
            tamer = conn.execute(
                "SELECT essence FROM tamers WHERE tamer_id = ?", (actor_id,)
            ).fetchone()
            if tamer is None:
                continue
            essence = float(tamer["essence"] or 0.0)
            key = f"ledger_base:tamer:{actor_id}"
            base = conn.execute(
                "SELECT value FROM globals WHERE key = ?", (key,)
            ).fetchone()
            if base is None:
                baseline = essence - delta
                conn.execute(
                    "INSERT INTO globals (key, value) VALUES (?, ?)",
                    (key, baseline),
                )
                wrote_baselines = True
            else:
                baseline = float(base["value"])
            expected = baseline + delta
            drift = essence - expected
            if abs(drift) > 1e-6:
                report["ok"] = False
                report["tamer_drifts"].append(
                    {
                        "tamer_id": actor_id,
                        "essence": essence,
                        "expected": expected,
                        "drift": drift,
                    }
                )
                if repair:
                    conn.execute(
                        "UPDATE tamers SET essence = ? WHERE tamer_id = ?",
                        (expected, actor_id),
                    )
                    report["repaired"] = True
            continue
        soul_id = actor_id
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
        report["tamer_drifts"] = []
    return report
