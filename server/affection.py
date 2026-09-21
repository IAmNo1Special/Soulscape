"""Pet interaction grammar (issue #31): chirp / pet / carry.

The tamer->soul grammar is affection and attention ONLY. It has zero
direct-command verbs: no "move", "stay", "fetch", "attack" -- nothing
that steers the soul. Steering stays with the soul's own drives,
reflexes and deliberation (#24/#25). Carry is physical, not a command:
the soul can escape mid-carry, and the escape odds are nature-based.

Kinds (see intents.py for payload validation):
  chirp          Left-click attention. Records a fixed "attention"
                 sensation in the soul's #24 ring (the brain hears it)
                 and pulls the #24 think scheduler forward; fans a "!"
                 solicited bubble to the owner's viewport sessions.
  affection_pet  Press-and-hold / strokes gesture (client-side
                 detector). Server-side: custody check, 5-minute
                 per-(soul, tamer) cooldown, writes a high-salience
                 loyalty-affection episodic memory (#26), nudges the
                 stored souls.loyalty scalar +0.02 toward 1.0, fans a
                 heart bubble. #24's loyalty drive reads the scalar.
  carry_move     Drag phases grab/move/release. Every move update is
                 adjudicated against (a) true-coordinate validation:
                 proposed within CARRY_LEASH_RANGE of the server's
                 read-through position (anti-teleport); (b) plot
                 borders: the proposed position must stay in the plot
                 the grab started in -- same-plot endpoints plus the
                 leash guarantee the true->proposed segment never
                 crosses a plot border (plots are convex); (c) an
                 escape roll at nature-based per-second odds. Escape
                 drops the soul at its true position and starts a
                 30 s re-grab cooldown. While carried, move_to intents
                 for the soul are suspended (see world_tick).

Custody: only the soul's custodian-tamer may pet or carry. Strangers
are rejected with CUSTODY_DENIED at WS ingress; the rejection is the
polite refusal -- no touch happens, no memory is written.
Dormant (#22) and collapsed (#21) souls can be neither petted,
chirped, nor carried: statues don't feel.

Carry sessions live in memory (per-process); a restart simply ends
any in-flight carry -- the soul stays wherever it was. Pet cooldowns
are durable in pet_cooldowns so restarts can't bypass them.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from dataclasses import dataclass

from . import biology
from . import database
from . import determinism
from . import dormancy
from . import persistence
from . import plots
from . import viewport
from .agents import memory as agent_memory
from .agents import scheduler as think_scheduler
from .agents import sensations

logger = logging.getLogger("soulscape_hub")

KIND_CHIRP = "chirp"
KIND_PET = "affection_pet"
KIND_CARRY_MOVE = "carry_move"

#: The complete tamer->soul affection grammar. Anything else submitted
#: as an intent kind is rejected as UNKNOWN_INTENT_KIND (see
#: intents.validate_payload) -- command verbs can never enter through
#: this grammar.
AFFECTION_KINDS = frozenset({KIND_CHIRP, KIND_PET, KIND_CARRY_MOVE})

#: Verbs that must never become part of this grammar. The vocab test
#: asserts every one of these is rejected as an intent kind.
FORBIDDEN_COMMAND_VERBS = frozenset(
    {"move", "stay", "fetch", "attack", "come", "go", "wait_order"}
)

#: Petting cooldown: one petting per soul per tamer per 5 minutes,
#: enforced server-side in the adjudication transaction.
PET_COOLDOWN_S = 300.0

#: Loyalty nudge per accepted petting: flat +0.02 toward 1.0.
PET_LOYALTY_NUDGE = 0.02

#: Carry leash: a proposed carry position must be within 120 world
#: units of the server's true position, else it is anti-teleport
#: rejected. One plot is 120 wu, so the leash plus the same-plot
#: rule together guarantee no plot border is ever crossed.
CARRY_LEASH_RANGE = 120.0

#: Seconds after an escape before the soul can be grabbed again.
ESCAPE_REGRAB_COOLDOWN_S = 30.0

#: Cap on the escape-roll time delta: a stalled client that resumes
#: streaming can't bank an arbitrarily large single-roll probability.
MAX_ESCAPE_DT_S = 5.0

#: Per-second escape probability by nature. The five anchors from the
#: issue (docile 2%, calm 5%, "playful" 12%, timid 25%, wild 40%) map
#: onto the closest natures; every other nature is assigned the tier
#: its temperament matches. Unknown/NULL natures read 10%.
NATURE_ESCAPE_RATE: dict[str, float] = {
    "docile": 0.02,
    "bashful": 0.02,
    "hardy": 0.02,
    "serious": 0.02,
    "quiet": 0.02,
    "calm": 0.05,
    "gentle": 0.05,
    "careful": 0.05,
    "relaxed": 0.05,
    "mild": 0.05,
    "modest": 0.05,
    "jolly": 0.12,
    "hasty": 0.12,
    "naive": 0.12,
    "lax": 0.12,
    "sassy": 0.12,
    "quirky": 0.12,
    "timid": 0.25,
    "lonely": 0.25,
    "bold": 0.25,
    "rash": 0.25,
    "brave": 0.40,
    "adamant": 0.40,
    "naughty": 0.40,
    "impish": 0.40,
}

#: Escape rate for unknown/NULL natures.
DEFAULT_ESCAPE_RATE = 0.10

#: Fixed chirp sensation text (byte-exact; clients/tests match it).
CHIRP_SENSATION = (
    "A bright attention-chirp pulses down from above -- "
    "your tamer is looking at you."
)

CARRY_PHASES = ("grab", "move", "release")

_rng = random.Random()


class AffectionRefusal(Exception):
    """Business-logic refusal of an affection intent."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass
class _CarrySession:
    tamer_id: str | None
    origin_plot: str
    last_x: float
    last_y: float
    last_move_at: float
    grabbed_at: float


_carries: dict[str, _CarrySession] = {}
_escape_cooldowns: dict[str, float] = {}


def reset_carry_state(soul_id: str | None = None) -> None:
    """Drop in-memory carry state (tests)."""
    if soul_id is None:
        _carries.clear()
        _escape_cooldowns.clear()
    else:
        _carries.pop(soul_id, None)
        _escape_cooldowns.pop(soul_id, None)


def carried_souls() -> set[str]:
    """Soul ids currently in an active carry session."""
    return set(_carries)


def escape_rate_for(nature: str | None) -> float:
    """Per-second escape probability for a nature."""
    return NATURE_ESCAPE_RATE.get(
        (nature or "").strip().lower(), DEFAULT_ESCAPE_RATE
    )


def roll_escape(
    nature: str | None, dt_seconds: float, rng: random.Random | None = None
) -> bool:
    """One escape roll for dt seconds of carry.

    p = 1 - (1 - rate)^dt so the per-second table rate holds at any
    adjudication cadence. Pure given the rng (tests seed it).
    """
    rate = escape_rate_for(nature)
    dt = min(max(float(dt_seconds), 0.0), MAX_ESCAPE_DT_S)
    if dt <= 0.0 or rate <= 0.0:
        return False
    p = 1.0 - (1.0 - rate) ** dt
    return (rng or _rng).random() < p


def _true_position(conn, soul_id: str) -> tuple[float, float]:
    """Server's true position: DB overlaid with the tick's unflushed
    dirty set (same read-through view the viewport streams)."""
    row = conn.execute(
        "SELECT position FROM souls WHERE soul_id = ?", (soul_id,)
    ).fetchone()
    if row is None:
        raise AffectionRefusal("soul_not_found", f"Soul {soul_id} not found")
    raw = row["position"]
    try:
        x, y = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError):
        try:
            x, y = json.loads(raw)
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            x, y = 0.0, 0.0
    unflushed = persistence.dirty_get(soul_id)
    if unflushed is not None and unflushed.get("position") is not None:
        x, y = unflushed["position"]
    return float(x), float(y)


def _check_custody(conn, intent: dict) -> dict:
    """Re-validate custody against live state (defense in depth: WS
    ingress checks too, but REST/unknown paths may not)."""
    soul_id = intent["soul_id"]
    row = conn.execute(
        "SELECT custodian_id, owner_id, state, nature, "
        "COALESCE(essence, 0.0) AS essence, COALESCE(loyalty, 0.5) AS loyalty "
        "FROM souls WHERE soul_id = ?",
        (soul_id,),
    ).fetchone()
    if row is None:
        raise AffectionRefusal("soul_not_found", f"Soul {soul_id} not found")
    if intent["custodian_id"] is not None and (
        (row["custodian_id"] or row["owner_id"]) != intent["custodian_id"]
    ):
        # Ownership transferred mid-carry: the old session's tamer is no
        # longer custodian, so the stale session must go -- otherwise the
        # new custodian's grab would hit "already_carried" forever. A
        # stranger poking at someone else's live carry does NOT clear it.
        session = _carries.get(soul_id)
        if session is not None and session.tamer_id != (
            row["custodian_id"] or row["owner_id"]
        ):
            del _carries[soul_id]
        raise AffectionRefusal(
            "custody", "Only the soul's custodian-tamer may touch it"
        )
    if (row["state"] or biology.STATE_NORMAL) == biology.STATE_COLLAPSED:
        if soul_id in _carries:
            del _carries[soul_id]
        raise AffectionRefusal("collapsed", "A collapsed soul cannot be touched")
    if dormancy.is_dormant(row["essence"]):
        if soul_id in _carries:
            del _carries[soul_id]
        raise AffectionRefusal("soul_dormant", "A dormant soul cannot be touched")
    return dict(row)


