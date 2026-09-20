"""Personalized quip budget + flash-tier generation (issue #30).

A quip is a tamer-requested one-liner from one of their souls. Two
budgets guard it:

- count: at most QUIPS_PER_SOUL_PER_DAY personalized quips per soul per
  UTC day (arch hard budget), tracked in the ``quip_budgets`` table.
- wallet: a fixed QUIP_PRICE_ESSENCE debit, charged immediately through
  the ledger (``charge_soul`` + a ``quip_debit`` ledger row) before the
  generation runs.

Generation rides the #25 flash tier: the tamer's provider chain
(preferred provider first, then the rest with active vault keys) is
walked with a small quip prompt; with no keys the deterministic
template fallback answers and ``fallback_used`` is set. The call is
also written to the ``llm_usage`` table so #27's metering still sees
the LLM cost -- the fixed price is the service charge on top, settled
immediately rather than batched like deliberation usage.
"""

from __future__ import annotations

import math
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from . import database
from .agents import deliberation
from .agents.deliberation import MODEL_COSTS, PROVIDER_ORDER, TIER_MODELS
from . import key_vault

QUIP_PRICE_ESSENCE = 2.0
QUIPS_PER_SOUL_PER_DAY = 3
QUIP_TIER = "flash"
QUIP_TIMEOUT_S = 30.0
QUIP_MAX_CHARS = 280

_FALLBACK_QUIPS = (
    "{name} stretches a sunbeam into a grin.",
    "{name} hums a tune only dust motes can hear.",
    "{name} does a tiny victory lap around nothing in particular.",
    "{name} winks at the nearest shadow.",
    "{name} practices looking mysterious. Nailed it.",
)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def quip_day() -> str:
    return _today_utc()


def get_quip_count(cursor: Any, soul_id: str, day: str | None = None) -> int:
    row = cursor.execute(
        "SELECT count FROM quip_budgets WHERE soul_id = ? AND day = ?",
        (soul_id, day or quip_day()),
    ).fetchone()
    return int(row["count"]) if row else 0


def quips_remaining(cursor: Any, soul_id: str, day: str | None = None) -> int:
    return max(0, QUIPS_PER_SOUL_PER_DAY - get_quip_count(cursor, soul_id, day))


def release_quip(conn: Any, soul_id: str, day: str) -> int:
    """Release one reserved budget slot (e.g. generation or charging
    failed after the reservation). Deletes the row when the count
    reaches zero. Returns the new count."""
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT count FROM quip_budgets WHERE soul_id = ? AND day = ?",
        (soul_id, day),
    ).fetchone()
    if row is None:
        return 0
    new_count = max(0, int(row[0]) - 1)
    if new_count == 0:
        cursor.execute(
            "DELETE FROM quip_budgets WHERE soul_id = ? AND day = ?",
            (soul_id, day),
        )
    else:
        cursor.execute(
            "UPDATE quip_budgets SET count = ? WHERE soul_id = ? AND day = ?",
            (new_count, soul_id, day),
        )
    return new_count


def record_quip(cursor: Any, soul_id: str, day: str | None = None) -> int:
    day = day or quip_day()
    cursor.execute(
        "INSERT INTO quip_budgets (soul_id, day, count) VALUES (?, ?, 1) "
        "ON CONFLICT(soul_id, day) DO UPDATE SET count = count + 1",
        (soul_id, day),
    )
    return get_quip_count(cursor, soul_id, day)


