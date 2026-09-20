"""Unified threaded social messages via intent adjudication (issue #18).

One `messages` table replaces the split social_posts/social_replies
tables: parent_id is a self-FK (NULL = top-level post, Reddit-style
nesting), authors are typed ('soul'|'tamer'), titles live on posts only.

Money flow for a post/reply:
  1. Ingress (REST): the intent and, for soul-authored content, the
     escrow hold commit in ONE transaction (commit-before-ack). The
     cached souls.essence is decremented here; the ledger debit row
     lands at adjudication.
  2. Tick pump (or the in-request pump for REST): the adjudication
     inserts the message row, writes the ledger debit, and marks the
     escrow applied -- all in one BEGIN IMMEDIATE transaction together
     with the intent status and the journal event (RPO = 0).
  3. Refusal (bad parent, custody fail, depth exceeded): the escrow is
     released (cached balance refunded) in the same transaction that
     marks the intent rejected. No message row, no ledger rows.

Decisions (see issue #18 close comment):
  - Operator charge exemption removed, per the #17 precedent: an
    operator authoring as a soul charges that soul's wallet. The
    operator identity itself has no wallet, so operator calls must
    name a soul or tamer author.
  - Tamer-authored content is free: tamers have no essence wallet, and
    essence is a soul-scoped economy. Spam is gated by the 6/min
    social write rate limit (#9).
  - edit/delete are free and intent-adjudicated; only the original
    author (or the operator, audit-logged) may mutate.
  - Delete is a soft delete. Deleted nodes render as tombstones;
    children stay reachable. Re-deleting is idempotent success.
  - Depth cap 25 (root = 0): bounds recursion in the tree reader and
    rules out pathological nesting.
  - Titles are immutable after creation; only bodies are editable.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import time
from typing import Any

from . import database
from . import determinism
from . import dormancy
from . import persistence
from .intents import _row_to_dict as _intent_row_to_dict
from .market import LEDGER_DEBIT, _release_escrow

logger = logging.getLogger("soulscape_hub")

KIND_SOCIAL_POST = "social_post"
KIND_SOCIAL_REPLY = "social_reply"
KIND_SOCIAL_EDIT = "social_edit"
KIND_SOCIAL_DELETE = "social_delete"
SOCIAL_KINDS = {
    KIND_SOCIAL_POST,
    KIND_SOCIAL_REPLY,
    KIND_SOCIAL_EDIT,
    KIND_SOCIAL_DELETE,
}
PAID_KINDS = {KIND_SOCIAL_POST: 20.00, KIND_SOCIAL_REPLY: 8.00}
POST_COST = 20.00
REPLY_COST = 8.00

AUTHOR_SOUL = database.ACTOR_SOUL
AUTHOR_TAMER = database.ACTOR_TAMER

MAX_DEPTH = 25
TOMBSTONE_BODY = "[deleted]"


class SocialRefusal(Exception):
    """Business-logic refusal to enqueue or adjudicate a social intent."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def _sanitize(text: str) -> str:
    if not text:
        return ""
    clean = re.sub(r"<[^>]*>", "", text)
    clean = clean.replace("[", "&#91;").replace("]", "&#93;")
    clean = clean.replace("{", "&#123;").replace("}", "&#125;")
    return clean[:2000].strip()


def _new_message_id(
    tick: Any = None, intent_id: str | None = None
) -> str:
    # Issue #38: minted ids are record-and-replayed in the replay CLI
    # (they are part of the recorded result) and secrets-based live.
    return determinism.tick_gen_id(
        tick,
        intent_id or "",
        "message_id",
        lambda: "msg_" + secrets.token_urlsafe(9),
    )


