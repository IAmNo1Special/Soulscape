"""Wallet-derived dormancy: the freeze state (issue #22).

A soul with no funding freezes: no cognition, no movement, slowed
biology, HP floored at 1, immune to theft and collapse, rendered as a
statue. Any funding refresh (ledger credit taking the cached balance
above zero) wakes it with a fresh think schedule.

Design decisions (issue #22 close comment carries the same list):
- Derived, not stored: is_dormant = (cached essence <= DORMANCY_THRESHOLD).
  No flag to go stale; survives crashes trivially. There is no
  dormant_until column -- the arch's "orthogonal wallet-derived
  dormant_until" is satisfied by derivation: `state` (normal|traveling|
  collapsed) and dormancy are orthogonal. A collapsed soul that drains
  to 0 is dormant too; a dormant soul can never collapse (HP floored
  at 1, collapse transition skipped).
- DORMANCY_THRESHOLD = 0.0: unfunded means a zero balance. Tunable.
- Newborn problem: souls are born with essence 0.0 in the ledger's
  eyes, which would mean born dormant. Every newborn gets a starter
  balance of STARTER_GRANT essence as a `mint` ledger entry (new entry
  type; conservation accounting in market.verify_balances treats mints
  explicitly). The cached souls.essence default (STARTING_ESSENCE in
  routers/souls.py) is the same amount by construction.
  *** TUNABLE: Malcom, retune STARTER_GRANT here if 100 is wrong. ***
- Wake: any ledger credit taking essence above 0 wakes automatically
  (derived). The transition is journaled as `soul_woke` so it is
  observable and replayable, and the #24 think-schedule hook is reset.
- Dormant souls cannot author intents (movement, social, market
  list/cancel/buy, plot claims, feed_soul as feeder): statues don't
  act. Rejection reason is `soul_dormant` (WS code SOUL_DORMANT),
  raised at enqueue (commit-before-ack ingress) and re-checked at
  adjudication where the intent kind flows through the tick pump.
- A dormant soul's marketplace listings REMAIN BUYABLE: the buyer acts,
  not the soul. Sale proceeds are a funding refresh, so a buy wakes a
  dormant seller (soul_woke journaled at adjudication). Documented
  here because it is the one deliberate exception to "dormant souls
  don't participate in the economy".
- Theft immunity: no theft/pilfer flow exists server-side in v1, so
  the immunity is a guard for future code. can_be_stolen_from() is the
  hook -- future pilfer/theft adjudication MUST call it and refuse
  when it returns False. It is enforced today in the one place theft
  could already bite: none exists, so this is a documented contract,
  tested directly.
- #24 (agent pool) owns cognition. The integration points are
  is_dormant()/soul_is_dormant() (skip cognition for dormant souls)
  and reset_think_schedule() (fresh think schedule on wake; no-op
  stub until #24 lands).
"""

from __future__ import annotations

import logging
import sqlite3
import time

from . import persistence

logger = logging.getLogger("soulscape_hub")

#: Cached essence at or below this means dormant (unfunded). Tunable.
DORMANCY_THRESHOLD = 0.0

#: Biology rate multiplier while dormant (slowed biology). Tunable.
DORMANCY_BIOLOGY_MULT = 0.25

#: Starter balance granted to every newborn soul as a `mint` ledger
#: entry. *** TUNABLE -- Malcom: retune here if 100 is wrong. ***
#: Kept equal to STARTING_ESSENCE in routers/souls.py by construction
#: (the router imports this constant as its single source of truth).
STARTER_GRANT = 100.0

#: New ledger entry type for the newborn starter grant. Conservation
#: accounting (market.verify_balances) counts mints explicitly.
LEDGER_MINT = "mint"

#: Journal event types for dormancy transitions.
EVENT_SOUL_DORMANT = persistence.EVENT_SOUL_DORMANT
EVENT_SOUL_WOKE = persistence.EVENT_SOUL_WOKE

#: Rejection reason used by every intent kind for dormant authors.
REASON_SOUL_DORMANT = "soul_dormant"


def is_dormant(essence: float | None) -> bool:
    """Pure dormancy predicate over the cached balance.

    essence <= DORMANCY_THRESHOLD. NULL (legacy rows) reads as 0.0,
    i.e. dormant -- an unfunded soul has no wallet to speak of.
    """
    return (essence if essence is not None else 0.0) <= DORMANCY_THRESHOLD