class QuipRefusal(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def provider_chain(tamer_id: str) -> list[str]:
    try:
        metas = key_vault.list_key_metadata(tamer_id)
    except key_vault.KeyVaultError:
        return []
    have = {
        m["provider"]
        for m in metas
        if m.get("revoked_at") is None and m["provider"] in PROVIDER_ORDER
    }
    return [p for p in PROVIDER_ORDER if p in have]


def _estimate_cost_usd(model: str, prompt_tokens: int, comp_tokens: int) -> float:
    rates = MODEL_COSTS.get(model)
    if not rates:
        return 0.0
    return prompt_tokens / 1000.0 * rates[0] + comp_tokens / 1000.0 * rates[1]


def _quip_prompt(name: str, species: str | None, user_prompt: str | None) -> str:
    brief = f'You are {name}, a soul orb drifting over a desktop.'
    if species:
        brief += f" Species: {species}."
    ask = user_prompt.strip() if user_prompt else "Say something playful about yourself."
    return (
        f"{brief}\nTamer's request: {ask}\n"
        "Reply with ONE short playful line, in character, no quotes, "
        f"under {QUIP_MAX_CHARS} characters."
    )


def generate_quip(
    soul_row: dict,
    user_prompt: str | None = None,
    provider_call: Callable[..., str] | None = None,
    timeout_s: float = QUIP_TIMEOUT_S,
    rng: random.Random | None = None,
) -> dict:
    name = soul_row.get("name") or soul_row.get("soul_id", "soul")
    prompt = _quip_prompt(name, soul_row.get("species"), user_prompt)
    call = provider_call or deliberation.default_provider_call
    tamer_id = (
        soul_row.get("custodian_id") or soul_row.get("owner_id") or ""
    )
    last_error = "no_provider_keys"
    for provider in provider_chain(tamer_id):
        model = TIER_MODELS[provider][QUIP_TIER]
        try:
            with key_vault.use_key(tamer_id, provider) as key:
                start = time.monotonic()
                raw = call(provider, model, prompt, timeout_s, key)
                latency_ms = (time.monotonic() - start) * 1000.0
        except key_vault.KeyNotFound:
            continue
        except Exception as exc:
            last_error = type(exc).__name__
            continue
        text = raw.strip().strip('"').strip("'")[:QUIP_MAX_CHARS]
        prompt_tokens = math.ceil(len(prompt) / 4)
        comp_tokens = math.ceil(len(text) / 4)
        return {
            "text": text,
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": comp_tokens,
            "cost_usd": _estimate_cost_usd(model, prompt_tokens, comp_tokens),
            "latency_ms": latency_ms,
            "fallback_used": False,
        }
    picked = (rng or random).choice(_FALLBACK_QUIPS).format(name=name)
    return {
        "text": picked,
        "provider": "template",
        "model": "template",
        "prompt_tokens": 0,
        "completion_tokens": math.ceil(len(picked) / 4),
        "cost_usd": 0.0,
        "latency_ms": 0.0,
        "fallback_used": True,
        "fallback_reason": last_error,
    }


def write_usage_row(conn: Any, soul_id: str, gen: dict) -> int:
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO llm_usage (soul_id, tier, provider, model, prompt_tokens, "
        "completion_tokens, estimated_cost_usd, latency_ms, fallback_used, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            soul_id,
            QUIP_TIER,
            gen["provider"],
            gen["model"],
            int(gen["prompt_tokens"]),
            int(gen["completion_tokens"]),
            float(gen["cost_usd"]),
            float(gen["latency_ms"]),
            1 if gen["fallback_used"] else 0,
            time.time(),
        ),
    )
    return int(cursor.lastrowid)


def charge_for_quip(conn: Any, soul_id: str) -> float:
    """Debit the fixed quip price; returns the remaining essence."""
    cursor = conn.cursor()
    database.charge_soul(cursor, soul_id, QUIP_PRICE_ESSENCE, "quip")
    cursor.execute(
        "INSERT INTO ledger (tick_id, intent_id, entry_type, soul_id, amount, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (0, f"quip:{uuid.uuid4().hex}", "quip_debit", soul_id,
         -QUIP_PRICE_ESSENCE, time.time()),
    )
    row = cursor.execute(
        "SELECT COALESCE(essence, 0.0) AS essence FROM souls WHERE soul_id = ?",
        (soul_id,),
    ).fetchone()
    return float(row["essence"])
