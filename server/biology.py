"""Server-side biology: needs decay, collapse state machine, feed_soul (issue #21).

Rates (code is the source of truth; arch doc is reference):
- Satiety 100 -> 0 in 36 h resting; 2.0x faster when active (~18 h active).
  NOTE: the arch line says "(~18 h active x1.5)" which is self-contradictory
  (36/1.5 = 24, not 18). The issue's verification numbers (36 h / 18 h) win,
  so the active multiplier is 2.0.
- Hydration 100 -> 0 in 24 h (activity-independent).
- Hungry (satiety <= 50): movement speed x0.75.
- Starving (satiety < 20): HP chip -1/h. (The arch's "activity success -50%"
  has no server-side activity roll in v1; documented, not implemented.)
- HP = 0 -> state='collapsed'. Starvation is the ONLY route in v1: no combat
  damage exists, and this module owns the only UPDATE that writes 'collapsed'.
- Recovery: state back to 'normal' when fed_flag=1 AND continuous rest >= 6 h.
- No permadeath. Dormancy (#22, NOT built): dormancy floors HP at 1 and makes
  souls immune to collapse -- when #22 lands, _decay_soul must skip the HP
  chip and the collapse transition for dormant souls (search DORMANCY_HOOK).

"Active" (decay x2.0) is defined precisely as: nonzero velocity OR a pending
move_target. Collapsed souls are always resting (their velocity is zeroed at
collapse). The client-reported `activity` column is NOT used for the decay
multiplier -- it is client-set and stale-prone.

"Resting" (rest clock) is: state == 'collapsed' OR activity in {"rest","idle"}.
rest_started_at marks when the current continuous rest began; any non-resting
observation clears it. A collapsed soul is definitionally resting, so the 6 h
gate runs from the collapse moment (or earlier if it was already resting).

feed_soul: any soul may feed a collapsed stranger. No custody requirement on
the recipient; the feeder is custody-checked as usual. The gift costs a real
10 essence held in escrow at enqueue (commit-before-ack, same pattern as
#17 market_buy) and transferred feeder -> recipient at adjudication. The
fed_flag + ledger rows + journal + intent status all commit atomically.

Decay runs two ways, with identical rates by construction:
- live: apply_biology_tick() every 50th world tick (0.1 Hz at 5 Hz);
- catch-up: apply_biology_decay() closed-form over the downtime gap
  (called from persistence.recover_world, capped at 24 h).
Both funnel into _decay_soul(), a pure closed-form advance over a field
dict, so simulated-day rates match exactly between live and catch-up.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time

from . import database
from . import determinism
from . import dormancy
from . import persistence
from . import viewport
from .intents import _row_to_dict as _intent_row_to_dict

logger = logging.getLogger("soulscape_hub")

# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------

SATIETY_REST_PER_H = 100.0 / 36.0
SATIETY_ACTIVE_MULT = 2.0  # 36 h resting -> ~18 h active (see module docstring)
HYDRATION_PER_H = 100.0 / 24.0
STARVE_CHIP_PER_H = 1.0
HUNGRY_SATIETY = 50.0
STARVING_SATIETY = 20.0
HUNGRY_SPEED_MULT = 0.75
RECOVERY_REST_SECONDS = 6.0 * 3600.0

BIOLOGY_EVERY_TICKS = 50  # 0.1 Hz at the 5 Hz world tick
CATCHUP_MAX_SECONDS = 24.0 * 3600.0  # downtime decay cap, mirrors persistence

REST_ACTIVITIES = frozenset({"rest", "idle"})

STATE_NORMAL = "normal"
STATE_TRAVELING = "traveling"  # reserved for v1.5 fast-travel; unused in v1
STATE_COLLAPSED = "collapsed"

NEED_BAND_LOW = 35.0
NEED_BAND_MID = 70.0


def need_band(value: float | None) -> str:
    v = float(value or 0.0)
    if v < NEED_BAND_LOW:
        return "low"
    if v < NEED_BAND_MID:
        return "mid"
    return "high"


VALID_STATES = (STATE_NORMAL, STATE_TRAVELING, STATE_COLLAPSED)

# ---------------------------------------------------------------------------
# feed_soul
# ---------------------------------------------------------------------------

KIND_FEED_SOUL = "feed_soul"
BIOLOGY_KINDS = frozenset({KIND_FEED_SOUL})
FEED_SOUL_COST = 10.0

_LEDGER_FEED_DEBIT = "feed_debit"
_LEDGER_FEED_CREDIT = "feed_credit"


class BiologyRefusal(Exception):
    """Business-logic refusal to enqueue or adjudicate a feed_soul intent."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason

    @property
    def ws_code(self) -> str:
        return _WS_ERROR_CODES.get(self.reason, "REJECTED")


