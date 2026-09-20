"""Actor inventories: soul and tamer item holdings.

Souls hold items in ``soul_inventory``; tamers hold items in the
``tamer_inventory`` account inventory. Both tables share the
(holder_id, item_name, quantity, metadata) shape; these helpers are
actor-aware so market adjudication can reserve, return, and credit
items for either kind of wallet-holder with one code path.
"""

from __future__ import annotations

import sqlite3

from . import database


def _table(actor_type: str) -> tuple[str, str]:
    if actor_type == database.ACTOR_TAMER:
        return "tamer_inventory", "tamer_id"
    if actor_type == database.ACTOR_SOUL:
        return "soul_inventory", "soul_id"
    raise ValueError(f"unknown actor_type {actor_type!r}")


def qty(
    conn: sqlite3.Connection, actor_type: str, actor_id: str, item: str
) -> int:
    """How many of ``item`` the actor holds; 0 when the row is absent."""
    table, id_col = _table(actor_type)
    row = conn.execute(
        f"SELECT quantity FROM {table} WHERE {id_col} = ? AND item_name = ?",
        (actor_id, item),
    ).fetchone()
    return int(row["quantity"]) if row else 0


def add(
    conn: sqlite3.Connection,
    actor_type: str,
    actor_id: str,
    item: str,
    amount: int,
    metadata: str | None = None,
) -> int:
    """Credit ``amount`` of ``item``; returns the new total.

    ``metadata`` is a JSON blob carried from the listing; it replaces
    the stored blob only when provided, so plain credits never wipe
    what is already there.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    table, id_col = _table(actor_type)
    row = conn.execute(
        f"SELECT quantity FROM {table} WHERE {id_col} = ? AND item_name = ?",
        (actor_id, item),
    ).fetchone()
    if row is None:
        conn.execute(
            f"INSERT INTO {table} ({id_col}, item_name, quantity, metadata) "
            "VALUES (?, ?, ?, ?)",
            (actor_id, item, amount, metadata),
        )
        return amount
    total = int(row["quantity"]) + amount
    if metadata is not None:
        conn.execute(
            f"UPDATE {table} SET quantity = ?, metadata = ? "
            f"WHERE {id_col} = ? AND item_name = ?",
            (total, metadata, actor_id, item),
        )
    else:
        conn.execute(
            f"UPDATE {table} SET quantity = ? "
            f"WHERE {id_col} = ? AND item_name = ?",
            (total, actor_id, item),
        )
    return total


def remove(
    conn: sqlite3.Connection,
    actor_type: str,
    actor_id: str,
    item: str,
    amount: int,
) -> bool:
    """Reserve ``amount`` of ``item``; False when the holder is short.

    Emptied rows vanish; the metadata blob is untouched on partial
    reservations.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    table, id_col = _table(actor_type)
    row = conn.execute(
        f"SELECT quantity FROM {table} WHERE {id_col} = ? AND item_name = ?",
        (actor_id, item),
    ).fetchone()
    if row is None or int(row["quantity"]) < amount:
        return False
    remaining = int(row["quantity"]) - amount
    if remaining <= 0:
        conn.execute(
            f"DELETE FROM {table} WHERE {id_col} = ? AND item_name = ?",
            (actor_id, item),
        )
    else:
        conn.execute(
            f"UPDATE {table} SET quantity = ? "
            f"WHERE {id_col} = ? AND item_name = ?",
            (remaining, actor_id, item),
        )
    return True
