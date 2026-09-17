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


_KIND_VALIDATORS = {"move_to": _validate_move_to}


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