def cached_essence(conn: sqlite3.Connection, soul_id: str) -> float | None:
    """Cached wallet balance for a soul; None when the soul is missing."""
    row = conn.execute(
        "SELECT COALESCE(essence, 0.0) AS essence FROM souls WHERE soul_id = ?",
        (soul_id,),
    ).fetchone()
    return float(row["essence"]) if row is not None else None


def soul_is_dormant(conn: sqlite3.Connection, soul_id: str) -> bool:
    """DB-backed dormancy check for ingress/adjudication guards.

    A missing soul is NOT dormant -- callers check existence separately
    and must not conflate "unknown" with "unfunded".
    """
    essence = cached_essence(conn, soul_id)
    return False if essence is None else is_dormant(essence)


def can_be_stolen_from(conn: sqlite3.Connection, soul_id: str) -> bool:
    """Theft-immunity guard (THEFT_HOOK).

    Returns False for dormant souls: statues cannot be robbed. Also
    False for unknown soul ids (fail closed: nothing to steal from).
    No theft/pilfer flow exists server-side in v1, so this is a
    contract for future code -- any pilfer/theft adjudication MUST
    call this and refuse when it returns False. (A dormant soul's
    marketplace listings staying buyable is NOT theft: the buyer acts,
    the soul doesn't, and the proceeds wake the soul.)
    """
    essence = cached_essence(conn, soul_id)
    if essence is None:
        return False
    return not is_dormant(essence)


def reset_think_schedule(soul_id: str) -> None:
    """Fresh think schedule on wake (#24 integration point).

    No-op stub until the agent pool (#24) lands: it will own cognition
    scheduling and must (a) skip dormant souls via soul_is_dormant()
    and (b) reset a woken soul's think timer here so the soul thinks
    promptly after waking instead of on a stale cadence.
    """
    logger.debug("dormancy: think-schedule reset hook for %s (no-op until #24)", soul_id)


def _current_tick_id(conn: sqlite3.Connection) -> int:
    """Best-effort tick id for journal rows written outside the tick pump
    (e.g. enqueue-time escrow holds). Falls back to 0."""
    try:
        return persistence.last_tick_meta(conn)[1]
    except Exception:
        return 0


def note_essence_change(
    conn: sqlite3.Connection,
    soul_id: str,
    essence_before: float,
    tick_id: int | None = None,
) -> tuple[bool, bool]:
    """Journal dormancy transitions after a cached-essence write.

    Call in the SAME transaction, immediately after any UPDATE of
    souls.essence. Compares the derived dormancy before/after: on a
    freeze flip journals `soul_dormant`; on a wake flip journals
    `soul_woke` and resets the #24 think-schedule hook. No flip, no
    journal. Returns (dormant_now, flipped).
    """
    if tick_id is None:
        tick_id = _current_tick_id(conn)
    essence_after = cached_essence(conn, soul_id)
    if essence_after is None:
        return False, False
    was_dormant = is_dormant(essence_before)
    now_dormant = is_dormant(essence_after)
    if was_dormant == now_dormant:
        return now_dormant, False
    now = time.time()
    if now_dormant:
        persistence.append_event(
            conn,
            tick_id,
            EVENT_SOUL_DORMANT,
            {
                "soul_id": soul_id,
                "essence_before": essence_before,
                "essence_after": essence_after,
                "at": now,
            },
        )
        logger.info(
            "dormancy: soul %s froze (essence %.2f -> %.2f)",
            soul_id,
            essence_before,
            essence_after,
        )
    else:
        persistence.append_event(
            conn,
            tick_id,
            EVENT_SOUL_WOKE,
            {
                "soul_id": soul_id,
                "essence_before": essence_before,
                "essence_after": essence_after,
                "at": now,
            },
        )
        reset_think_schedule(soul_id)
        logger.info(
            "dormancy: soul %s woke (essence %.2f -> %.2f)",
            soul_id,
            essence_before,
            essence_after,
        )
    return now_dormant, True


def mint_starter_grant(
    conn: sqlite3.Connection, tick_id: int, soul_id: str
) -> float:
    """Write the newborn `mint` ledger entry for a soul's starter grant.

    Called at birth (POST /souls) for truly-new souls only, in the same
    transaction as the soul INSERT. The cached souls.essence is set to
    STARTER_GRANT by the INSERT; this row is the ledger truth behind
    it, so conservation accounting stays exact.
    """
    now = time.time()
    conn.execute(
        "INSERT INTO ledger "
        "(tick_id, intent_id, entry_type, soul_id, amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tick_id, f"birth:{soul_id}", LEDGER_MINT, soul_id, STARTER_GRANT, now),
    )
    return STARTER_GRANT
