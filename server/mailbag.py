"""Mailbag: soul questions answered by the tamer (issue #32).

Tamer questions for souls queue through the mailbag; souls triage them
as they think; answers arrive ambiently (tray notification + reply
bubble). This module owns the server side of that loop:

- ``mailbag`` table: one row per question (id, soul_id, question text,
  created_at, expires_at = created_at + 48 h, status
  pending/answered/expired, answer text, answered_at, latency).
- ``recap_sources`` table: the queue #33's ambient recap will consume.
  Expiry writes kind='mailbag_unanswered' rows (the seam).

Decisions (documented per the issue, all covered by tests):

1. Cap overflow: the 4th question is DROPPED with a journaled
   ``mailbag_cap_dropped`` reason. Replacing the oldest would silently
   erase a question the tamer already saw in their tray badge -- the
   honest reading of "capped" is drop, not replace.
2. "Free": a question costs nothing beyond the deliberation that
   already ran. It is NOT charged against the #30 quip budget -- quips
   are tamer-requested one-liners with a hard 3/day cap; mailbag
   questions are soul-initiated and ride free on the think output.
3. Emission source: only successful LLM deliberations
   (status == "deliberated") may attach a question. Reflex thinks are
   heuristics and the deterministic fallback policy never asks
   questions either: templated questions would be indistinguishable
   from genuine soul curiosity and would prompt the tamer for nothing
   real, so a degraded brain stays quiet. Max one question per thought;
   only when fewer than 3 are pending.
4. Question bubbles are UNSOLICITED (the tamer didn't ask for this
   one) and go through the #30 bubble seam subject to the noise caps.
   Answers, by contrast, ride a priority observation -- the soul must
   notice its own answer.
5. Answer pull-forward reuses #31's chirp mechanism exactly:
   the answer is recorded as a priority observation (cause
   ``mailbag_answer``) and ``scheduler.note_attention`` moves the
   soul's next think to now + PULL_FORWARD_DELAY (5 s), never later,
   honoring the 60 s minimum gap.
6. Loyalty: an answer nudges loyalty +0.02 (same scale as petting,
   clamped to [0.0, 1.0]); 48 h of silence decays it -0.01. Both write
   ``loyalty_delta`` journal rows (decision-trace style, #27) AND
   episodic memories (#26): kind='affection' for answers,
   kind='intent_outcome' for ignored questions.
7. Expiry: the world-tick sweeper (every MAILBAG_SWEEP_EVERY_TICKS
   ticks, reusing the existing cadence -- no new thread) marks
   pending questions past 48 h as expired. They are archived (kept,
   status='expired', never deleted) and their text is queued into
   recap_sources for the ambient recap.

Journal event types: mailbag_asked, mailbag_cap_dropped,
mailbag_answered, mailbag_expired, loyalty_delta.
"""

from __future__ import annotations

import secrets

from . import database, persistence
from .agents import memory as agent_memory
from .agents import scheduler as think_scheduler
from .agents import sensations

#: Max pending questions per soul. The 4th is dropped (see docstring).
MAILBAG_MAX_PENDING = 3

#: Pending questions live 48 h; then they expire unanswered.
MAILBAG_TTL_S = 48.0 * 3600.0

#: Max question text length (mirrors the bubble 280-char truncation).
MAILBAG_QUESTION_MAX_CHARS = 280

#: Max answer text length (mirrors the bubble 280-char truncation).
MAILBAG_ANSWER_MAX_CHARS = 280

#: Loyalty nudge when the tamer answers (same scale as petting).
ANSWER_LOYALTY_NUDGE = 0.02

#: Loyalty decay when a question expires unanswered.
IGNORE_LOYALTY_DECAY = -0.01

#: World-tick sweep cadence: every 300 ticks = 60 s at 5 Hz. Expiry
#: granularity of a minute is fine for a 48 h TTL.
MAILBAG_SWEEP_EVERY_TICKS = 300

#: Journal event types.
EVENT_MAILBAG_ASKED = "mailbag_asked"
EVENT_MAILBAG_CAP_DROPPED = "mailbag_cap_dropped"
EVENT_MAILBAG_ANSWERED = "mailbag_answered"
EVENT_MAILBAG_EXPIRED = "mailbag_expired"
EVENT_LOYALTY_DELTA = "loyalty_delta"