_WS_ERROR_CODES = {
    "feeder_not_found": "SOUL_NOT_FOUND",
    "recipient_not_found": "SOUL_NOT_FOUND",
    "insufficient_funds": "INSUFFICIENT_FUNDS",
    "custody": "CUSTODY_DENIED",
    "feeder_collapsed": "FEEDER_COLLAPSED",
    "soul_dormant": "SOUL_DORMANT",
    "not_collapsed": "NOT_COLLAPSED",
    "already_fed": "ALREADY_FED",
    "escrow_short": "ESCROW_SHORT",
}


# ---------------------------------------------------------------------------
# Pure decay core (shared by the live tick and the analytic catch-up path)
# ---------------------------------------------------------------------------


def is_active(velocity: tuple[float, float], move_target, activity) -> bool:
    """Precise "active" definition for the x2.0 satiety drain.

    A soul is active when it has nonzero velocity or a pending move_target.
    Collapsed souls are never active (velocity is zeroed at collapse).
    The client-reported `activity` string is ignored here -- only the rest
    clock below consults it.
    """
    vx, vy = velocity
    if vx != 0.0 or vy != 0.0:
        return True
    return move_target is not None


def is_resting(state: str, activity) -> bool:
    """Precise "resting" definition for the rest clock.

    A collapsed soul is definitionally resting; otherwise the soul rests
    when its client-reported activity is rest/idle.
    """
    if state == STATE_COLLAPSED:
        return True
    return (activity or "") in REST_ACTIVITIES


def speed_multiplier(satiety: float) -> float:
    """Movement speed multiplier: x0.75 while hungry (satiety <= 50)."""
    return HUNGRY_SPEED_MULT if satiety <= HUNGRY_SATIETY else 1.0


def full_or_100(value) -> float:
    """Legacy/direct-inserted rows may carry NULL biology fields; the server
    default for a new soul is full (100), matching the REST layer."""
    return float(value) if value is not None else 100.0


