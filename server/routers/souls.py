"""
API endpoints for managing Soul states, inventory, and lifecycle.
"""

import json
import logging
import math
import secrets
import time
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from .. import database
from .. import dormancy
from .. import intents
from .. import persistence
from .. import plots
from .. import quips
from .. import recap
from .. import viewport
from ..models import FeedSoulRequest, QuipRequest, SoulResponse, SoulUpdate
from ..rate_limit import market_write_limit, read_limit
from ..world_tick import WorldTick
from ..security import (
    UserIdentity,
    assert_custody,
    get_api_key,
    make_secret_record,
    generate_token_expiry,
    require_scoped,
    validate_secret,
)

logger = logging.getLogger("soulscape_hub")

router = APIRouter(prefix="/souls", tags=["Souls"], dependencies=[Depends(get_api_key)])

# Newborn starter balance (issue #22). Single source of truth lives in
# dormancy.STARTER_GRANT; the mint ledger entry written at birth carries
# the same amount, so conservation accounting stays exact.
# *** TUNABLE -- see dormancy.STARTER_GRANT. ***
STARTING_ESSENCE = dormancy.STARTER_GRANT
MAX_LEVEL_PER_HOUR = 10.0
MAX_XP_PER_HOUR = 100000.0
MAX_POSITION = 4096.0
MAX_VELOCITY = 500.0
MAX_ITEM_QUANTITY = 99
MAX_ITEM_NAME_LEN = 64

_IV_KEYS = [
    "stat_hp_iv",
    "stat_atk_iv",
    "stat_def_iv",
    "stat_spa_iv",
    "stat_spd_iv",
    "stat_spe_iv",
    "stat_vis_iv",
]
_EV_KEYS = [
    "stat_hp_ev",
    "stat_atk_ev",
    "stat_def_ev",
    "stat_spa_ev",
    "stat_spd_ev",
    "stat_spe_ev",
    "stat_vis_ev",
]
_BASE_KEYS = [
    "stat_hp_base",
    "stat_atk_base",
    "stat_def_base",
    "stat_spa_base",
    "stat_spd_base",
    "stat_spe_base",
    "stat_vis_base",
]


def _to_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _to_int(value: Any, default: int) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return default
    return result


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def _lineage_id(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _validate_soul_state(
    s: dict, stored: dict | None, now: float
) -> tuple[dict, str | None]:
    soul_id = s.get("soul_id")
    if not soul_id or not isinstance(soul_id, str):
        return {}, "missing soul_id"
    out: dict[str, Any] = {}
    out["satiety"] = _clamp(_to_float(s.get("satiety"), 100.0), 0.0, 100.0)
    out["hydration"] = _clamp(_to_float(s.get("hydration"), 100.0), 0.0, 100.0)
    out["max_hp"] = _clamp(_to_float(s.get("max_hp"), 100.0) or 100.0, 1.0, 9999.0)
    out["hp"] = _clamp(_to_float(s.get("hp"), out["max_hp"]), 0.0, out["max_hp"])
    level = _clamp(_to_int(s.get("level"), 1), 1, 100)
    xp = max(0.0, _to_float(s.get("xp"), 0.0))
    if stored is not None:
        stored_level = _to_int(stored.get("level"), 1)
        stored_xp = max(0.0, _to_float(stored.get("xp"), 0.0))
        if level < stored_level:
            return {}, f"level decreased {stored_level} -> {level}"
        if xp < stored_xp:
            return {}, f"xp decreased {stored_xp} -> {xp}"
        elapsed_hours = max(
            (now - _to_float(stored.get("updated_at"), now)) / 3600.0, 1.0 / 3600.0
        )
        if (level - stored_level) / elapsed_hours > MAX_LEVEL_PER_HOUR:
            return {}, f"level gain too fast {stored_level} -> {level}"
        if (xp - stored_xp) / elapsed_hours > MAX_XP_PER_HOUR:
            return {}, f"xp gain too fast {stored_xp} -> {xp}"
    out["level"] = level
    out["xp"] = xp
    for key in _IV_KEYS:
        out[key] = int(_clamp(_to_int(s.get(key), 0), 0, 31))
    for key in _EV_KEYS:
        out[key] = int(_clamp(_to_int(s.get(key), 0), 0, 255))
    for key in _BASE_KEYS:
        out[key] = int(_clamp(_to_int(s.get(key), 100), 1, 255))
    pos = s.get("position", [0, 0])
    if (
        not isinstance(pos, (list, tuple))
        or len(pos) != 2
        or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in pos)
    ):
        stored_pos = [0, 0]
        if stored is not None and stored.get("position"):
            try:
                stored_pos = json.loads(stored["position"])
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        pos = stored_pos
    out["position"] = [
        float(_clamp(pos[0], 0.0, MAX_POSITION)),
        float(_clamp(pos[1], 0.0, MAX_POSITION)),
    ]
    if stored is None:
        cx, cy = plots.commons_center()
        out["position"] = [cx, cy]
    vel = s.get("velocity", [0, 0])
    if (
        not isinstance(vel, (list, tuple))
        or len(vel) != 2
        or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vel)
    ):
        stored_vel = [0, 0]
        if stored is not None and stored.get("velocity"):
            try:
                stored_vel = json.loads(stored["velocity"])
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        vel = stored_vel
    out["velocity"] = [
        float(_clamp(vel[0], -MAX_VELOCITY, MAX_VELOCITY)),
        float(_clamp(vel[1], -MAX_VELOCITY, MAX_VELOCITY)),
    ]
    return out, None


