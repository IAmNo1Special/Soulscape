"""Free first-soul grant for online custodians.

Manual soul spawn is an offline-only client action. Online, a
custodian's first soul is granted by the Hub -- on WS hello or on
their first POST /souls -- and later souls are earned in game through
sim intents. Operators keep full powers.

The builder here produces a ``souls_upsert``-compatible entry; the
write itself runs in the sim (``grant_free_soul`` command), which
checks custodian-emptiness and inserts atomically.
"""

from __future__ import annotations

import random
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from . import plots
from .security import generate_token_expiry, make_secret_record

MANUAL_MINT_DISABLED_REASON = "manual_mint_disabled"

FREE_SOUL_NAME = "Soul 1"
FREE_SOUL_SPECIES = "Soul"
FREE_SOUL_NATURE = "Hardy"
FREE_SOUL_BASE_STAT = 100


def _random_color() -> list[float]:
    return [random.random(), random.random(), random.random()]


def _hp_for_level_one(iv_hp: int) -> int:
    inner = (2 * FREE_SOUL_BASE_STAT + iv_hp) * 1 // 100
    return int(inner + 1 + 10)


def free_soul_entry() -> tuple[dict[str, Any], str]:
    """Build the upsert entry and plaintext secret for a free soul.

    Returns (entry, secret). The secret is disclosed once by the
    caller (POST response); the stored row keeps only its hash.
    """
    now = time.time()
    cx, cy = plots.commons_center()
    iv_hp = random.randint(0, 31)
    hp = _hp_for_level_one(iv_hp)
    secret = secrets.token_hex(16)
    secret_hash, secret_prefix = make_secret_record(secret)
    validated: dict[str, Any] = {
        "satiety": 100.0,
        "hydration": 100.0,
        "max_hp": float(hp),
        "hp": float(hp),
        "level": 1,
        "xp": 0.0,
        "position": [float(cx), float(cy)],
        "velocity": [0.0, 0.0],
    }
    for key in (
        "stat_hp_base",
        "stat_atk_base",
        "stat_def_base",
        "stat_spa_base",
        "stat_spd_base",
        "stat_spe_base",
        "stat_vis_base",
    ):
        validated[key] = FREE_SOUL_BASE_STAT
    validated["stat_hp_iv"] = iv_hp
    for key in (
        "stat_atk_iv",
        "stat_def_iv",
        "stat_spa_iv",
        "stat_spd_iv",
        "stat_spe_iv",
        "stat_vis_iv",
    ):
        validated[key] = random.randint(0, 31)
    for key in (
        "stat_hp_ev",
        "stat_atk_ev",
        "stat_def_ev",
        "stat_spa_ev",
        "stat_spd_ev",
        "stat_spe_ev",
        "stat_vis_ev",
    ):
        validated[key] = 0
    entry = {
        "soul_id": secrets.token_hex(16),
        "validated": validated,
        "fields": {
            "name": FREE_SOUL_NAME,
            "first_name": "Soul",
            "family_name": "1",
            "species": FREE_SOUL_SPECIES,
            "gender": random.choice(["Male", "Female"]),
            "mother_id": None,
            "father_id": None,
            "hometown": [float(cx), float(cy)],
            "birth_date": datetime.now(timezone.utc).isoformat(),
            "activity": "resting",
            "orb_color": _random_color(),
            "aura_color": _random_color(),
            "aura_visible": True,
            "nature": FREE_SOUL_NATURE,
        },
        "secret": {
            "hash": secret_hash,
            "prefix": secret_prefix,
            "token_expiry": generate_token_expiry(),
        },
        "secret_rotated": False,
        "essence": None,
        "inventory": [],
        "now": now,
    }
    return entry, secret
