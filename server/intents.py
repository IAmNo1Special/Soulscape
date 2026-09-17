import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from typing import Any

from . import database


def canonical_intent(message: dict[str, Any]) -> str:
    body = {k: v for k, v in message.items() if k not in ("type", "signature")}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def sign_intent(message: dict[str, Any], hmac_key: str) -> str:
    mac = hmac.new(
        hmac_key.encode(), canonical_intent(message).encode(), hashlib.sha256
    )
    return mac.hexdigest()


def _validate_move_to(
    message: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        x = float(message["x"])
        y = float(message["y"])
    except (KeyError, TypeError, ValueError):
        return None, "BAD_PAYLOAD"
    if not math.isfinite(x) or not math.isfinite(y):
        return None, "BAD_PAYLOAD"
    return {"x": x, "y": y}, None


def _validate_market_list(
    message: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    item = message.get("item")
    seller_soul_id = message.get("seller_soul_id")
    try:
        price = float(message["price"])
    except (KeyError, TypeError, ValueError):
        return None, "BAD_PAYLOAD"
    if (
        not isinstance(item, dict)
        or not isinstance(seller_soul_id, str)
        or not seller_soul_id
        or not math.isfinite(price)
        or price <= 0
    ):
        return None, "BAD_PAYLOAD"
    payload: dict[str, Any] = {
        "item": item,
        "price": price,
        "seller_soul_id": seller_soul_id,
    }
    if message.get("listing_id") is not None:
        if not isinstance(message["listing_id"], str) or not message["listing_id"]:
            return None, "BAD_PAYLOAD"
        payload["listing_id"] = message["listing_id"]
    if message.get("seller_name") is not None:
        if not isinstance(message["seller_name"], str):
            return None, "BAD_PAYLOAD"
        payload["seller_name"] = message["seller_name"]
    return payload, None


def _validate_market_buy(
    message: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    listing_id = message.get("listing_id")
    buyer_soul_id = message.get("buyer_soul_id")
    if (
        not isinstance(listing_id, str)
        or not listing_id
        or not isinstance(buyer_soul_id, str)
        or not buyer_soul_id
    ):
        return None, "BAD_PAYLOAD"
    return {"listing_id": listing_id, "buyer_soul_id": buyer_soul_id}, None


def _validate_market_cancel(
    message: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    listing_id = message.get("listing_id")
    if not isinstance(listing_id, str) or not listing_id:
        return None, "BAD_PAYLOAD"
    return {"listing_id": listing_id}, None


def _validate_social_author(
    message: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    author_type = message.get("author_type")
    author_id = message.get("author_id")
    if author_type not in ("soul", "tamer"):
        return None, "BAD_PAYLOAD"
    if not isinstance(author_id, str) or not author_id:
        return None, "BAD_PAYLOAD"
    author_name = message.get("author_name", "")
    if not isinstance(author_name, str):
        return None, "BAD_PAYLOAD"
    return {"author_type": author_type, "author_id": author_id,
            "author_name": author_name}, None


def _validate_social_post(
    message: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    title = message.get("title")
    body = message.get("body")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(body, str)
        or not body.strip()
    ):
        return None, "BAD_PAYLOAD"
    author, error = _validate_social_author(message)
    if error is not None:
        return None, error
    assert author is not None
    author["title"] = title
    author["body"] = body
    return author, None


def _validate_social_reply(
    message: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    parent_id = message.get("parent_id")
    body = message.get("body")
    if not isinstance(parent_id, str) or not parent_id:
        return None, "BAD_PAYLOAD"
    if not isinstance(body, str) or not body.strip():
        return None, "BAD_PAYLOAD"
    if message.get("title"):
        return None, "BAD_PAYLOAD"
    author, error = _validate_social_author(message)
    if error is not None:
        return None, error
    assert author is not None
    author["parent_id"] = parent_id
    author["body"] = body
    return author, None


def _validate_social_edit(
    message: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    message_id = message.get("message_id")
    body = message.get("body")
    if not isinstance(message_id, str) or not message_id:
        return None, "BAD_PAYLOAD"
    if not isinstance(body, str) or not body.strip():
        return None, "BAD_PAYLOAD"
    return {"message_id": message_id, "body": body}, None


def _validate_social_delete(
    message: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    message_id = message.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        return None, "BAD_PAYLOAD"
    return {"message_id": message_id}, None


_KIND_VALIDATORS = {
    "move_to": _validate_move_to,
    "market_list": _validate_market_list,
    "market_buy": _validate_market_buy,
    "market_cancel": _validate_market_cancel,
    "social_post": _validate_social_post,
    "social_reply": _validate_social_reply,
    "social_edit": _validate_social_edit,
    "social_delete": _validate_social_delete,
}


def validate_payload(
    kind: str | None, message: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    validator = _KIND_VALIDATORS.get(kind or "")
    if validator is None:
        return None, "UNKNOWN_INTENT_KIND"
    return validator(message)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    if out.get("payload"):
        out["payload"] = json.loads(out["payload"])
    if out.get("result"):
        out["result"] = json.loads(out["result"])
    return out


def enqueue_intent(
    session_id: str,
    nonce: str,
    custodian_id: str | None,
    soul_id: str,
    kind: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    intent_id = "int_" + secrets.token_urlsafe(16)
    now = time.time()
    with database.get_db() as conn:
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
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        row = conn.execute(
            "SELECT * FROM intents WHERE session_id = ? AND nonce = ?",
            (session_id, nonce),
        ).fetchone()
        assert row is not None
        return _row_to_dict(row)


def get_intent_by_nonce(session_id: str, nonce: str) -> dict[str, Any] | None:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM intents WHERE session_id = ? AND nonce = ?",
            (session_id, nonce),
        ).fetchone()
        return _row_to_dict(row) if row else None


def pending_intents(limit: int = 500) -> list[dict[str, Any]]:
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM intents WHERE status = 'pending' "
            "ORDER BY created_at ASC, rowid ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]


def _set_status(intent_id: str, status: str, result: dict[str, Any] | None) -> None:
    with database.get_db() as conn:
        conn.execute(
            "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
            (status, json.dumps(result) if result is not None else None, intent_id),
        )
        conn.commit()


def mark_adjudicated(intent_id: str, result: dict[str, Any] | None = None) -> None:
    _set_status(intent_id, "adjudicated", result)


def mark_rejected(intent_id: str, result: dict[str, Any] | None = None) -> None:
    _set_status(intent_id, "rejected", result)