def _decay_soul(fields: dict, seconds: float, now: float) -> dict:
    """Closed-form biology advance over `seconds` (mutates nothing).

    `fields` keys: satiety, hydration, hp, max_hp, state, fed_flag,
    rest_started_at, activity, velocity (tuple), move_target (tuple|None),
    dormant (bool).
    Returns a dict of changed columns -> new values (JSON strings for
    velocity/move_target where applicable), plus optional keys:
      "_collapsed": True when this span collapsed the soul,
      "_recovered": True when this span recovered the soul,
      "_journal": list of (event_type, payload) to journal.

    Dormancy (issue #22): a dormant soul's biology runs at
    DORMANCY_BIOLOGY_MULT, the starvation HP chip is skipped (HP is
    floored at 1 -- the chip is the only thing that lowers HP in v1,
    so skipping it is the floor), and the collapse transition is
    skipped (collapse immunity). A dormant soul can never collapse,
    even starving at HP 1.
    """
    out: dict = {}
    journal: list[tuple[str, dict]] = []
    soul_id = fields["soul_id"]

    if seconds <= 0:
        return out

    state = fields.get("state") or STATE_NORMAL
    dormant = bool(fields.get("dormant"))
    sat = float(fields.get("satiety") or 0.0)
    hyd = float(fields.get("hydration") or 0.0)
    hp = float(fields.get("hp") or 0.0)
    fed_flag = int(fields.get("fed_flag") or 0)
    rest_started_at = fields.get("rest_started_at")
    activity = fields.get("activity")
    velocity = fields.get("velocity") or (0.0, 0.0)
    move_target = fields.get("move_target")

    hours = seconds / 3600.0
    active = is_active(velocity, move_target, activity) and state != STATE_COLLAPSED

    # --- needs decay (linear in time: per-tick sum == closed form) ---
    # Dormant souls decay at x0.25 (issue #22).
    dorm_mult = dormancy.DORMANCY_BIOLOGY_MULT if dormant else 1.0
    sat_rate = SATIETY_REST_PER_H * (SATIETY_ACTIVE_MULT if active else 1.0)
    sat_rate *= dorm_mult
    hyd_rate = HYDRATION_PER_H * dorm_mult
    sat_after = max(0.0, sat - sat_rate * hours)
    hyd_after = max(0.0, hyd - hyd_rate * hours)
    if sat_after != sat:
        out["satiety"] = sat_after
    if hyd_after != hyd:
        out["hydration"] = hyd_after

    # --- starvation HP chip: -1/h for the portion of the span with sat < 20 ---
    # DORMANCY (issue #22): dormant souls skip the chip entirely -- HP
    # is floored at 1 (the chip is the only thing that lowers HP in v1)
    # and the collapse transition below is skipped (immunity).
    if dormant:
        chip = 0.0
    elif sat < STARVING_SATIETY:
        starving_h = hours
        chip = STARVE_CHIP_PER_H * starving_h
    else:
        t_to_starve = (sat - STARVING_SATIETY) / sat_rate if sat_rate > 0 else hours
        starving_h = max(0.0, hours - t_to_starve)
        chip = STARVE_CHIP_PER_H * starving_h

    collapsed = False
    if state == STATE_COLLAPSED:
        # Already collapsed: HP stays 0, rest accrues below. No chip.
        pass
    elif dormant:
        # DORMANCY (issue #22): collapse immunity. A dormant soul never
        # takes the collapse transition, whatever its HP or satiety.
        # (A collapsed soul that drains to 0 is dormant too -- orthogonal
        # states -- but it stays collapsed via the branch above.)
        pass
    elif hp - chip <= 0.0:
        # HP hits 0 inside this span (or was already 0): starvation
        # collapses the soul. This is the ONLY collapse route in v1.
        if chip > 0.0 and hp > 0.0:
            # HP depletes `hp / chip_rate` hours into the starving portion,
            # which is the tail of the span.
            collapse_offset_h = hours - starving_h + hp / STARVE_CHIP_PER_H
        else:
            collapse_offset_h = 0.0
        collapse_at = now - (hours - collapse_offset_h) * 3600.0
        state = STATE_COLLAPSED
        out["state"] = state
        out["hp"] = 0.0
        out["velocity"] = json.dumps([0.0, 0.0])
        out["move_target"] = None
        # Collapse starts the rest clock (a collapsed soul is resting).
        rest_started_at = collapse_at
        out["rest_started_at"] = collapse_at
        collapsed = True
        journal.append(
            (
                persistence.EVENT_SOUL_COLLAPSED,
                {"soul_id": soul_id, "collapsed_at": collapse_at},
            )
        )
    elif chip > 0.0:
        hp_after = max(0.0, hp - chip)
        if hp_after != hp:
            out["hp"] = hp_after

    # --- dormancy HP floor (issue #22): a frozen soul's HP never sits
    # below 1, whatever drained it. Orthogonal to the lifecycle state: a
    # collapsed soul that also drains stays collapsed, HP floored at 1
    # (recovery still needs feeding + rest).
    if dormant and hp < 1.0:
        out["hp"] = 1.0

    # --- rest clock ---
    if state == STATE_COLLAPSED and collapsed:
        pass  # rest_started_at already set to the collapse moment above
    elif is_resting(state, activity):
        if rest_started_at is None:
            # Unknown rest start (e.g. long downtime): be conservative and
            # credit rest only from the start of this span.
            rest_started_at = now - seconds
            out["rest_started_at"] = rest_started_at
    elif rest_started_at is not None:
        rest_started_at = None
        out["rest_started_at"] = None

    # --- recovery: fed AND continuously rested >= 6 h ---
    if state == STATE_COLLAPSED and fed_flag == 1 and rest_started_at is not None:
        rested_s = now - float(rest_started_at)
        if rested_s >= RECOVERY_REST_SECONDS:
            state = STATE_NORMAL
            out["state"] = state
            out["fed_flag"] = 0
            out["rest_started_at"] = None
            journal.append(
                (
                    persistence.EVENT_SOUL_RECOVERED,
                    {
                        "soul_id": soul_id,
                        "rested_seconds": rested_s,
                    },
                )
            )

    if journal:
        out["_journal"] = journal
    if collapsed:
        out["_collapsed"] = True
    return out