def _check_author(
    conn: sqlite3.Connection,
    custodian_id: str | None,
    intent_soul_id: str,
    author_type: str,
    author_id: str,
) -> None:
    """Validate that the intent's identity may author as (type, id).

    custodian_id None means operator: may author as any existing soul
    or tamer. A soul acts as itself. A tamer authors as itself, or as
    a soul it holds custody of (the soul's wallet pays).
    """
    if author_type == AUTHOR_SOUL:
        exists = conn.execute(
            "SELECT custodian_id, owner_id FROM souls WHERE soul_id = ?",
            (author_id,),
        ).fetchone()
        if exists is None:
            raise SocialRefusal(
                "author_not_found", f"Author soul {author_id} not found"
            )
        if custodian_id is None:
            return
        if author_id == intent_soul_id:
            return
        if (exists["custodian_id"] or exists["owner_id"]) == custodian_id:
            return
        raise SocialRefusal("custody", "No custody of the authoring soul")
    if author_type == AUTHOR_TAMER:
        exists = conn.execute(
            "SELECT tamer_id FROM tamers WHERE tamer_id = ?", (author_id,)
        ).fetchone()
        if exists is None:
            raise SocialRefusal(
                "author_not_found", f"Author tamer {author_id} not found"
            )
        if custodian_id is None:
            return
        if author_id != intent_soul_id:
            raise SocialRefusal("custody", "Tamers author as themselves")
        return
    raise SocialRefusal("bad_payload", f"Unknown author_type {author_type!r}")


def _paid_amount(author_type: str, kind: str) -> float:
    if author_type == AUTHOR_SOUL:
        return PAID_KINDS[kind]
    return 0.0


def _hold_social_escrow(
    conn: sqlite3.Connection,
    intent_id: str,
    author_soul_id: str,
    amount: float,
) -> None:
    """Hold the author soul's funds for a paid social intent.

    Runs inside the enqueue transaction. Raises SocialRefusal when the
    soul is missing or short on essence.
    """
    essence_before = dormancy.cached_essence(conn, author_soul_id)
    cursor = conn.execute(
        "UPDATE souls SET essence = essence - ? WHERE soul_id = ? AND essence >= ?",
        (amount, author_soul_id, amount),
    )
    if cursor.rowcount == 0:
        exists = conn.execute(
            "SELECT 1 FROM souls WHERE soul_id = ?", (author_soul_id,)
        ).fetchone()
        if exists is None:
            raise SocialRefusal(
                "author_not_found", f"Author soul {author_soul_id} not found"
            )
        raise SocialRefusal(
            "insufficient_funds",
            f"Insufficient essence to post (needs {amount})",
        )
    # A hold that drains the author to 0 freezes it (dormancy is
    # derived); the flip is journaled.
    dormancy.note_essence_change(conn, author_soul_id, essence_before or 0.0)
    conn.execute(
        "INSERT INTO escrows "
        "(escrow_id, intent_id, soul_id, amount, status, created_at) "
        "VALUES (?, ?, ?, ?, 'held', ?)",
        (
            "esc_" + secrets.token_urlsafe(12),
            intent_id,
            author_soul_id,
            amount,
            time.time(),
        ),
    )


