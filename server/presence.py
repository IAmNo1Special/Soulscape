"""Tamer presence pipeline, server side (issue #28).

Privacy contract (hard): the only presence data the Hub ever stores is the
redacted allowlist the client ships -- presence state, coarse idle bucket,
discrete events, and (only with explicit opt-in) a coarse app category.
Exact timestamps, window titles, paths, URLs, keystrokes, content,
screenshots, mic/cam are never accepted: the strict schema below rejects
unknown fields and unknown enum values outright, at ingress AND at
adjudication.

Cadence: clients ship on change plus a 60 s heartbeat. The Hub marks a
tamer `away` after 3 missed heartbeats (180 s) -- derived at read time, so
no writer is needed to flip the flag.

Tamer-return escalation: when a stored record shows an absence of >= 30 min
(presence was idle/locked) and a fresh report shows the tamer back, every
soul custodied by that tamer gets #25's `note_tamer_return` escalation.
"""

import logging
import secrets
import time
from typing import Any

from . import database
from . import determinism

logger = logging.getLogger("soulscape_hub")

PRESENCE_ACTIVE = "active"
PRESENCE_IDLE = "idle"
PRESENCE_LOCKED = "locked"
PRESENCE_AWAY = "away"
PRESENCE_VALUES = (
    PRESENCE_ACTIVE,
    PRESENCE_IDLE,
    PRESENCE_LOCKED,
    PRESENCE_AWAY,
)

IDLE_BUCKETS = ("0-5", "5-30", "30+")

EVENT_TAMER_RETURN = "tamer_return"
EVENT_LOCK = "lock"
EVENT_UNLOCK = "unlock"
PRESENCE_EVENTS = (EVENT_TAMER_RETURN, EVENT_LOCK, EVENT_UNLOCK)

APP_CATEGORIES = frozenset({"game", "browser", "media", "chat", "work", "other"})

_ALLOWLISTED_KEYS = frozenset({"presence", "idle_bucket", "event", "app_category"})

KIND_TAMER_PRESENCE = "tamer_presence"

HEARTBEAT_SECONDS = 60.0
STALENESS_BEATS = 3
STALENESS_SECONDS = HEARTBEAT_SECONDS * STALENESS_BEATS
TAMER_RETURN_ABSENCE_SECONDS = 30 * 60.0


def _init_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tamer_presence (
            tamer_id TEXT PRIMARY KEY,
            presence TEXT NOT NULL,
            idle_bucket TEXT NOT NULL,
            last_event TEXT,
            app_category TEXT,
            updated_at REAL NOT NULL
        )
        """
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tamers)")}
    if "presence_app_opt_in" not in cols:
        conn.execute(
            "ALTER TABLE tamers ADD COLUMN presence_app_opt_in "
            "INTEGER NOT NULL DEFAULT 0"
        )


def init_presence_schema() -> None:
    with database.get_db() as conn:
        _init_schema(conn)
        conn.commit()


def validate_presence_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Strict allowlist validation of a redacted presence payload.

    Rejects unknown fields, unknown enum values, and `app_category`
    values outside the closed category set. Returns the canonical
    payload (exactly the allowlisted keys) or (None, reason).
    """
    if not isinstance(payload, dict):
        return None, "BAD_PAYLOAD"
    unknown = set(payload) - _ALLOWLISTED_KEYS
    if unknown:
        return None, f"UNKNOWN_FIELDS:{sorted(unknown)[0]}"
    presence = payload.get("presence")
    idle_bucket = payload.get("idle_bucket")
    if presence not in PRESENCE_VALUES:
        return None, "BAD_PRESENCE"
    if idle_bucket not in IDLE_BUCKETS:
        return None, "BAD_IDLE_BUCKET"
    event = payload.get("event")
    if event is not None and event not in PRESENCE_EVENTS:
        return None, "BAD_EVENT"
    app_category = payload.get("app_category")
    if app_category is not None and app_category not in APP_CATEGORIES:
        return None, "BAD_APP_CATEGORY"
    canonical: dict[str, Any] = {
        "presence": presence,
        "idle_bucket": idle_bucket,
    }
    if event is not None:
        canonical["event"] = event
    if app_category is not None:
        canonical["app_category"] = app_category
    return canonical, None


def get_app_opt_in(tamer_id: str) -> bool:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT presence_app_opt_in FROM tamers WHERE tamer_id = ?",
            (tamer_id,),
        ).fetchone()
        return bool(row["presence_app_opt_in"]) if row else False


def set_app_opt_in(tamer_id: str, enabled: bool) -> bool:
    with database.get_db() as conn:
        cur = conn.execute(
            "UPDATE tamers SET presence_app_opt_in = ? WHERE tamer_id = ?",
            (1 if enabled else 0, tamer_id),
        )
        conn.commit()
        return cur.rowcount > 0