def _clamp_to_bounds(x: float, y: float) -> tuple[float, float]:
    margin = persistence.POSITION_MARGIN
    w, h = database.SCREEN_BOUNDS
    return (
        min(max(x, margin), w - margin),
        min(max(y, margin), h - margin),
    )


def _write_position(
    conn, soul_id: str, x: float, y: float
) -> None:
    """Authoritative position write for carry: DB + dirty overlay, so
    the tick's read-through view and the viewport stream agree."""
    conn.execute(
        "UPDATE souls SET position = ?, velocity = ?, move_target = NULL "
        "WHERE soul_id = ?",
        (json.dumps([x, y]), json.dumps([0.0, 0.0]), soul_id),
    )
    persistence.dirty.mark(
        soul_id, position=[x, y], velocity=[0.0, 0.0], move_target=None
    )


def _settle(
    conn, tick_id: int, intent: dict, status: str, result: dict
) -> None:
    """Commit one adjudicated/rejected affection intent with its
    journal event. Callers run inside BEGIN IMMEDIATE."""
    event = (
        persistence.EVENT_INTENT_ADJUDICATED
        if status == "adjudicated"
        else persistence.EVENT_INTENT_REJECTED
    )
    conn.execute(
        "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
        (status, json.dumps(result), intent["intent_id"]),
    )
    payload = {
        "intent_id": intent["intent_id"],
        "kind": intent["kind"],
        "soul_id": intent["soul_id"],
    }
    if status == "adjudicated":
        payload["result"] = result
    else:
        payload["reason"] = result["reason"]
    persistence.append_event(conn, tick_id, event, payload)


def _reject(
    tick_id: int,
    intent: dict,
    reason: str,
    detail: str = "",
    log_episode: bool = True,
) -> None:
    """Mark an affection intent rejected (never raises)."""
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM intents WHERE intent_id = ?",
                (intent["intent_id"],),
            ).fetchone()
            if row is None or row["status"] != "pending":
                conn.rollback()
                return
            _settle(
                conn,
                tick_id,
                intent,
                "rejected",
                {"reason": reason, "detail": detail or reason},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if log_episode:
        agent_memory.log_episode(
            intent["soul_id"],
            "intent_outcome",
            {
                "summary": f"intent {intent['kind']} rejected: {reason}",
                "intent_id": intent["intent_id"],
                "kind": intent["kind"],
                "reason": reason,
            },
            salience=0.6,
        )


def _owner_of(conn, soul_id: str) -> str | None:
    """Owner id for bubble fanout (read inside the tx; the bubble is
    emitted after commit so a rollback can't leave a phantom)."""
    row = conn.execute(
        "SELECT owner_id FROM souls WHERE soul_id = ?", (soul_id,)
    ).fetchone()
    if row is None or not row["owner_id"]:
        return None
    return row["owner_id"]


def _emit_bubble(owner_id: str | None, soul_id: str, text: str) -> None:
    if owner_id:
        viewport.viewport.notify_bubble(
            owner_id, soul_id, text, kind="system", solicited=True
        )


# ------------------------------------------------------------------
# chirp


def _adjudicate_chirp(tick, intent: dict) -> None:
    soul_id = intent["soul_id"]
    owner_id: str | None = None
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM intents WHERE intent_id = ?",
                (intent["intent_id"],),
            ).fetchone()
            if row is None or row["status"] != "pending":
                conn.rollback()
                return
            _check_custody(conn, intent)
            sensations.record(
                soul_id,
                CHIRP_SENSATION,
                "chirp",
                tick_id=tick.tick_id,
                journal_conn=conn,
            )
            owner_id = _owner_of(conn, soul_id)
            _settle(conn, tick.tick_id, intent, "adjudicated", {"chirped": True})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    _emit_bubble(owner_id, soul_id, "!")
    # Attention pulls the brain forward: the soul notices the chirp
    # (min-gap honored by the scheduler).
    think_scheduler.default().note_attention(soul_id, time.time())