def _parse_velocity(raw) -> tuple[float, float]:
    if raw is None:
        return (0.0, 0.0)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return (0.0, 0.0)
    try:
        return (float(raw[0]), float(raw[1]))
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0)


def _parse_target(raw):
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    try:
        return (float(raw[0]), float(raw[1]))
    except (TypeError, ValueError, IndexError):
        return None


_BIOLOGY_COLUMNS = (
    "soul_id, satiety, hydration, hp, max_hp, state, fed_flag,"
    " rest_started_at, activity, velocity, move_target,"
    " COALESCE(essence, 0.0) AS essence"
)


def _advance_rows(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    seconds: float,
    now: float,
    tick_id: int,
) -> dict[str, int]:
    """Apply _decay_soul to DB rows; journal transitions; hungry-rescale.

    Returns a small report dict. Commits nothing -- the caller owns the
    transaction (live tick uses autocommit-per-call below; catch-up folds
    into the recovery transaction).
    """
    report = {"decayed": 0, "collapsed": 0, "recovered": 0, "rescaled": 0}
    for row in rows:
        soul_id = row["soul_id"]
        unflushed = persistence.dirty_get(soul_id)
        velocity = _parse_velocity(row["velocity"])
        move_target = _parse_target(row["move_target"])
        if unflushed is not None:
            if unflushed.get("velocity") is not None:
                v = unflushed["velocity"]
                velocity = (float(v[0]), float(v[1]))
            if "move_target" in unflushed:
                mt = unflushed["move_target"]
                move_target = None if mt is None else (float(mt[0]), float(mt[1]))
        fields = {
            "soul_id": soul_id,
            "satiety": full_or_100(row["satiety"]),
            "hydration": full_or_100(row["hydration"]),
            "hp": full_or_100(row["hp"]),
            "max_hp": row["max_hp"],
            "state": row["state"],
            "fed_flag": row["fed_flag"],
            "rest_started_at": row["rest_started_at"],
            "activity": row["activity"],
            "velocity": velocity,
            "move_target": move_target,
            # Dormancy (issue #22): derived from the cached balance; the
            # decay core slows rates x0.25, floors HP at 1 (no chip), and
            # grants collapse immunity. Same core serves the live tick
            # and the analytic catch-up path, so downtime decay matches.
            "dormant": dormancy.is_dormant(row["essence"]),
        }
        mutations = _decay_soul(fields, seconds, now)
        journal = mutations.pop("_journal", [])
        collapsed_now = mutations.pop("_collapsed", False)
        sat_after = mutations.get("satiety", fields["satiety"])
        if not collapsed_now and sat_after <= HUNGRY_SATIETY:
            # Hungry mid-journey: clamp in-flight speed to x0.75, preserving
            # direction. Adjudication applies the same factor to new moves.
            from .world_tick import INTENT_MOVE_SPEED

            hungry_speed = INTENT_MOVE_SPEED * HUNGRY_SPEED_MULT
            speed = (velocity[0] ** 2 + velocity[1] ** 2) ** 0.5
            if speed > hungry_speed + 1e-9:
                ratio = hungry_speed / speed
                nv = [velocity[0] * ratio, velocity[1] * ratio]
                mutations["velocity"] = json.dumps(nv)
                persistence.dirty.mark(soul_id, velocity=nv)
                report["rescaled"] += 1
        if collapsed_now:
            # Keep the read-through view consistent: the collapsed soul stops.
            persistence.dirty.mark(soul_id, velocity=[0.0, 0.0], move_target=None)
            report["collapsed"] += 1
        updates = {k: v for k, v in mutations.items() if not k.startswith("_")}
        if updates:
            sets = ", ".join(f"{col} = ?" for col in updates)
            conn.execute(
                f"UPDATE souls SET {sets} WHERE soul_id = ?",
                (*updates.values(), soul_id),
            )
            report["decayed"] += 1
        for event_type, payload in journal:
            persistence.append_event(conn, tick_id, event_type, payload)
            if event_type == persistence.EVENT_SOUL_RECOVERED:
                report["recovered"] += 1
        if collapsed_now or any(
            e == persistence.EVENT_SOUL_RECOVERED for e, _ in journal
        ):
            logger.info(
                "biology: soul %s %s (tick %d)",
                soul_id,
                "collapsed" if collapsed_now else "recovered",
                tick_id,
            )
    return report