def _require_held_escrow(conn: sqlite3.Connection, intent_id: str) -> None:
    """Refuse adjudication when the preface hold is missing.

    The hold and the intent insert commit atomically at enqueue, so a
    missing hold means a malformed state; refusing keeps the message,
    debit, and escrow from diverging.
    """
    held = conn.execute(
        "SELECT escrow_id FROM escrows WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    ).fetchone()
    if held is None:
        raise SocialRefusal("escrow_missing", "No held escrow for paid social intent")


def message_depth(conn: sqlite3.Connection, message_id: str) -> int | None:
    """Depth of a message (root post = 0). None when missing."""
    row = conn.execute(
        """
        WITH RECURSIVE chain(id, depth) AS (
            SELECT message_id, 0 FROM messages WHERE message_id = ?
            UNION ALL
            SELECT m.parent_id, chain.depth + 1
            FROM messages m JOIN chain ON m.message_id = chain.id
            WHERE m.parent_id IS NOT NULL
        )
        SELECT MAX(depth) AS depth FROM chain
        """,
        (message_id,),
    ).fetchone()
    if row is None or row["depth"] is None:
        return None
    return int(row["depth"])


def _check_parent(conn: sqlite3.Connection, parent_id: str) -> None:
    row = conn.execute(
        "SELECT deleted FROM messages WHERE message_id = ?", (parent_id,)
    ).fetchone()
    if row is None:
        raise SocialRefusal("parent_not_found", f"Target message {parent_id} not found")
    if row["deleted"]:
        raise SocialRefusal("parent_deleted", "Cannot reply to a deleted message")
    depth = message_depth(conn, parent_id)
    assert depth is not None
    if depth + 1 > MAX_DEPTH:
        raise SocialRefusal(
            "depth_exceeded", f"Reply depth exceeds the cap of {MAX_DEPTH}"
        )


def _check_mutation_custody(
    conn: sqlite3.Connection,
    custodian_id: str | None,
    intent_soul_id: str,
    message: sqlite3.Row,
) -> None:
    if custodian_id is None:
        return
    if (
        message["author_type"] == AUTHOR_TAMER
        and message["author_id"] == intent_soul_id
    ):
        return
    if message["author_type"] == AUTHOR_SOUL and message["author_id"] == intent_soul_id:
        return
    raise SocialRefusal("custody", "Only the original author may mutate this message")


def _get_message(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    if row is None:
        raise SocialRefusal("message_not_found", "Message not found or unauthorized")
    return row


def enqueue_social_intent(
    session_id: str,
    nonce: str,
    custodian_id: str | None,
    soul_id: str,
    kind: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Enqueue a social intent with commit-before-ack.

    The intent insert and, for soul-authored posts/replies, the escrow
    hold commit in ONE transaction. Returns (record, created); a
    duplicate (session_id, nonce) returns the existing record with
    created=False and never double-holds funds. Raises SocialRefusal
    on preface failures (no intent row is written).
    """
    if kind not in SOCIAL_KINDS:
        raise ValueError(f"not a social intent kind: {kind}")
    clean = dict(payload)
    if kind in (KIND_SOCIAL_POST, KIND_SOCIAL_REPLY):
        clean["title"] = _sanitize(str(payload.get("title") or ""))
        clean["body"] = _sanitize(str(payload.get("body") or ""))
        clean["author_name"] = _sanitize(str(payload.get("author_name") or ""))
        if kind == KIND_SOCIAL_POST and not clean["title"]:
            raise SocialRefusal("title_required", "Posts require a non-empty title")
    elif kind == KIND_SOCIAL_EDIT:
        clean["body"] = _sanitize(str(payload.get("body") or ""))
        if not clean["body"]:
            raise SocialRefusal("bad_payload", "Edit body must be non-empty")
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
                        json.dumps(clean),
                        now,
                    ),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            if created:
                # Dormancy (issue #22): dormant souls cannot author social
                # content -- statues don't act. Tamer-authored content is
                # exempt (tamers have no wallet; the intent soul is the
                # tamer, which is never dormant).
                acting_soul: str | None = None
                if kind in (KIND_SOCIAL_POST, KIND_SOCIAL_REPLY):
                    if clean.get("author_type") == AUTHOR_SOUL:
                        acting_soul = clean.get("author_id")
                elif kind in (KIND_SOCIAL_EDIT, KIND_SOCIAL_DELETE):
                    acting_soul = soul_id
                if acting_soul is not None and dormancy.soul_is_dormant(
                    conn, acting_soul
                ):
                    raise SocialRefusal(
                        "soul_dormant",
                        "A dormant (unfunded) soul cannot author social content",
                    )
            if created and kind in PAID_KINDS:
                author_type = clean["author_type"]
                author_id = clean["author_id"]
                _check_author(conn, custodian_id, soul_id, author_type, author_id)
                if kind == KIND_SOCIAL_REPLY:
                    _check_parent(conn, clean["parent_id"])
                amount = _paid_amount(author_type, kind)
                if amount > 0:
                    _hold_social_escrow(conn, intent_id, author_id, amount)
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


def _apply_post(
    conn: sqlite3.Connection,
    tick: Any,
    intent: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    author_type = payload["author_type"]
    author_id = payload["author_id"]
    _check_author(
        conn, intent["custodian_id"], intent["soul_id"], author_type, author_id
    )
    # Dormancy (issue #22): re-checked at adjudication because WS
    # intents bypass the REST enqueue -- a soul that drained between
    # enqueue and adjudication cannot publish.
    if author_type == AUTHOR_SOUL and dormancy.soul_is_dormant(conn, author_id):
        raise SocialRefusal(
            "soul_dormant", "A dormant (unfunded) soul cannot post"
        )
    if not payload.get("title"):
        raise SocialRefusal("title_required", "Posts require a non-empty title")
    amount = _paid_amount(author_type, KIND_SOCIAL_POST)
    if amount > 0:
        _require_held_escrow(conn, intent["intent_id"])
    message_id = _new_message_id(tick, intent["intent_id"])
    # Issue #38: message + ledger timestamps ride the adjudication
    # clock so a seeded replay writes identical rows.
    now = determinism.tick_now(tick) if now is None else now
    conn.execute(
        "INSERT INTO messages "
        "(message_id, parent_id, author_type, author_id, author_name, "
        "title, body, created_at, edited_at, deleted) "
        "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, NULL, 0)",
        (
            message_id,
            author_type,
            author_id,
            payload.get("author_name", ""),
            payload["title"],
            payload.get("body", ""),
            now,
        ),
    )
    if amount > 0:
        conn.execute(
            "UPDATE escrows SET status = 'applied' "
            "WHERE intent_id = ? AND status = 'held'",
            (intent["intent_id"],),
        )
        conn.executemany(
            "INSERT INTO ledger "
            "(tick_id, intent_id, entry_type, soul_id, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(tick.tick_id, intent["intent_id"], LEDGER_DEBIT, author_id, amount, now)],
        )
    return {"message_id": message_id, "cost": amount}


def _apply_reply(
    conn: sqlite3.Connection,
    tick: Any,
    intent: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    author_type = payload["author_type"]
    author_id = payload["author_id"]
    _check_author(
        conn, intent["custodian_id"], intent["soul_id"], author_type, author_id
    )
    _check_parent(conn, payload["parent_id"])
    # Dormancy (issue #22): see _apply_post -- WS intents adjudicate here.
    if author_type == AUTHOR_SOUL and dormancy.soul_is_dormant(conn, author_id):
        raise SocialRefusal(
            "soul_dormant", "A dormant (unfunded) soul cannot reply"
        )
    amount = _paid_amount(author_type, KIND_SOCIAL_REPLY)
    if amount > 0:
        _require_held_escrow(conn, intent["intent_id"])
    message_id = _new_message_id(tick, intent["intent_id"])
    # Issue #38: see _apply_post -- timestamps ride the tick clock.
    now = determinism.tick_now(tick) if now is None else now
    conn.execute(
        "INSERT INTO messages "
        "(message_id, parent_id, author_type, author_id, author_name, "
        "title, body, created_at, edited_at, deleted) "
        "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, NULL, 0)",
        (
            message_id,
            payload["parent_id"],
            author_type,
            author_id,
            payload.get("author_name", ""),
            payload.get("body", ""),
            now,
        ),
    )
    if amount > 0:
        conn.execute(
            "UPDATE escrows SET status = 'applied' "
            "WHERE intent_id = ? AND status = 'held'",
            (intent["intent_id"],),
        )
        conn.executemany(
            "INSERT INTO ledger "
            "(tick_id, intent_id, entry_type, soul_id, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(tick.tick_id, intent["intent_id"], LEDGER_DEBIT, author_id, amount, now)],
        )
    return {"message_id": message_id, "cost": amount}


def _apply_edit(
    conn: sqlite3.Connection,
    tick: Any,
    intent: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    message = _get_message(conn, payload["message_id"])
    if message["deleted"]:
        raise SocialRefusal("message_deleted", "Cannot edit a deleted message")
    # Dormancy (issue #22): a dormant soul cannot exercise its agency,
    # even through the operator (the operator acts AS the soul).
    if message["author_type"] == AUTHOR_SOUL and dormancy.soul_is_dormant(
        conn, message["author_id"]
    ):
        raise SocialRefusal(
            "soul_dormant", "A dormant (unfunded) soul cannot edit messages"
        )
    _check_mutation_custody(conn, intent["custodian_id"], intent["soul_id"], message)
    if intent["custodian_id"] is None:
        database.log_audit(
            conn,
            intent["soul_id"],
            "social_edit_as_operator",
            target_type="message",
            target_id=message["message_id"],
        )
    conn.execute(
        "UPDATE messages SET body = ?, edited_at = ? WHERE message_id = ?",
        # Issue #38: edited_at rides the adjudication clock.
        (
            payload["body"],
            determinism.tick_now(tick) if now is None else now,
            message["message_id"],
        ),
    )
    return {"message_id": message["message_id"]}


def _apply_delete(
    conn: sqlite3.Connection,
    tick: Any,
    intent: dict[str, Any],
    now: float | None = None,
) -> dict[str, Any]:
    payload = intent["payload"] or {}
    message = _get_message(conn, payload["message_id"])
    # Dormancy (issue #22): see _apply_edit.
    if message["author_type"] == AUTHOR_SOUL and dormancy.soul_is_dormant(
        conn, message["author_id"]
    ):
        raise SocialRefusal(
            "soul_dormant", "A dormant (unfunded) soul cannot delete messages"
        )
    _check_mutation_custody(conn, intent["custodian_id"], intent["soul_id"], message)
    if intent["custodian_id"] is None:
        database.log_audit(
            conn,
            intent["soul_id"],
            "social_delete_as_operator",
            target_type="message",
            target_id=message["message_id"],
        )
    conn.execute(
        "UPDATE messages SET deleted = 1 WHERE message_id = ?",
        (message["message_id"],),
    )
    return {"message_id": message["message_id"]}


def _settle_once(tick: Any, intent: dict[str, Any]) -> None:
    """Adjudicate one social intent in a single BEGIN IMMEDIATE
    transaction: message insert, escrow apply/release, ledger debit,
    intent status, and journal event all commit together (RPO = 0).

    The intent row is re-read inside the write transaction and settled
    only when still pending, so two racing pumps can never
    double-adjudicate the same intent. Refusals release the escrow and
    write no message or ledger rows.
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
                if kind == KIND_SOCIAL_POST:
                    result = _apply_post(conn, tick, intent, now=now)
                elif kind == KIND_SOCIAL_REPLY:
                    result = _apply_reply(conn, tick, intent, now=now)
                elif kind == KIND_SOCIAL_EDIT:
                    result = _apply_edit(conn, tick, intent, now=now)
                elif kind == KIND_SOCIAL_DELETE:
                    result = _apply_delete(conn, tick, intent, now=now)
                else:
                    raise SocialRefusal("unknown_kind", kind)
                status, event = "adjudicated", persistence.EVENT_INTENT_ADJUDICATED
            except SocialRefusal as refusal:
                if kind in PAID_KINDS:
                    _release_escrow(conn, intent_id)
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
            if intent["kind"] in PAID_KINDS:
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
                "social compensate failed for %s; boot reconcile is the net",
                intent_id,
            )


def adjudicate_social_intent(tick: Any, intent: dict[str, Any]) -> None:
    """Tick-pump entry point for social intents. Never raises."""
    try:
        _settle_once(tick, intent)
    except Exception:
        logger.exception("social adjudication failed: %s", intent["intent_id"])
        _compensate_reject(tick, intent)


def build_tree(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Build the nested message tree with a single query (no N+1).

    Deleted nodes render as tombstones with their children still
    nested underneath. Messages whose parent is missing (legacy
    orphans) are treated as roots.
    """
    rows = conn.execute(
        "SELECT message_id, parent_id, author_type, author_id, author_name, "
        "title, body, created_at, edited_at, deleted "
        "FROM messages ORDER BY created_at ASC, rowid ASC"
    ).fetchall()
    nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        tombstoned = bool(record["deleted"])
        body = TOMBSTONE_BODY if tombstoned else record["body"]
        node = {
            "message_id": record["message_id"],
            "parent_id": record["parent_id"],
            "author_type": record["author_type"],
            "author_id": record["author_id"],
            # Legacy content/timestamp aliases for the desktop client.
            "author_name": TOMBSTONE_BODY if tombstoned else record["author_name"],
            "title": None if tombstoned else record["title"],
            "body": body,
            "content": body,
            "created_at": record["created_at"],
            "timestamp": record["created_at"],
            "edited_at": record["edited_at"],
            "deleted": tombstoned,
            "replies": [],
        }
        nodes[record["message_id"]] = node
    roots: list[dict[str, Any]] = []
    for node in nodes.values():
        parent_id = node["parent_id"]
        parent = nodes.get(parent_id) if parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent["replies"].append(node)
    return roots