# ------------------------------------------------------------------
# petting


def _pet_cooldown_remaining(
    conn, soul_id: str, tamer_id: str, now: float
) -> float:
    row = conn.execute(
        "SELECT last_pet_at FROM pet_cooldowns WHERE soul_id = ? AND tamer_id = ?",
        (soul_id, tamer_id),
    ).fetchone()
    if row is None:
        return 0.0
    return max(0.0, PET_COOLDOWN_S - (now - float(row["last_pet_at"])))


def _adjudicate_pet(tick, intent: dict) -> None:
    soul_id = intent["soul_id"]
    tamer_id = intent["custodian_id"] or "operator"
    # Issue #38: pet cooldowns ride the adjudication clock so a seeded
    # replay makes the same allow/deny decision.
    now = determinism.tick_now(tick)
    new_loyalty: float | None = None
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM intents WHERE intent_id = ?",
                (intent["intent_id"],),
            ).fetchone()
            if row is None or row["status"] != "pending":
                conn.rollback()
                return
            soul = _check_custody(conn, intent)
            remaining = _pet_cooldown_remaining(conn, soul_id, tamer_id, now)
            if remaining > 0:
                raise AffectionRefusal(
                    "pet_cooldown",
                    f"Petting on cooldown; {remaining:.0f}s remaining",
                )
            new_loyalty = min(1.0, float(soul["loyalty"]) + PET_LOYALTY_NUDGE)
            conn.execute(
                "UPDATE souls SET loyalty = ? WHERE soul_id = ?",
                (new_loyalty, soul_id),
            )
            conn.execute(
                "INSERT INTO pet_cooldowns (soul_id, tamer_id, last_pet_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(soul_id, tamer_id) DO UPDATE SET "
                "last_pet_at = excluded.last_pet_at",
                (soul_id, tamer_id, now),
            )
            owner_id = _owner_of(conn, soul_id)
            _settle(
                conn,
                tick.tick_id,
                intent,
                "adjudicated",
                {"petted": True, "loyalty": new_loyalty},
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    assert new_loyalty is not None
    _emit_bubble(owner_id, soul_id, "\u2665")
    # High-salience loyalty-affection memory: petting is relationship
    # memory the arch's loyalty scalar learns from.
    agent_memory.log_episode(
        soul_id,
        "affection",
        {
            "summary": f"Your tamer petted you ({tamer_id})",
            "tamer": tamer_id,
            "loyalty": round(new_loyalty, 4),
        },
        salience=0.85,
    )


# ------------------------------------------------------------------
# carry


def _adjudicate_carry_move(tick, intent: dict) -> None:
    soul_id = intent["soul_id"]
    payload = intent["payload"] or {}
    phase = payload.get("phase", "move")
    if phase not in CARRY_PHASES:
        raise AffectionRefusal("bad_phase", f"Unknown carry phase: {phase!r}")
    # Issue #38: carry timing rides the adjudication clock; the escape
    # roll rides the scenario-seeded stream. In replay the in-memory
    # session is rebuilt by processing the window's carry intents in
    # order (same as live); a window that starts mid-carry cannot
    # rebuild it and the diff flags that honestly.
    now = determinism.tick_now(tick)
    rng = determinism.tick_rng(tick, "affection", intent["intent_id"])
    result: dict | None = None
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM intents WHERE intent_id = ?",
                (intent["intent_id"],),
            ).fetchone()
            if row is None or row["status"] != "pending":
                conn.rollback()
                return
            soul = _check_custody(conn, intent)
            if phase == "grab":
                result = _carry_grab(conn, intent, soul, now)
            elif phase == "move":
                result = _carry_move_step(conn, intent, soul, payload, now, rng=rng)
            else:
                result = _carry_release(conn, intent, now)
            _settle(conn, tick.tick_id, intent, "adjudicated", result)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if result is not None and result.get("escaped"):
        _emit_bubble(result.get("owner_id"), soul_id, "slips from your grasp!")
        agent_memory.log_episode(
            soul_id,
            "intent_outcome",
            {
                "summary": "You escaped your tamer's carry",
                "intent_id": intent["intent_id"],
                "kind": intent["kind"],
            },
            salience=0.6,
        )