#: Recap-source kind for questions that expired unanswered (the #33 seam).
RECAP_KIND_UNANSWERED = "mailbag_unanswered"

#: Sensation cause for a delivered tamer answer.
CAUSE_MAILBAG_ANSWER = "mailbag_answer"

#: Bubble kind for question bubbles (client ui/bubbles.py mirrors this).
BUBBLE_KIND_MAILBAG = "mailbag"


def _new_question_id() -> str:
    return "q_" + secrets.token_urlsafe(12)


def _journal(conn, tick_id: int, event_type: str, payload: dict) -> None:
    persistence.append_event(conn, tick_id, event_type, payload)


def pending_count(conn, soul_id: str) -> int:
    """Number of pending (unanswered, unexpired) questions for a soul."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM mailbag WHERE soul_id = ? AND status = 'pending'",
        (soul_id,),
    ).fetchone()
    return int(row["c"])


def list_pending(conn, soul_id: str) -> list[dict]:
    """Pending questions for a soul, oldest first."""
    rows = conn.execute(
        "SELECT question_id, soul_id, question, created_at, expires_at "
        "FROM mailbag WHERE soul_id = ? AND status = 'pending' "
        "ORDER BY created_at ASC",
        (soul_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_question(conn, question_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM mailbag WHERE question_id = ?", (question_id,)
    ).fetchone()
    return dict(row) if row else None


def _soul_owner(conn, soul_id: str) -> tuple[str | None, str | None]:
    """Effective custodian (custodian_id else owner_id) + soul name."""
    row = conn.execute(
        "SELECT COALESCE(custodian_id, owner_id) AS custodian, name "
        "FROM souls WHERE soul_id = ?",
        (soul_id,),
    ).fetchone()
    if row is None:
        return None, None
    return row["custodian"], row["name"]


def _apply_loyalty_delta(
    conn,
    soul_id: str,
    delta: float,
    source: str,
    now: float,
    tick_id: int,
) -> float:
    """Apply a loyalty delta, clamped to [0.0, 1.0], and journal it
    decision-trace style (#27). Returns the new loyalty."""
    row = conn.execute(
        "SELECT COALESCE(loyalty, 0.5) AS loyalty FROM souls WHERE soul_id = ?",
        (soul_id,),
    ).fetchone()
    before = float(row["loyalty"]) if row else 0.5
    after = min(1.0, max(0.0, before + delta))
    conn.execute(
        "UPDATE souls SET loyalty = ? WHERE soul_id = ?", (after, soul_id)
    )
    _journal(
        conn,
        tick_id,
        EVENT_LOYALTY_DELTA,
        {
            "soul_id": soul_id,
            "source": source,
            "delta": delta,
            "before": round(before, 4),
            "after": round(after, 4),
            "at": now,
        },
    )
    return after


def _fan_question_bubble(
    owner_id: str | None, soul_id: str, question: str, question_id: str
) -> None:
    """Fan the question bubble post-commit (unsolicited: subject to
    the client's noise caps, quiet hours, work mode). The question_id
    rides the op payload so the client's tap handler can open the
    right question in the answer surface."""
    if not owner_id:
        return
    from . import viewport

    viewport.viewport.notify_bubble(
        owner_id,
        soul_id,
        question,
        kind=BUBBLE_KIND_MAILBAG,
        solicited=False,
        payload={"question_id": question_id},
    )


def ask_question(
    soul_id: str,
    question: str,
    now: float,
    tick_id: int = 0,
) -> dict:
    """Record one soul question in the mailbag.

    Enforces the 3-pending cap: at-cap souls get the question dropped
    with a journaled ``mailbag_cap_dropped`` reason. Empty questions
    are refused outright (no row, no journal). Returns a status dict;
    on success the question bubble fans to the owner's viewport
    sessions after commit (never inside the transaction, so a rollback
    can't leave a phantom bubble).
    """
    text = (question or "").strip()[:MAILBAG_QUESTION_MAX_CHARS]
    if not text:
        return {"status": "refused", "reason": "empty_question"}
    question_id = _new_question_id()
    expires_at = now + MAILBAG_TTL_S
    owner_id: str | None = None
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            soul = conn.execute(
                "SELECT soul_id FROM souls WHERE soul_id = ?", (soul_id,)
            ).fetchone()
            if soul is None:
                conn.rollback()
                return {"status": "refused", "reason": "soul_not_found"}
            if pending_count(conn, soul_id) >= MAILBAG_MAX_PENDING:
                _journal(
                    conn,
                    tick_id,
                    EVENT_MAILBAG_CAP_DROPPED,
                    {
                        "soul_id": soul_id,
                        "question": text,
                        "reason": "cap",
                        "cap": MAILBAG_MAX_PENDING,
                    },
                )
                conn.commit()
                return {
                    "status": "dropped",
                    "reason": "cap",
                    "cap": MAILBAG_MAX_PENDING,
                }
            conn.execute(
                "INSERT INTO mailbag (question_id, soul_id, question, "
                "created_at, expires_at, status) "
                "VALUES (?, ?, ?, ?, ?, 'pending')",
                (question_id, soul_id, text, now, expires_at),
            )
            _journal(
                conn,
                tick_id,
                EVENT_MAILBAG_ASKED,
                {
                    "question_id": question_id,
                    "soul_id": soul_id,
                    "question": text,
                    "expires_at": expires_at,
                },
            )
            owner_id, _ = _soul_owner(conn, soul_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    _fan_question_bubble(owner_id, soul_id, text, question_id)
    return {
        "status": "asked",
        "question_id": question_id,
        "soul_id": soul_id,
        "question": text,
        "expires_at": expires_at,
    }


def answer_question(
    question_id: str,
    soul_id: str,
    answer: str,
    now: float,
    tick_id: int = 0,
) -> dict:
    """Record the tamer's answer and deliver it to the soul.

    The answer is delivered as a PRIORITY observation (sensation cause
    ``mailbag_answer``) and the soul's next think is pulled forward via
    the same ``note_attention`` mechanism as #31's chirp (now + 5 s,
    honoring the 60 s minimum gap). Loyalty is nudged +0.02 with a
    journaled ``loyalty_delta`` entry and an affection episodic memory.
    Returns the delivery summary including answer latency.
    """
    text = (answer or "").strip()[:MAILBAG_ANSWER_MAX_CHARS]
    if not text:
        return {"status": "refused", "reason": "empty_answer"}
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = get_question(conn, question_id)
            if row is None:
                conn.rollback()
                return {"status": "refused", "reason": "not_found"}
            if row["soul_id"] != soul_id:
                conn.rollback()
                return {"status": "refused", "reason": "soul_mismatch"}
            if row["status"] != "pending":
                conn.rollback()
                return {
                    "status": "refused",
                    "reason": f"already_{row['status']}",
                }
            latency_ms = (now - float(row["created_at"])) * 1000.0
            conn.execute(
                "UPDATE mailbag SET status = 'answered', answer = ?, "
                "answered_at = ?, answer_latency_ms = ? "
                "WHERE question_id = ?",
                (text, now, latency_ms, question_id),
            )
            new_loyalty = _apply_loyalty_delta(
                conn, soul_id, ANSWER_LOYALTY_NUDGE, "mailbag_answered", now, tick_id
            )
            _journal(
                conn,
                tick_id,
                EVENT_MAILBAG_ANSWERED,
                {
                    "question_id": question_id,
                    "soul_id": soul_id,
                    "question": row["question"],
                    "answer": text,
                    "latency_ms": round(latency_ms, 1),
                    "loyalty": round(new_loyalty, 4),
                },
            )
            question_text = row["question"]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    # Post-commit: episodic memory (#26), priority observation, and the
    # think pull-forward. All after commit so a rollback leaves no
    # phantom memory/observation.
    agent_memory.log_episode(
        soul_id,
        "affection",
        {
            "summary": f"Your tamer answered your question: {question_text[:120]}",
            "question_id": question_id,
            "question": question_text[:280],
            "answer": text[:280],
            "latency_ms": round(latency_ms, 1),
            "loyalty": round(new_loyalty, 4),
        },
        salience=0.85,
    )
    # Journal the sensation delivery itself (the ring is volatile; the
    # journal is the durable record that the answer reached the brain).
    with database.get_db() as sconn:
        sensations.record(
            soul_id,
            f"Your tamer answered your question '{question_text[:160]}' -- "
            f"their answer: '{text[:160]}'",
            CAUSE_MAILBAG_ANSWER,
            tick_id=tick_id,
            journal_conn=sconn,
        )
        sconn.commit()
    think_scheduler.default().note_attention(soul_id, now)
    return {
        "status": "answered",
        "question_id": question_id,
        "soul_id": soul_id,
        "latency_ms": round(latency_ms, 1),
        "loyalty": round(new_loyalty, 4),
    }


def sweep_expired(now: float, tick_id: int = 0) -> list[dict]:
    """Mark pending questions past their 48 h TTL as expired.

    Expired questions are archived (kept, status='expired') and queued
    into recap_sources (kind='mailbag_unanswered') for #33's ambient
    recap. Silence decays loyalty -0.01 with a journaled loyalty_delta
    entry and an intent_outcome episodic memory. Returns the expired
    rows. Simulated-clock friendly: ``now`` is injected.
    """
    expired: list[dict] = []
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT question_id, soul_id, question, created_at "
                "FROM mailbag WHERE status = 'pending' AND expires_at <= ?",
                (now,),
            ).fetchall()
            for row in rows:
                qid = row["question_id"]
                soul_id = row["soul_id"]
                question_text = row["question"]
                conn.execute(
                    "UPDATE mailbag SET status = 'expired' WHERE question_id = ?",
                    (qid,),
                )
                new_loyalty = _apply_loyalty_delta(
                    conn,
                    soul_id,
                    IGNORE_LOYALTY_DECAY,
                    "mailbag_ignored",
                    now,
                    tick_id,
                )
                conn.execute(
                    "INSERT INTO recap_sources (kind, soul_id, ref_id, summary, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        RECAP_KIND_UNANSWERED,
                        soul_id,
                        qid,
                        f"Unanswered after 48h: {question_text[:200]}",
                        now,
                    ),
                )
                _journal(
                    conn,
                    tick_id,
                    EVENT_MAILBAG_EXPIRED,
                    {
                        "question_id": qid,
                        "soul_id": soul_id,
                        "question": question_text,
                        "unanswered_for_s": round(now - float(row["created_at"]), 1),
                        "loyalty": round(new_loyalty, 4),
                    },
                )
                expired.append(
                    {
                        "question_id": qid,
                        "soul_id": soul_id,
                        "question": question_text,
                    }
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    # Post-commit episodes (never inside the tx).
    for item in expired:
        agent_memory.log_episode(
            item["soul_id"],
            "intent_outcome",
            {
                "summary": "Your tamer never answered your question; "
                "it faded unanswered.",
                "question_id": item["question_id"],
                "question": item["question"][:280],
            },
            salience=0.6,
        )
    return expired


def queue_recap_source(
    conn, kind: str, soul_id: str, ref_id: str | None, summary: str, now: float
) -> int:
    """Append one row to the recap source queue (the #33 seam)."""
    cursor = conn.execute(
        "INSERT INTO recap_sources (kind, soul_id, ref_id, summary, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (kind, soul_id, ref_id, summary, now),
    )
    return int(cursor.lastrowid)


def recap_sources_for(kind: str, limit: int = 100) -> list[dict]:
    """Read recap source rows (what #33's ambient recap will consume)."""
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT source_id, kind, soul_id, ref_id, summary, created_at "
            "FROM recap_sources WHERE kind = ? ORDER BY created_at ASC LIMIT ?",
            (kind, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def pending_for_owner(
    conn, owner_id: str, *, all_souls: bool = False, soul_id: str | None = None
) -> list[dict]:
    """Pending questions across an owner's souls, oldest first.

    With all_souls (operator), returns every pending question. With
    soul_id (a soul-user identity), only that soul's questions.
    """
    base = (
        "SELECT m.question_id, m.soul_id, m.question, m.created_at, "
        "m.expires_at, COALESCE(s.name, m.soul_id) AS soul_name "
        "FROM mailbag m LEFT JOIN souls s ON s.soul_id = m.soul_id "
    )
    if all_souls:
        rows = conn.execute(
            base + "WHERE m.status = 'pending' ORDER BY m.created_at ASC"
        ).fetchall()
    elif soul_id is not None:
        rows = conn.execute(
            base
            + "WHERE m.status = 'pending' AND m.soul_id = ? "
            "ORDER BY m.created_at ASC",
            (soul_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            base
            + "WHERE m.status = 'pending' "
            "AND COALESCE(s.custodian_id, s.owner_id) = ? "
            "ORDER BY m.created_at ASC",
            (owner_id,),
        ).fetchall()
    return [dict(r) for r in rows]
