"""Sensation recording (issue #24).

Sensations are the Soul's first-person read on notable outcomes:
illegal off-menu emissions, notable reflex results ("no food in
sight"). Storage is two-tier:

1. A bounded in-memory ring per soul (deque, RING_SIZE=32) for fast
   inclusion in the think observation payload. Rebuilt empty on boot:
   sensations are ephemeral impressions, not durable state.
2. Durable journal rows (agent_sensation / agent_stale_reject) so the
   audit trail survives restarts. Stale-intent rejections are journaled
   ONLY (silently): they surface nowhere, not even in the ring.

Sensation text is clamped to 280 characters (the arch's untrusted-text
discipline); callers should prefer fixed strings for the canonical
cases so tests and clients can match byte-exactly.
"""

import time
from collections import deque

from .. import persistence

#: Journal event type for a recorded sensation (durable audit trail).
EVENT_AGENT_SENSATION = "agent_sensation"

#: Journal event type for a silently rejected stale intent (no ring
#: entry, no surface -- journaled, not surfaced).
EVENT_AGENT_STALE_REJECT = "agent_stale_reject"

#: Per-soul ring capacity. Oldest sensations fall off the end.
RING_SIZE = 32

#: Max sensation text length (arch untrusted-text clamp).
MAX_SENSATION_LEN = 280

_ring: dict[str, deque] = {}


def record(
    soul_id: str,
    text: str,
    cause: str,
    tick_id: int = 0,
    journal_conn=None,
) -> dict:
    """Record a sensation in the ring and, with a connection, the journal.

    Returns the sensation dict. Text is clamped to MAX_SENSATION_LEN.
    """
    sensation = {
        "soul_id": soul_id,
        "text": str(text)[:MAX_SENSATION_LEN],
        "cause": cause,
        "at": time.time(),
    }
    ring = _ring.setdefault(soul_id, deque(maxlen=RING_SIZE))
    ring.append(sensation)
    if journal_conn is not None:
        persistence.append_event(
            journal_conn,
            tick_id,
            EVENT_AGENT_SENSATION,
            {
                "soul_id": soul_id,
                "text": sensation["text"],
                "cause": cause,
            },
        )
    return sensation


def journal_stale_reject(
    journal_conn,
    tick_id: int,
    soul_id: str,
    action: str,
    payload: dict,
    reason: str,
) -> int:
    """Journal a stale intent's silent rejection. No ring entry: the
    soul never feels this; it is purely audit trail."""
    return persistence.append_event(
        journal_conn,
        tick_id,
        EVENT_AGENT_STALE_REJECT,
        {
            "soul_id": soul_id,
            "action": action,
            "payload": payload,
            "reason": reason,
            "vocab_version": 1,
        },
    )


def recent(soul_id: str, limit: int = 8) -> list[dict]:
    """Most recent sensations for a soul, newest last, up to `limit`."""
    return list(_ring.get(soul_id, deque()))[-max(limit, 0) :]


def clear(soul_id: str | None = None) -> None:
    """Drop ring entries (tests / boot rebuild)."""
    if soul_id is None:
        _ring.clear()
    else:
        _ring.pop(soul_id, None)