def _carry_grab(conn, intent: dict, soul: dict, now: float) -> dict:
    soul_id = intent["soul_id"]
    if soul_id in _carries:
        raise AffectionRefusal("already_carried", "Soul is already being carried")
    last_escape = _escape_cooldowns.get(soul_id)
    if last_escape is not None and now - last_escape < ESCAPE_REGRAB_COOLDOWN_S:
        raise AffectionRefusal(
            "escape_cooldown",
            f"Soul just escaped; "
            f"{ESCAPE_REGRAB_COOLDOWN_S - (now - last_escape):.0f}s before re-grab",
        )
    x, y = _true_position(conn, soul_id)
    origin_plot = plots.plot_id_for(*plots.plot_at(x, y))
    _carries[soul_id] = _CarrySession(
        tamer_id=intent["custodian_id"],
        origin_plot=origin_plot,
        last_x=x,
        last_y=y,
        last_move_at=now,
        grabbed_at=now,
    )
    _write_position(conn, soul_id, x, y)
    return {"phase": "grab", "x": x, "y": y, "plot": origin_plot}


def _carry_move_step(
    conn, intent: dict, soul: dict, payload: dict, now: float, rng=None
) -> dict:
    soul_id = intent["soul_id"]
    owner_id = _owner_of(conn, soul_id)
    session = _carries.get(soul_id)
    if session is None:
        raise AffectionRefusal("no_carry_session", "Grab before streaming moves")
    if session.tamer_id != intent["custodian_id"]:
        raise AffectionRefusal("custody", "Another tamer holds this soul")
    px, py = _clamp_to_bounds(float(payload["x"]), float(payload["y"]))
    tx, ty = _true_position(conn, soul_id)
    if math.hypot(px - tx, py - ty) > CARRY_LEASH_RANGE:
        raise AffectionRefusal(
            "beyond_leash",
            f"Proposed position {CARRY_LEASH_RANGE}wu+ from true position",
        )
    if plots.plot_id_for(*plots.plot_at(px, py)) != session.origin_plot:
        raise AffectionRefusal(
            "plot_border",
            "Carry cannot cross a plot border; soul stays at last valid position",
        )
    dt = min(now - session.last_move_at, MAX_ESCAPE_DT_S)
    # Issue #38: the escape roll draws from the scenario-seeded
    # per-intent stream; unseeded it keeps the legacy module RNG.
    if roll_escape(soul.get("nature"), dt, rng=rng):
        _escape(conn, intent, tx, ty, now)
        return {
            "phase": "move",
            "escaped": True,
            "x": tx,
            "y": ty,
            "owner_id": owner_id,
        }
    session.last_x, session.last_y = px, py
    session.last_move_at = now
    _write_position(conn, soul_id, px, py)
    return {"phase": "move", "escaped": False, "x": px, "y": py}


def _escape(conn, intent: dict, tx: float, ty: float, now: float) -> None:
    """An escape drops the soul at its true position and starts the
    re-grab cooldown. Recorded as an episode; the bubble is emitted
    post-commit by the caller."""
    soul_id = intent["soul_id"]
    del _carries[soul_id]
    _escape_cooldowns[soul_id] = now
    _write_position(conn, soul_id, tx, ty)


def _carry_release(conn, intent: dict, now: float) -> dict:
    soul_id = intent["soul_id"]
    session = _carries.pop(soul_id, None)
    if session is None:
        return {"phase": "release", "was_carried": False}
    return {
        "phase": "release",
        "was_carried": True,
        "x": session.last_x,
        "y": session.last_y,
    }


# ------------------------------------------------------------------
# entry point


def _settled_outcome(intent_id: str) -> dict:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
    if row is None:
        return {"status": "missing", "result": None}
    result = row["result"]
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            result = None
    return {"status": row["status"], "result": result}


def adjudicate_affection_intent(tick, intent: dict) -> dict:
    """Tick-pump entry point for the pet grammar. Never raises.

    Returns the settled outcome: {"status": ..., "result": ...}.
    """
    kind = intent["kind"]
    try:
        if kind == KIND_CHIRP:
            _adjudicate_chirp(tick, intent)
        elif kind == KIND_PET:
            _adjudicate_pet(tick, intent)
        elif kind == KIND_CARRY_MOVE:
            _adjudicate_carry_move(tick, intent)
        else:
            _reject(tick.tick_id, intent, "unknown_kind", log_episode=False)
    except AffectionRefusal as refusal:
        # Carry move rejections are routine (leash/border spam while a
        # tamer drags wildly); they journal but never write episodes.
        _reject(
            tick.tick_id,
            intent,
            refusal.reason,
            refusal.detail,
            log_episode=(kind != KIND_CARRY_MOVE),
        )
    except Exception:
        logger.exception("affection adjudication failed: %s", intent["intent_id"])
        _reject(tick.tick_id, intent, "internal")
    return _settled_outcome(intent["intent_id"])