def _validate_inventory_items(inventory: Any) -> list[dict]:
    if not isinstance(inventory, dict):
        return []
    items = inventory.get("items", [])
    if not isinstance(items, list):
        return []
    clean = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name or not isinstance(name, str):
            continue
        try:
            quantity = int(float(item.get("quantity", 1)))
        except (TypeError, ValueError):
            continue
        clean.append(
            {
                "name": name[:MAX_ITEM_NAME_LEN],
                "quantity": int(_clamp(quantity, 1, MAX_ITEM_QUANTITY)),
            }
        )
    return clean


@router.get("", response_model=list[SoulResponse], dependencies=[Depends(read_limit)])
def get_souls(
    owner_id: str | None = Query(default=None),
    custodian_id: str | None = Query(default=None),
    identity: UserIdentity = Depends(require_scoped),
):
    """Returns soul states. Optionally filter by custodian_id
    (legacy owner_id accepted during the migration window)."""
    # IDOR Mitigation: non-operators are scoped to their own custody.
    effective = custodian_id or owner_id
    if not identity.is_operator:
        if effective and effective != identity.custodian_id:
            raise HTTPException(
                status_code=403,
                detail="Cross-custody access denied",
            )
        effective = identity.custodian_id

    try:
        with database.get_db() as conn:
            cursor = conn.cursor()
            if effective:
                if identity.is_operator and effective != identity.owner_id:
                    database.audit_log(
                        identity.id,
                        "souls_view_other_custodian",
                        target_type="custodian",
                        target_id=effective,
                    )
                cursor.execute(
                    "SELECT soul_id, owner_id, custodian_id, name, first_name, "
                    "family_name, species, gender, level, essence, hp, max_hp, "
                    "satiety, hydration, xp, position, velocity, hometown, "
                    "birth_date, activity, mother_id, father_id, orb_color, "
                    "aura_color, aura_visible, stat_hp_base, stat_atk_base, "
                    "stat_def_base, stat_spa_base, stat_spd_base, stat_spe_base, "
                    "stat_vis_base, stat_hp_iv, stat_atk_iv, stat_def_iv, "
                    "stat_spa_iv, stat_spd_iv, stat_spe_iv, stat_vis_iv, "
                    "stat_hp_ev, stat_atk_ev, stat_def_ev, stat_spa_ev, "
                    "stat_spd_ev, stat_spe_ev, stat_vis_ev, nature, "
                    "state, fed_flag "
                    "FROM souls WHERE COALESCE(custodian_id, owner_id) = ?",
                    (effective,),
                )
            else:
                if identity.is_operator:
                    database.audit_log(
                        identity.id,
                        "souls_view_all",
                        target_type="souls",
                        target_id="all",
                    )
                cursor.execute(
                    "SELECT soul_id, owner_id, custodian_id, name, first_name, "
                    "family_name, species, gender, level, essence, hp, max_hp, "
                    "satiety, hydration, xp, position, velocity, hometown, "
                    "birth_date, activity, mother_id, father_id, orb_color, "
                    "aura_color, aura_visible, stat_hp_base, stat_atk_base, "
                    "stat_def_base, stat_spa_base, stat_spd_base, stat_spe_base, "
                    "stat_vis_base, stat_hp_iv, stat_atk_iv, stat_def_iv, "
                    "stat_spa_iv, stat_spd_iv, stat_spe_iv, stat_vis_iv, "
                    "stat_hp_ev, stat_atk_ev, stat_def_ev, stat_spa_ev, "
                    "stat_spd_ev, stat_spe_ev, stat_vis_ev, nature, "
                    "state, fed_flag FROM souls"
                )
            souls = []
            for row in cursor.fetchall():
                s = dict(row)
                souls.append(s)

            soul_ids = [s["soul_id"] for s in souls]
            if not soul_ids:
                return souls
            inventory_items = []
            if soul_ids:
                placeholders = ",".join("?" for _ in soul_ids)
                cursor.execute(
                    f"SELECT * FROM soul_inventory WHERE soul_id IN ({placeholders})",
                    soul_ids,
                )
                inventory_items = cursor.fetchall()

            inv_map = {}
            for item in inventory_items:
                sid = item["soul_id"]
                if sid not in inv_map:
                    inv_map[sid] = {}
                inv_map[sid][item["item_name"]] = item["quantity"]

            for s in souls:
                s["inventory"] = inv_map.get(s["soul_id"], {})
                for key in [
                    "orb_color",
                    "aura_color",
                    "hometown",
                    "position",
                    "velocity",
                ]:
                    if (
                        s.get(key)
                        and isinstance(s[key], str)
                        and s[key].startswith(("[", "{"))
                    ):
                        try:
                            s[key] = json.loads(s[key])
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
                unflushed = persistence.dirty_get(s["soul_id"])
                if unflushed is not None:
                    if unflushed.get("position") is not None:
                        s["position"] = [
                            float(unflushed["position"][0]),
                            float(unflushed["position"][1]),
                        ]
                    if unflushed.get("velocity") is not None:
                        s["velocity"] = [
                            float(unflushed["velocity"][0]),
                            float(unflushed["velocity"][1]),
                        ]
            return souls
    except Exception as e:
        logger.error(f"Error in get_souls: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
def update_souls(
    payload: SoulUpdate,
    request: Request,
    identity: UserIdentity = Depends(require_scoped),
):
    """Updates souls for a custodian. Expects {custodian_id: str, souls: [...]}.
    Legacy owner_id is accepted during the migration window."""
    # IDOR Mitigation: non-operators are scoped to their own custody.
    custodian_id = payload.custodian_id or payload.owner_id
    if not custodian_id:
        raise HTTPException(
            status_code=400,
            detail="custodian_id (or legacy owner_id) is required",
        )
    assert_custody(identity, custodian_id)

    souls = payload.souls

    try:
        now = time.time()
        with database.get_db() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT soul_id, secret_hash, secret_prefix, token_expiry, "
                "essence, xp, level, position, velocity, updated_at "
                "FROM souls WHERE COALESCE(custodian_id, owner_id) = ?",
                (custodian_id,),
            )
            stored_rows = {row["soul_id"]: dict(row) for row in cursor.fetchall()}

            # IDOR/Takeover Fix: Verify that all provided souls are either new or owned by this custodian
            for s in souls:
                sid = s.get("soul_id")
                cursor.execute(
                    "SELECT custodian_id, owner_id FROM souls WHERE soul_id = ?",
                    (sid,),
                )
                existing = cursor.fetchone()
                existing_custodian = None
                if existing:
                    existing_custodian = (
                        existing["custodian_id"] or existing["owner_id"]
                    )
                if existing and existing_custodian != custodian_id:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Soul {sid} is owned by another user.",
                    )

            saved = 0
            skipped: list[dict] = []
            validated_souls: list[tuple[dict, dict]] = []
            saved_ids: list[str] = []
            # Newborn souls (no stored row) get the issue-#22 starter
            # grant: cached essence = STARTER_GRANT plus a `mint` ledger
            # row, so the ledger stays the truth behind the cache.
            newborn_ids: list[str] = []
            for s in souls:
                soul_id = s.get("soul_id")
                stored = stored_rows.get(soul_id) if soul_id else None
                validated, reason = _validate_soul_state(s, stored, now)
                if reason is not None:
                    logger.warning(f"Rejected soul save {soul_id}: {reason}")
                    skipped.append({"soul_id": soul_id, "reason": reason})
                    continue
                validated_souls.append((s, validated))

            if validated_souls:
                placeholders = ",".join("?" for _ in validated_souls)
                valid_ids = [s.get("soul_id") for s, _ in validated_souls]
                cursor.execute(
                    f"DELETE FROM soul_inventory WHERE soul_id IN ({placeholders})",
                    valid_ids,
                )
                cursor.execute(
                    f"DELETE FROM souls WHERE COALESCE(custodian_id, owner_id) = ? "
                    f"AND soul_id IN ({placeholders})",
                    [custodian_id, *valid_ids],
                )
            seen_ids = {s.get("soul_id") for s, _ in validated_souls}
            seen_ids |= {
                item["soul_id"] for item in skipped if item["soul_id"] is not None
            }
            stale_ids = [sid for sid in stored_rows if sid not in seen_ids]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                cursor.execute(
                    f"DELETE FROM soul_inventory WHERE soul_id IN ({placeholders})",
                    stale_ids,
                )
                cursor.execute(
                    f"DELETE FROM souls WHERE COALESCE(custodian_id, owner_id) = ? "
                    f"AND soul_id IN ({placeholders})",
                    [custodian_id, *stale_ids],
                )
            for s, validated in validated_souls:
                soul_id = s.get("soul_id")
                stored = stored_rows.get(soul_id)
                if stored is None:
                    # Truly new soul: the mint ledger row below explains
                    # the starter grant (issue #22).
                    newborn_ids.append(soul_id)
                secret = s.get("secret")
                existing_hash = stored.get("secret_hash") if stored else None
                secret_hash = existing_hash
                secret_prefix = stored.get("secret_prefix") if stored else None
                token_expiry = stored.get("token_expiry") if stored else None
                if secret:
                    if existing_hash and database.verify_secret_hash(
                        secret, existing_hash
                    ):
                        pass
                    else:
                        validate_secret(secret)
                        secret_hash, secret_prefix = make_secret_record(secret)
                        token_expiry = generate_token_expiry()
                        if existing_hash:
                            cursor.execute(
                                "DELETE FROM ws_sessions WHERE owner_id = ?",
                                (custodian_id,),
                            )
                essence = (
                    stored.get("essence")
                    if stored and stored.get("essence") is not None
                    else STARTING_ESSENCE
                )

                orb = s.get("orb_color", [1, 1, 1])
                aura = s.get("aura_color", [1, 1, 1])

                cursor.execute(
                    """
                    INSERT OR REPLACE INTO souls (
                        soul_id, owner_id, custodian_id, name, first_name, family_name, species, gender,
                        level, xp, mother_id, father_id, hp, max_hp, satiety, hydration,
                        essence, position, velocity, hometown, birth_date, activity,
                        orb_color, aura_color, aura_visible,
                        stat_hp_base, stat_atk_base, stat_def_base, stat_spa_base, stat_spd_base, stat_spe_base, stat_vis_base,
                        stat_hp_iv, stat_atk_iv, stat_def_iv, stat_spa_iv, stat_spd_iv, stat_spe_iv, stat_vis_iv,
                        stat_hp_ev, stat_atk_ev, stat_def_ev, stat_spa_ev, stat_spd_ev, stat_spe_ev, stat_vis_ev,
                        nature, secret_hash, secret_prefix, updated_at, token_expiry, is_revoked
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?
                    )
                """,
                    (
                        soul_id,
                        custodian_id,
                        custodian_id,
                        s.get("name"),
                        s.get("first_name"),
                        s.get("family_name"),
                        s.get("species"),
                        s.get("gender"),
                        validated["level"],
                        validated["xp"],
                        _lineage_id(s.get("mother_id")),
                        _lineage_id(s.get("father_id")),
                        validated["hp"],
                        validated["max_hp"],
                        validated["satiety"],
                        validated["hydration"],
                        essence,
                        json.dumps(validated["position"]),
                        json.dumps(validated["velocity"]),
                        json.dumps(s.get("hometown")),
                        s.get("birth_date"),
                        s.get("activity"),
                        json.dumps(orb),
                        json.dumps(aura),
                        1 if s.get("aura_visible") else 0,
                        validated["stat_hp_base"],
                        validated["stat_atk_base"],
                        validated["stat_def_base"],
                        validated["stat_spa_base"],
                        validated["stat_spd_base"],
                        validated["stat_spe_base"],
                        validated["stat_vis_base"],
                        validated["stat_hp_iv"],
                        validated["stat_atk_iv"],
                        validated["stat_def_iv"],
                        validated["stat_spa_iv"],
                        validated["stat_spd_iv"],
                        validated["stat_spe_iv"],
                        validated["stat_vis_iv"],
                        validated["stat_hp_ev"],
                        validated["stat_atk_ev"],
                        validated["stat_def_ev"],
                        validated["stat_spa_ev"],
                        validated["stat_spd_ev"],
                        validated["stat_spe_ev"],
                        validated["stat_vis_ev"],
                        s.get("nature", "Hardy"),
                        secret_hash,
                        secret_prefix,
                        now,
                        token_expiry,
                        0,
                    ),
                )

                for item in _validate_inventory_items(s.get("inventory", {})):
                    cursor.execute(
                        "INSERT INTO soul_inventory (soul_id, item_name, quantity) VALUES (?, ?, ?)",
                        (
                            soul_id,
                            item["name"],
                            item["quantity"],
                        ),
                    )
                saved += 1
                saved_ids.append(soul_id)
            # Newborn starter grants (issue #22): one `mint` ledger row
            # per newborn, in the same transaction as the soul INSERTs.
            if newborn_ids:
                tick = getattr(request.app.state, "world_tick", None)
                tick_id = tick.tick_id if tick is not None else 0
                for soul_id in newborn_ids:
                    dormancy.mint_starter_grant(conn, tick_id, soul_id)
            conn.commit()
            for soul_id in saved_ids:
                persistence.invalidate(soul_id)
    except Exception as e:
        logger.error(f"Error in update_souls: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "success", "count": saved, "skipped": skipped}


_FEED_REFUSAL_STATUS = {
    "feeder_not_found": 404,
    "recipient_not_found": 404,
    "insufficient_funds": 400,
    "feeder_collapsed": 400,
    "custody": 403,
}

_FEED_REJECTION_STATUS = {
    "feeder_not_found": 404,
    "recipient_not_found": 404,
    "custody": 403,
    "not_collapsed": 400,
    "already_fed": 409,
    "escrow_short": 500,
}


def _settle_feed_intent(request: Request, record: dict) -> dict:
    """Pump the tick's intent queue once and return the fresh intent row."""
    tick = getattr(request.app.state, "world_tick", None)
    if tick is None:
        tick = WorldTick()
    tick.pump_intents()
    fresh = intents.get_intent_by_nonce(record["session_id"], record["nonce"])
    assert fresh is not None
    return fresh


@router.post("/feed", dependencies=[Depends(market_write_limit)])
def feed_soul(
    feed: FeedSoulRequest,
    request: Request,
    identity: UserIdentity = Depends(get_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Feed a collapsed stranger (issue #21).

    Any soul may feed any collapsed soul -- no custody requirement on the
    recipient. The 10-essence gift is escrowed at enqueue (commit-before-ack)
    and settles synchronously via one in-request intent pump, mirroring the
    #17 market pattern. Idempotency-Key makes retries return the original
    outcome instead of double-feeding.
    """
    from .. import biology

    feeder_id = feed.feeder_soul_id
    if identity.role == "user":
        # IDOR mitigation: plain soul-owners feed as themselves. Tamers keep
        # their chosen soul but must pass the custody check below (their
        # identity.id is a tamer ID, not a soul ID).
        feeder_id = identity.id
    soul_id = feeder_id
    custodian_id = None if identity.is_operator else (
        identity.custodian_id or identity.owner_id
    )
    payload = {
        "feeder_soul_id": feeder_id,
        "recipient_soul_id": feed.recipient_soul_id,
    }
    validated, error = intents.validate_payload(biology.KIND_FEED_SOUL, payload)
    if error is not None:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {error}")
    session_id = f"rest:{identity.id}"
    nonce = (
        idempotency_key.strip()
        if idempotency_key and idempotency_key.strip()
        else "rest_" + secrets.token_urlsafe(16)
    )
    try:
        record, _created = biology.enqueue_feed_soul(
            session_id, nonce, custodian_id, soul_id, biology.KIND_FEED_SOUL,
            validated,
        )
    except biology.BiologyRefusal as refusal:
        raise HTTPException(
            status_code=_FEED_REFUSAL_STATUS.get(refusal.reason, 400),
            detail=refusal.detail,
        )
    try:
        record = _settle_feed_intent(request, record)
    except Exception as e:
        logger.error(f"Error settling feed_soul intent: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    if record["status"] == "adjudicated":
        return {
            "status": "success",
            "recipient_soul_id": record["result"]["recipient_soul_id"],
            "gift": record["result"]["gift"],
        }
    if record["status"] == "pending":
        return {"status": "pending", "intent_id": record["intent_id"]}
    reason = (record["result"] or {}).get("reason", "internal")
    raise HTTPException(
        status_code=_FEED_REJECTION_STATUS.get(reason, 400),
        detail=(record["result"] or {}).get("detail", reason),
    )


@router.post("/{soul_id}/quip", dependencies=[Depends(market_write_limit)])
def request_quip(
    soul_id: str,
    body: QuipRequest,
    identity: UserIdentity = Depends(get_api_key),
):
    """Request a personalized quip from a soul (issue #30).

    Budget: at most 3 personalized quips per soul per UTC day; the 4th
    is rejected with 429 and a machine-readable reason the client must
    surface. Price: a fixed QUIP_PRICE_ESSENCE debit, charged immediately
    through the ledger before generation; insufficient essence is a 402.
    Generation rides the #25 flash tier (tamer vault keys); with no keys
    a labeled template fallback answers. The quip also goes out as a
    solicited ``bubble`` viewport op so the client renders it.
    """
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT soul_id, owner_id, custodian_id, name, species, "
            "COALESCE(essence, 0.0) AS essence FROM souls WHERE soul_id = ?",
            (soul_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Soul {soul_id} not found")
    if identity.role == "user":
        if soul_id != identity.id:
            raise HTTPException(
                status_code=403, detail="Soul-users may only quip as themselves"
            )
    else:
        assert_custody(identity, row["custodian_id"] or row["owner_id"])

    soul = dict(row)
    day = quips.quip_day()
    # Phase 1: atomically reserve a budget slot and verify funds under
    # one BEGIN IMMEDIATE, so concurrent requests cannot overspend the
    # daily budget. The reservation is released if generation or
    # charging fails below.
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if quips.quips_remaining(conn, soul_id) <= 0:
                conn.rollback()
                resets_at = datetime.combine(
                    datetime.now(timezone.utc).date() + timedelta(days=1),
                    dtime.min,
                    tzinfo=timezone.utc,
                ).isoformat()
                raise HTTPException(
                    status_code=429,
                    detail={
                        "reason": "budget_exhausted",
                        "message": "Quip budget exhausted: 3 personalized "
                        "quips per soul per day.",
                        "quips_remaining_today": 0,
                        "resets_at": resets_at,
                    },
                )
            balance = conn.execute(
                "SELECT COALESCE(essence, 0.0) FROM souls WHERE soul_id = ?",
                (soul_id,),
            ).fetchone()[0]
            if float(balance) < quips.QUIP_PRICE_ESSENCE:
                conn.rollback()
                raise HTTPException(
                    status_code=402,
                    detail={
                        "reason": "insufficient_essence",
                        "message": "Not enough essence for a quip.",
                        "price": quips.QUIP_PRICE_ESSENCE,
                        "essence": float(balance),
                    },
                )
            quips.record_quip(conn, soul_id, day)
            used = quips.get_quip_count(conn, soul_id, day)
            conn.commit()
        except HTTPException:
            raise
        except Exception:
            conn.rollback()
            raise

    # Phase 2: generate outside the write lock (LLM calls take seconds).
    try:
        gen = quips.generate_quip(soul, body.prompt)
    except Exception as exc:
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                quips.release_quip(conn, soul_id, day)
                conn.commit()
            except Exception:
                conn.rollback()
        raise HTTPException(
            status_code=502,
            detail={
                "reason": "generation_failed",
                "message": "Quip generation failed; budget slot released.",
            },
        ) from exc

    # Phase 3: charge the fixed price and write the ledger/usage rows.
    try:
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                essence_left = quips.charge_for_quip(conn, soul_id)
                quips.write_usage_row(conn, soul_id, gen)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except HTTPException as exc:
        if "Insufficient essence" in str(exc.detail):
            # Balance raced between reservation and charge: release the
            # slot and report 402.
            with database.get_db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    quips.release_quip(conn, soul_id, day)
                    conn.commit()
                except Exception:
                    conn.rollback()
            raise HTTPException(
                status_code=402,
                detail={
                    "reason": "insufficient_essence",
                    "message": "Not enough essence for a quip.",
                    "price": quips.QUIP_PRICE_ESSENCE,
                },
            ) from exc
        raise

    owner_id = soul["owner_id"] or soul["custodian_id"] or ""
    viewport.viewport.notify_bubble(
        owner_id, soul_id, gen["text"], kind="quip", solicited=True
    )
    return {
        "status": "success",
        "soul_id": soul_id,
        "quip": gen["text"],
        "essence_debited": quips.QUIP_PRICE_ESSENCE,
        "essence_remaining": essence_left,
        "quips_used_today": used,
        "quips_remaining_today": quips.QUIPS_PER_SOUL_PER_DAY - used,
        "fallback_used": gen["fallback_used"],
    }


@router.get("/{soul_id}/recaps", dependencies=[Depends(read_limit)])
def get_recaps(
    soul_id: str,
    limit: int = Query(default=30, ge=1, le=100),
    identity: UserIdentity = Depends(get_api_key),
):
    """Browsable overnight recaps for a soul, newest first (issue #33).

    Custody-scoped like the rest of the soul surface: soul-users may
    only read their own soul; tamers only souls in their custody.
    """
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT custodian_id, owner_id FROM souls WHERE soul_id = ?",
            (soul_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Soul {soul_id} not found")
    if identity.role == "user":
        if soul_id != identity.id:
            raise HTTPException(
                status_code=403, detail="Soul-users may only read their own recaps"
            )
    else:
        assert_custody(identity, row["custodian_id"] or row["owner_id"])
    return {"recaps": recap.list_recaps(soul_id, limit=limit)}