def apply_biology_tick(
    conn: sqlite3.Connection, tick_id: int, now: float, tick_dt: float
) -> dict[str, int]:
    """Live path: decay every 50th tick (0.1 Hz). Commits its own transaction."""
    seconds = BIOLOGY_EVERY_TICKS * tick_dt
    rows = conn.execute(f"SELECT {_BIOLOGY_COLUMNS} FROM souls").fetchall()
    conn.execute("BEGIN IMMEDIATE")
    try:
        report = _advance_rows(conn, rows, seconds, now, tick_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report


def apply_biology_decay(
    conn: sqlite3.Connection, seconds: float, now: float, tick_id: int
) -> dict[str, int]:
    """Catch-up path: closed-form decay over a downtime gap (capped at 24 h).

    Same _decay_soul core as the live tick, so downtime decay matches live
    decay rates exactly. Does NOT commit -- the caller (recover_world) folds
    this into the recovery transaction. Souls that collapse mid-gap have
    their velocity zeroed; their replayed position is a documented
    approximation (analytic motion ran the full gap before biology applied).
    """
    seconds = min(max(seconds, 0.0), CATCHUP_MAX_SECONDS)
    if seconds <= 0:
        return {"decayed": 0, "collapsed": 0, "recovered": 0, "rescaled": 0}
    rows = conn.execute(f"SELECT {_BIOLOGY_COLUMNS} FROM souls").fetchall()
    return _advance_rows(conn, rows, seconds, now, tick_id)


# ---------------------------------------------------------------------------
# feed_soul intent: enqueue (commit-before-ack) + adjudication (atomic)
# ---------------------------------------------------------------------------


def _write_ledger(
    conn: sqlite3.Connection,
    tick_id: int,
    intent_id: str,
    rows: list[tuple[str, str, float]],
    now: float | None = None,
) -> None:
    # Issue #38: ledger created_at rides the adjudication clock so a
    # seeded replay writes identical rows.
    now = time.time() if now is None else now
    conn.executemany(
        "INSERT INTO ledger "
        "(tick_id, intent_id, entry_type, soul_id, amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (tick_id, intent_id, entry_type, soul_id, amount, now)
            for entry_type, soul_id, amount in rows
        ],
    )


def _hold_feed_escrow(
    conn: sqlite3.Connection, intent_id: str, feeder_soul_id: str
) -> float:
    """Hold the 10-essence gift inside the enqueue transaction.

    Raises BiologyRefusal when the feeder is missing or short on funds.
    A hold that drains the feeder to 0 freezes it (dormancy is derived);
    the flip is journaled via note_essence_change.
    """
    essence_before = dormancy.cached_essence(conn, feeder_soul_id)
    cursor = conn.execute(
        "UPDATE souls SET essence = essence - ? WHERE soul_id = ? AND essence >= ?",
        (FEED_SOUL_COST, feeder_soul_id, FEED_SOUL_COST),
    )
    if cursor.rowcount == 0:
        exists = conn.execute(
            "SELECT 1 FROM souls WHERE soul_id = ?", (feeder_soul_id,)
        ).fetchone()
        if exists is None:
            raise BiologyRefusal("feeder_not_found", "Feeder soul not found")
        raise BiologyRefusal(
            "insufficient_funds",
            f"Insufficient essence to hold {FEED_SOUL_COST} for feed_soul",
        )
    dormancy.note_essence_change(conn, feeder_soul_id, essence_before or 0.0)
    conn.execute(
        "INSERT INTO escrows "
        "(escrow_id, intent_id, soul_id, amount, status, created_at) "
        "VALUES (?, ?, ?, ?, 'held', ?)",
        (
            "esc_" + secrets.token_urlsafe(12),
            intent_id,
            feeder_soul_id,
            FEED_SOUL_COST,
            time.time(),
        ),
    )
    return FEED_SOUL_COST