def _raw_presence(tamer_id: str) -> dict | None:
    """The stored row without staleness derivation."""
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT presence, idle_bucket, last_event, app_category, updated_at "
            "FROM tamer_presence WHERE tamer_id = ?",
            (tamer_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def get_presence(tamer_id: str | None, now: float | None = None) -> dict | None:
    """Current redacted presence for a tamer, or None when unknown.

    Never fabricated: returns None when no report was ever stored.
    Staleness is derived at read time -- after STALENESS_BEATS missed
    heartbeats the tamer reads as `away`.
    """
    if not tamer_id:
        return None
    if now is None:
        now = time.time()
    row = _raw_presence(tamer_id)
    if row is None:
        return None
    record = {
        "presence": row["presence"],
        "idle_bucket": row["idle_bucket"],
        "last_event": row["last_event"],
        "app_category": row["app_category"],
        "updated_at": row["updated_at"],
        "stale": False,
    }
    if now - float(row["updated_at"]) > STALENESS_SECONDS:
        record["presence"] = PRESENCE_AWAY
        record["idle_bucket"] = "30+"
        record["stale"] = True
    return record


def record_presence(
    tamer_id: str,
    payload: dict[str, Any],
    now: float | None = None,
) -> dict:
    """Store a validated redacted presence report. Returns the stored row."""
    if now is None:
        now = time.time()
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO tamer_presence "
            "(tamer_id, presence, idle_bucket, last_event, app_category, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tamer_id) DO UPDATE SET "
            "presence = excluded.presence, "
            "idle_bucket = excluded.idle_bucket, "
            "last_event = excluded.last_event, "
            "app_category = excluded.app_category, "
            "updated_at = excluded.updated_at",
            (
                tamer_id,
                payload["presence"],
                payload["idle_bucket"],
                payload.get("event"),
                payload.get("app_category"),
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT tamer_id, presence, idle_bucket, last_event, app_category, "
            "updated_at FROM tamer_presence WHERE tamer_id = ?",
            (tamer_id,),
        ).fetchone()
        return dict(row)


def _representative_soul_id(tamer_id: str) -> str:
    """A soul_id for the NOT NULL intents column.

    The presence intent is tamer-scoped; adjudication keys on
    custodian_id and never on soul_id. Prefer a real custodied soul so
    intent rows stay joinable; fall back to a synthetic tamer key when
    the tamer has no souls yet.
    """
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT soul_id FROM souls "
            "WHERE COALESCE(custodian_id, owner_id) = ? LIMIT 1",
            (tamer_id,),
        ).fetchone()
    if row is not None:
        return row["soul_id"]
    return f"tamer:{tamer_id}"


def enqueue_presence_intent(
    session_id: str,
    nonce: str,
    tamer_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Durable enqueue of a validated tamer_presence intent (pre-ack)."""
    from . import intents

    validated, error = validate_presence_payload(payload)
    if error is not None:
        raise ValueError(f"invalid presence payload: {error}")
    assert validated is not None
    return intents.enqueue_intent(
        session_id,
        nonce,
        tamer_id,
        _representative_soul_id(tamer_id),
        KIND_TAMER_PRESENCE,
        validated,
    )


def _note_tamer_return(tamer_id: str, now: float) -> int:
    """Fire #25's tamer_return escalation on the tamer's souls."""
    from .agents import deliberation

    tracker = deliberation.tracker()
    count = 0
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT soul_id FROM souls WHERE COALESCE(custodian_id, owner_id) = ?",
            (tamer_id,),
        ).fetchall()
    for row in rows:
        tracker.note_tamer_return(row["soul_id"], now)
        count += 1
    return count


def adjudicate_presence_intent(tick, intent: dict[str, Any]) -> None:
    """Tick-boundary adjudication: strict re-validation, then store.

    The tamer identity comes from the intent's custodian_id (set from the
    authenticated identity at ingress) -- never from client fields, so a
    tamer can only ever report their own presence.
    """
    from . import intents

    intent_id = intent["intent_id"]
    tamer_id = intent.get("custodian_id")
    # Issue #38: presence timestamps ride the adjudication clock so a
    # seeded replay writes identical rows.
    now = determinism.tick_now(tick)
    payload = intent.get("payload") or {}
    validated, error = validate_presence_payload(payload)
    if error is not None or validated is None:
        intents.mark_rejected(intent_id, {"reason": f"presence_schema:{error}"})
        logger.warning("tamer_presence rejected at adjudication: %s", error)
        return
    if not tamer_id:
        intents.mark_rejected(intent_id, {"reason": "presence_no_tamer"})
        return
    if validated.get("app_category") is not None and not get_app_opt_in(tamer_id):
        intents.mark_rejected(intent_id, {"reason": "presence_app_category_no_opt_in"})
        logger.warning(
            "tamer_presence with app_category but no opt-in: tamer=%s", tamer_id
        )
        return
    previous = _raw_presence(tamer_id)
    record_presence(tamer_id, validated, now)
    returned = False
    if previous is not None:
        absent_long = now - float(previous["updated_at"]) >= (
            TAMER_RETURN_ABSENCE_SECONDS
        )
        was_away = previous["presence"] in (
            PRESENCE_IDLE,
            PRESENCE_LOCKED,
            PRESENCE_AWAY,
        )
        back_now = validated["presence"] in (PRESENCE_ACTIVE, PRESENCE_IDLE)
        if absent_long and was_away and back_now:
            _note_tamer_return(tamer_id, now)
            returned = True
    # Issue #33: an unlock is a wake. Generate the overnight recap when
    # due (>22 h) and emit at most one morning-note bubble per cycle.
    # Issue #38: suppressed in replay -- recap generation is an
    # auxiliary LLM-backed side effect, not the adjudication outcome.
    if validated.get("event") == EVENT_UNLOCK and getattr(tick, "replay", None) is None:
        from . import recap as recap_module

        try:
            recap_module.on_unlock(tamer_id, now=now)
        except Exception:
            logger.exception("morning-note hook failed: tamer=%s", tamer_id)
    intents.mark_adjudicated(
        intent_id,
        {"stored": True, "tamer_return": returned},
    )
    logger.info(
        "tamer_presence adjudicated: tamer=%s presence=%s bucket=%s return=%s",
        tamer_id,
        validated["presence"],
        validated["idle_bucket"],
        returned,
    )


def make_nonce() -> str:
    return "rest_" + secrets.token_urlsafe(16)
