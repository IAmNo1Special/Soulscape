"""
API endpoints for managing Soul states, inventory, and lifecycle.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import database
from ..models import SoulResponse, SoulUpdate
from ..security import UserIdentity, get_api_key, validate_secret, generate_token_expiry

logger = logging.getLogger("soulscape_hub")

router = APIRouter(prefix="/souls", tags=["Souls"], dependencies=[Depends(get_api_key)])


@router.get("", response_model=list[SoulResponse])
def get_souls(
    owner_id: str | None = Query(default=None),
    identity: UserIdentity = Depends(get_api_key),
):
    """Returns soul states. Optionally filter by owner_id."""
    # IDOR Mitigation: Users can only query their own ID
    if identity.is_user:
        if owner_id and owner_id != identity.owner_id:
            raise HTTPException(
                status_code=403,
                detail="You can only access souls you own.",
            )
        owner_id = identity.owner_id

    try:
        with database.get_db() as conn:
            cursor = conn.cursor()
            if owner_id:
                cursor.execute(
                    "SELECT soul_id, owner_id, name, first_name, family_name, "
                    "species, gender, level, essence, hp, max_hp, satiety, "
                    "hydration, xp, position, hometown, birth_date, activity, "
                    "mother_id, father_id, orb_color, aura_color, aura_visible, "
                    "stat_hp_base, stat_atk_base, stat_def_base, "
                    "stat_spa_base, stat_spd_base, stat_spe_base, stat_vis_base, "
                    "stat_hp_iv, stat_atk_iv, stat_def_iv, "
                    "stat_spa_iv, stat_spd_iv, stat_spe_iv, stat_vis_iv, "
                    "stat_hp_ev, stat_atk_ev, stat_def_ev, "
                    "stat_spa_ev, stat_spd_ev, stat_spe_ev, stat_vis_ev, "
                    "nature FROM souls WHERE owner_id = ?",
                    (owner_id,),
                )
            else:
                cursor.execute(
                    "SELECT soul_id, owner_id, name, first_name, family_name, "
                    "species, gender, level, essence, hp, max_hp, satiety, "
                    "hydration, xp, position, hometown, birth_date, activity, "
                    "mother_id, father_id, orb_color, aura_color, aura_visible, "
                    "stat_hp_base, stat_atk_base, stat_def_base, "
                    "stat_spa_base, stat_spd_base, stat_spe_base, stat_vis_base, "
                    "stat_hp_iv, stat_atk_iv, stat_def_iv, "
                    "stat_spa_iv, stat_spd_iv, stat_spe_iv, stat_vis_iv, "
                    "stat_hp_ev, stat_atk_ev, stat_def_ev, "
                    "stat_spa_ev, stat_spd_ev, stat_spe_ev, stat_vis_ev, "
                    "nature FROM souls"
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
                for key in ["orb_color", "aura_color", "hometown", "position"]:
                    if (
                        s.get(key)
                        and isinstance(s[key], str)
                        and s[key].startswith(("[", "{"))
                    ):
                        try:
                            s[key] = json.loads(s[key])
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
            return souls
    except Exception as e:
        logger.error(f"Error in get_souls: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
def update_souls(payload: SoulUpdate, identity: UserIdentity = Depends(get_api_key)):
    """Updates souls for a specific owner. Expects {owner_id: str, souls: [...]}."""
    # IDOR Mitigation: Derive owner_id from identity
    owner_id = payload.owner_id
    if identity.is_user:
        owner_id = identity.owner_id

    souls = payload.souls

    if not owner_id:
        raise HTTPException(status_code=400, detail="owner_id is required")

    try:
        with database.get_db() as conn:
            cursor = conn.cursor()

            # Fix Token Loss: Fetch existing secrets for this owner before deletion
            cursor.execute(
                "SELECT soul_id, secret FROM souls WHERE owner_id = ?",
                (owner_id,),
            )
            existing_secrets = {
                row["soul_id"]: row["secret"] for row in cursor.fetchall()
            }

            # IDOR/Takeover Fix: Verify that all provided souls are either new or owned by this owner
            for s in souls:
                sid = s.get("soul_id")
                cursor.execute("SELECT owner_id FROM souls WHERE soul_id = ?", (sid,))
                existing = cursor.fetchone()
                if existing and existing["owner_id"] != owner_id:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Soul {sid} is owned by another user.",
                    )

            cursor.execute(
                "DELETE FROM soul_inventory WHERE soul_id IN (SELECT soul_id FROM souls WHERE owner_id = ?)",
                (owner_id,),
            )
            cursor.execute("DELETE FROM souls WHERE owner_id = ?", (owner_id,))

            for s in souls:
                soul_id = s.get("soul_id")
                secret = s.get("secret") or existing_secrets.get(soul_id)
                if secret and secret != existing_secrets.get(soul_id):
                    validate_secret(secret)
                token_expiry = (
                    generate_token_expiry()
                    if secret and secret != existing_secrets.get(soul_id)
                    else None
                )

                orb = s.get("orb_color", [1, 1, 1])
                aura = s.get("aura_color", [1, 1, 1])

                cursor.execute(
                    """
                    INSERT OR REPLACE INTO souls (
                        soul_id, owner_id, name, first_name, family_name, species, gender,
                        level, xp, mother_id, father_id, hp, max_hp, satiety, hydration,
                        essence, position, hometown, birth_date, activity,
                        orb_color, aura_color, aura_visible,
                        stat_hp_base, stat_atk_base, stat_def_base, stat_spa_base, stat_spd_base, stat_spe_base, stat_vis_base,
                        stat_hp_iv, stat_atk_iv, stat_def_iv, stat_spa_iv, stat_spd_iv, stat_spe_iv, stat_vis_iv,
                        stat_hp_ev, stat_atk_ev, stat_def_ev, stat_spa_ev, stat_spd_ev, stat_spe_ev, stat_vis_ev,
                        nature, secret, token_expiry, is_revoked
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?,
                        ?, ?
                    )
                """,
                    (
                        soul_id,
                        owner_id,
                        s.get("name"),
                        s.get("first_name"),
                        s.get("family_name"),
                        s.get("species"),
                        s.get("gender"),
                        s.get("level"),
                        s.get("xp"),
                        s.get("mother_id"),
                        s.get("father_id"),
                        s.get("hp"),
                        s.get("max_hp") or 100,
                        s.get("satiety"),
                        s.get("hydration"),
                        s.get("essence"),
                        json.dumps(s.get("position", [0, 0])),
                        json.dumps(s.get("hometown")),
                        s.get("birth_date"),
                        s.get("activity"),
                        json.dumps(orb),
                        json.dumps(aura),
                        1 if s.get("aura_visible") else 0,
                        s.get("stat_hp_base", 0),
                        s.get("stat_atk_base", 0),
                        s.get("stat_def_base", 0),
                        s.get("stat_spa_base", 0),
                        s.get("stat_spd_base", 0),
                        s.get("stat_spe_base", 0),
                        s.get("stat_vis_base", 0),
                        s.get("stat_hp_iv", 0),
                        s.get("stat_atk_iv", 0),
                        s.get("stat_def_iv", 0),
                        s.get("stat_spa_iv", 0),
                        s.get("stat_spd_iv", 0),
                        s.get("stat_spe_iv", 0),
                        s.get("stat_vis_iv", 0),
                        s.get("stat_hp_ev", 0),
                        s.get("stat_atk_ev", 0),
                        s.get("stat_def_ev", 0),
                        s.get("stat_spa_ev", 0),
                        s.get("stat_spd_ev", 0),
                        s.get("stat_spe_ev", 0),
                        s.get("stat_vis_ev", 0),
                        s.get("nature", "Hardy"),
                        secret,
                        token_expiry,
                        0,
                    ),
                )

                soul_id = s.get("soul_id")
                inventory = s.get("inventory", {})
                if isinstance(inventory, dict):
                    items = inventory.get("items", [])
                    for item in items:
                        cursor.execute(
                            "INSERT INTO soul_inventory (soul_id, item_name, quantity) VALUES (?, ?, ?)",
                            (
                                soul_id,
                                item.get("name"),
                                item.get("quantity", 1),
                            ),
                        )
            conn.commit()
    except Exception as e:
        logger.error(f"Error in update_souls: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "success", "count": len(souls)}