def _release_feed_escrow(conn: sqlite3.Connection, intent_id: str) -> bool:
    """Refund a held feed escrow to the feeder's cached balance.

    A refund can wake a dormant feeder (funding refresh); the flip is
    journaled via note_essence_change.
    """
    row = conn.execute(
        "SELECT soul_id, amount FROM escrows WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    ).fetchone()
    if row is None:
        return False
    essence_before = dormancy.cached_essence(conn, row["soul_id"])
    conn.execute(
        "UPDATE souls SET essence = essence + ? WHERE soul_id = ?",
        (float(row["amount"]), row["soul_id"]),
    )
    dormancy.note_essence_change(conn, row["soul_id"], essence_before or 0.0)
    conn.execute(
        "UPDATE escrows SET status = 'released' "
        "WHERE intent_id = ? AND status = 'held'",
        (intent_id,),
    )
    return True


def enqueue_feed_soul(
    session_id: str,
    nonce: str,
    custodian_id: str | None,
    soul_id: str,
    kind: str,
    payload: dict,
) -> tuple[dict, bool]:
    """Enqueue a feed_soul intent with commit-before-ack.

    The intent insert and the 10-essence escrow hold commit in ONE
    transaction: the ack is only ever sent for durable state. Returns
    (record, created); a duplicate (session_id, nonce) returns the existing
    record with created=False and never double-holds funds. Raises
    BiologyRefusal on preface failures (no intent row is written).
    """
    if kind != KIND_FEED_SOUL:
        raise ValueError(f"not a biology intent kind: {kind}")
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
                        json.dumps(payload),
                        now,
                    ),
                )
                created = True
            except sqlite3.IntegrityError:
                created = False
            if created:
                feeder = conn.execute(
                    "SELECT custodian_id, owner_id, state FROM souls WHERE soul_id = ?",
                    (soul_id,),
                ).fetchone()
                if feeder is None:
                    raise BiologyRefusal("feeder_not_found", "Feeder soul not found")
                if custodian_id is not None:
                    if payload.get("feeder_soul_id") != soul_id:
                        raise BiologyRefusal(
                            "custody", "feeder_soul_id does not match intent soul"
                        )
                    if (feeder["custodian_id"] or feeder["owner_id"]) != custodian_id:
                        raise BiologyRefusal(
                            "custody", "Only the feeder's custodian can feed"
                        )
                if (feeder["state"] or STATE_NORMAL) == STATE_COLLAPSED:
                    raise BiologyRefusal(
                        "feeder_collapsed", "A collapsed soul cannot feed"
                    )
                # Dormancy (issue #22): statues don't act. (A dormant soul
                # is broke anyway, so the escrow hold below would fail --
                # this rejects early with a clear reason.)
                if dormancy.soul_is_dormant(conn, soul_id):
                    raise BiologyRefusal(
                        "soul_dormant",
                        "A dormant (unfunded) soul cannot feed",
                    )
                _hold_feed_escrow(conn, intent_id, soul_id)
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


