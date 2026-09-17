"""Essence wallets for souls and tamers.

Both actor kinds hold a cached essence balance (``souls.essence``,
``tamers.essence``); the ledger is the truth behind the cache. These
helpers debit and credit either wallet with one code path so market
escrow, transfers, and tamer funding work for tamers exactly like
souls.
"""

from __future__ import annotations

import sqlite3

from . import database


def _wallet(actor_type: str) -> tuple[str, str]:
    if actor_type == database.ACTOR_TAMER:
        return "tamers", "tamer_id"
    if actor_type == database.ACTOR_SOUL:
        return "souls", "soul_id"
    raise ValueError(f"unknown actor_type {actor_type!r}")


def cached_balance(
    conn: sqlite3.Connection, actor_type: str, actor_id: str
) -> float | None:
    """Cached essence for a soul or tamer wallet; None when missing."""
    table, id_col = _wallet(actor_type)
    row = conn.execute(
        f"SELECT COALESCE(essence, 0.0) AS essence FROM {table} "
        f"WHERE {id_col} = ?",
        (actor_id,),
    ).fetchone()
    return float(row["essence"]) if row else None


def debit(
    conn: sqlite3.Connection, actor_type: str, actor_id: str, amount: float
) -> bool:
    """Decrement when the wallet covers ``amount``; True when debited."""
    table, id_col = _wallet(actor_type)
    cursor = conn.execute(
        f"UPDATE {table} SET essence = essence - ? "
        f"WHERE {id_col} = ? AND essence >= ?",
        (amount, actor_id, amount),
    )
    return cursor.rowcount > 0


def credit(
    conn: sqlite3.Connection, actor_type: str, actor_id: str, amount: float
) -> bool:
    """Increment; False when the wallet does not exist."""
    table, id_col = _wallet(actor_type)
    cursor = conn.execute(
        f"UPDATE {table} SET essence = essence + ? WHERE {id_col} = ?",
        (amount, actor_id),
    )
    return cursor.rowcount > 0