def adjudicate_feed_soul(tick, intent: dict) -> None:
    """Adjudicate a feed_soul intent atomically.

    One BEGIN IMMEDIATE commit sets the recipient's fed_flag, applies the
    escrowed gift (feeder -10 already held at enqueue, recipient +10 here),
    writes the ledger rows, journals the outcome, and marks the intent
    adjudicated. Rejections release the escrow back to the feeder in the
    same commit.
    """
    from .world_tick import WorldTick  # noqa: F401  (type only)

    intent_id = intent["intent_id"]
    feeder_soul_id = intent["soul_id"]
    payload = intent["payload"] or {}
    recipient_soul_id = payload.get("recipient_soul_id")

    def reject(conn, reason: str, detail: str = "") -> None:
        _release_feed_escrow(conn, intent_id)
        conn.execute(
            "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
            (
                "rejected",
                json.dumps({"reason": reason, "detail": detail or reason}),
                intent_id,
            ),
        )
        persistence.append_event(
            conn,
            tick.tick_id,
            persistence.EVENT_INTENT_REJECTED,
            {
                "intent_id": intent_id,
                "kind": KIND_FEED_SOUL,
                "soul_id": feeder_soul_id,
                "reason": reason,
            },
        )

    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            feeder = conn.execute(
                "SELECT custodian_id, owner_id FROM souls WHERE soul_id = ?",
                (feeder_soul_id,),
            ).fetchone()
            if feeder is None:
                reject(conn, "feeder_not_found")
                conn.commit()
                return
            if (
                intent["custodian_id"] is not None
                and (feeder["custodian_id"] or feeder["owner_id"])
                != intent["custodian_id"]
            ):
                reject(conn, "custody")
                conn.commit()
                return
            recipient = conn.execute(
                "SELECT soul_id, state, fed_flag FROM souls WHERE soul_id = ?",
                (recipient_soul_id,),
            ).fetchone()
            if recipient is None:
                reject(conn, "recipient_not_found")
                conn.commit()
                return
            if (recipient["state"] or STATE_NORMAL) != STATE_COLLAPSED:
                # Feeding a non-collapsed soul is rejected -- and this is
                # also why feed_soul can never collapse anyone.
                reject(conn, "not_collapsed")
                conn.commit()
                return
            if int(recipient["fed_flag"] or 0) == 1:
                reject(conn, "already_fed")
                conn.commit()
                return
            escrow = conn.execute(
                "SELECT amount, status FROM escrows WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if (
                escrow is None
                or escrow["status"] != "held"
                or float(escrow["amount"]) < FEED_SOUL_COST - 1e-9
            ):
                reject(conn, "escrow_short")
                conn.commit()
                return
            conn.execute(
                "UPDATE souls SET fed_flag = 1 WHERE soul_id = ?",
                (recipient_soul_id,),
            )
            # The 10-essence gift is a funding refresh: it wakes a
            # dormant recipient (issue #22 -- soul_woke journaled).
            recipient_before = dormancy.cached_essence(conn, recipient_soul_id)
            conn.execute(
                "UPDATE souls SET essence = essence + ? WHERE soul_id = ?",
                (FEED_SOUL_COST, recipient_soul_id),
            )
            dormancy.note_essence_change(
                conn,
                recipient_soul_id,
                recipient_before or 0.0,
                tick.tick_id,
                # Issue #38: adjudication clock, not wall clock.
                now=determinism.tick_now(tick),
            )
            conn.execute(
                "UPDATE escrows SET status = 'applied' "
                "WHERE intent_id = ? AND status = 'held'",
                (intent_id,),
            )
            _write_ledger(
                conn,
                tick.tick_id,
                intent_id,
                [
                    (_LEDGER_FEED_DEBIT, feeder_soul_id, -FEED_SOUL_COST),
                    (_LEDGER_FEED_CREDIT, recipient_soul_id, FEED_SOUL_COST),
                ],
                # Issue #38: adjudication clock, not wall clock.
                now=determinism.tick_now(tick),
            )
            result = {
                "recipient_soul_id": recipient_soul_id,
                "gift": FEED_SOUL_COST,
                "fed": True,
            }
            conn.execute(
                "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                ("adjudicated", json.dumps(result), intent_id),
            )
            persistence.append_event(
                conn,
                tick.tick_id,
                persistence.EVENT_SOUL_FED,
                {
                    "intent_id": intent_id,
                    "kind": KIND_FEED_SOUL,
                    "soul_id": feeder_soul_id,
                    "recipient_soul_id": recipient_soul_id,
                    "gift": FEED_SOUL_COST,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    viewport.viewport.notify_economy_soul(feeder_soul_id)
    viewport.viewport.notify_economy_soul(recipient_soul_id)
    # #26: feeding is episodic memory for both souls. The recipient
    # was collapsed and got 10e -- high salience, immediately
    # retrievable.
    from .agents import memory as _memory

    _memory.log_episode(
        feeder_soul_id,
        "feed",
        {
            "summary": f"fed {recipient_soul_id} (+{FEED_SOUL_COST:.0f}e)",
            "role": "feeder",
            "other": recipient_soul_id,
            "gift": FEED_SOUL_COST,
        },
        salience=0.6,
    )
    _memory.log_episode(
        recipient_soul_id,
        "feed",
        {
            "summary": f"was fed by {feeder_soul_id} (+{FEED_SOUL_COST:.0f}e)",
            "role": "recipient",
            "other": feeder_soul_id,
            "gift": FEED_SOUL_COST,
        },
        salience=0.7,
    )
