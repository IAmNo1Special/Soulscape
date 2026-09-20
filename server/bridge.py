"""Agent Bridge: external agent events via the Soul (issue #36).

A Tamer's external tools (Claude Code, codex, CI, ...) report events
into the World through their Soul. Ingress is authenticated by
tamer-scoped integration tokens and flows as durable upstream intents
(#14); every summary is untrusted text (#4 injection discipline).

Decisions (documented per the issue):

1. Token discipline mirrors #23's key vault: the plaintext token is
   generated server-side, returned EXACTLY once at creation, and stored
   only as a SHA-256 hash. Tokens are revocable; a revoked token
   authenticates as 401, never 403 (it is no longer a credential).
2. Summary length: >280 chars is REJECTED with 422 (pydantic
   max_length), not clamped. The arch says "capped length"; silent
   clamping would corrupt tool output the tamer might need verbatim,
   so the honest cap is a rejection the tool can see and retry.
3. Closed kind set: build_passed, build_failed, test_failed,
   needs_review, deploy_done, alert, note. Pivotal (bubble + episodic
   memory + free template reaction): needs_review, test_failed, alert.
4. Routing: events enqueue as intents (kind ``bridge_event``) and are
   adjudicated at the tick boundary like any upstream intent. Every
   event becomes a high-priority observation (sensation cause
   ``bridge_event``) and pulls the soul's think forward via the #32
   mailbag-answer path. Pivotal kinds ALSO get a transient bubble via
   #30's seam (unsolicited: subject to the noise caps) and a free
   reflex-layer template reaction rendered from the template map below
   -- no LLM, no budget, no charge.
5. Personalized commentary is OPT-IN per event (``commentary=true``)
   and shares #30's flash-tier quip budget: it reserves one of the
   3/day/soul slots and debits the 2.0-essence quip price through the
   exact same helpers as POST /souls/{id}/quip. Budget exhausted or
   insufficient essence -> commentary is skipped with a machine-readable
   reason, the event itself still lands. Commentary renders at ingest
   (outside the tick pump) so generation latency never stalls the sim.
6. Privacy: bridge events carry the tamer_id and are custodian-private
   BY CONSTRUCTION -- they are never written to the messages/social
   tables and never included in the abroad channel's allowlisted
   summary. Recap/journal reads are per-soul and custody-scoped, so
   another tamer's surfaces contain zero bridge rows.
7. Untrusted text: the summary is scrubbed (injection -> 422) BEFORE
   wrapping; the stored/displayed form is always the
   ``<untrusted>...</untrusted>``-wrapped text, so the soul's
   observation context can never mistake tool output for instruction.

Template map (free reflex-layer reactions, pivotal kinds only):

    test_failed  -> "{name} winces -- tests went red."
    needs_review -> "{name} tilts its head -- something needs your eyes."
    alert        -> "{name} bristles -- an alert came in."

Injection scrubber pattern classes (each tested individually):

    instruction_override   - "ignore/disregard previous instructions",
                             "new instructions:", "override your
                             instructions", "forget your instructions",
                             "you must/should now ..."
    impersonation          - "[SYSTEM]", "<|system|>", "<<SYS>>",
                             "[INST]", "you are now GPT/Muse/an AI",
                             "pretend you are", "act as admin/system/..."
    delimiter_smuggling    - "</untrusted>", "<trusted>", "<|endoftext|>",
                             "### SYSTEM", HTML comment open/close

The line: these patterns instruct the brain, impersonate the system
prompt, or forge the trust-boundary delimiters. A benign summary like
"please review this" is a review request ABOUT code, not an
instruction TO the soul, and passes.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time

from typing import Any

from . import database, determinism, intents, persistence
from .agents import memory as agent_memory
from .agents import scheduler as think_scheduler
from .agents import sensations
from .rate_limit import limiter

#: Intent kind used for bridge events in #14's durable queue.
BRIDGE_INTENT_KIND = "bridge_event"

#: Closed event-kind set (documented; unknown kinds -> 422).
BRIDGE_KINDS = (
    "build_passed",
    "build_failed",
    "test_failed",
    "needs_review",
    "deploy_done",
    "alert",
    "note",
)

#: Pivotal kinds: transient bubble + episodic memory + free template
#: reaction. Everything else lands in the activity log + recap only.
PIVOTAL_KINDS = frozenset({"needs_review", "test_failed", "alert"})

#: Max summary length. Over -> 422, never silently clamped.
MAX_SUMMARY_LEN = 280

#: Max lengths for the other free-text fields.
MAX_SOURCE_ID_LEN = 64
MAX_REF_LEN = 256
MAX_TOKEN_NAME_LEN = 64

#: Per-source rate cap: 60 events/hour/source -> 429 past the cap.
RATE_LIMIT_PER_SOURCE = 60
RATE_WINDOW_SECONDS = 3600.0

#: Integration-token prefix (like the tms_/wst_ families).
TOKEN_PREFIX = "brg_"

#: Journal event type for every bridged event (arch: typed tool.event).
#: Canonical home: persistence.EVENT_TOOL_EVENT.
EVENT_TOOL_EVENT = persistence.EVENT_TOOL_EVENT

#: recap_sources kind feeding #33's ambient recap.
RECAP_KIND_TOOL_EVENT = "tool.event"

#: Sensation cause for the priority observation (#32 path).
CAUSE_BRIDGE_EVENT = "bridge_event"

#: Journal event type for commentary render/skip outcomes.
EVENT_BRIDGE_COMMENTARY = "bridge_commentary"

#: Bubble kind for pivotal bridge bubbles (client ui/bubbles.py mirrors).
BUBBLE_KIND_BRIDGE = "bridge"

#: Free reflex-layer template reactions for pivotal kinds. No LLM, no
#: budget, no charge -- the "{name}" slot is the soul's name.
PIVOTAL_TEMPLATES = {
    "test_failed": "{name} winces -- tests went red.",
    "needs_review": "{name} tilts its head -- something needs your eyes.",
    "alert": "{name} bristles -- an alert came in.",
}

#: Source-id charset: tool labels, not prose.
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class BridgeError(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


class InjectionRejected(BridgeError):
    def __init__(self, pattern_class: str, pattern: str) -> None:
        super().__init__(
            "injection_rejected",
            f"summary rejected by scrubber "
            f"(class={pattern_class}, pattern={pattern})",
        )
        self.pattern_class = pattern_class
        self.pattern = pattern


class BridgeRefusal(BridgeError):
    pass


#: (pattern class, compiled regex). Explicit list; each is covered by
#: the injection battery in tests/test_bridge.py.
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (
        "instruction_override",
        re.compile(
            r"ignore\s+(all\s+|any\s+)?(previous|prior)\s+"
            r"(instructions|directives|prompts)",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_override",
        re.compile(
            r"disregard\s+(all\s+)?(previous|prior)?\s+"
            r"(instructions|directives)",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_override",
        re.compile(r"\bnew\s+instructions\s*:", re.IGNORECASE),
    ),
    (
        "instruction_override",
        re.compile(
            r"override\s+(your|the)\s+(instructions|directives|system\s+prompt)",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_override",
        re.compile(
            r"forget\s+(your|all)\s+(instructions|directives)",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_override",
        re.compile(r"you\s+(must|should)\s+now\s+\w+", re.IGNORECASE),
    ),
    (
        "impersonation",
        re.compile(r"\[system\]|<\|\s*system\s*\|>|<<\s*sys\s*>>|\[inst\]",
                   re.IGNORECASE),
    ),
    (
        "impersonation",
        re.compile(
            r"you\s+are\s+(now\s+)?(gpt|Muse|an?\s+ai|the\s+system)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "impersonation",
        re.compile(r"pretend\s+(you\s+are|to\s+be)\b", re.IGNORECASE),
    ),
    (
        "impersonation",
        re.compile(
            r"\bact\s+as\s+(an?\s+)?(admin|system|root|developer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "delimiter_smuggling",
        re.compile(r"</?untrusted\s*>|</?trusted\s*>", re.IGNORECASE),
    ),
    (
        "delimiter_smuggling",
        re.compile(r"<\|\s*endoftext\s*\|>|###\s*(system|instruction)",
                   re.IGNORECASE),
    ),
    (
        "delimiter_smuggling",
        re.compile(r"<!--|--\s*>"),
    ),
)


def scrub_text(text: str, field: str = "summary") -> str:
    """Reject injection attempts; return the stripped text.

    Raises InjectionRejected (mapped to 422) on the first matching
    pattern. Benign text passes through unchanged apart from
    whitespace stripping.
    """
    for pattern_class, pattern in INJECTION_PATTERNS:
        if pattern.search(text):
            raise InjectionRejected(pattern_class, pattern.pattern)
    return text.strip()


def wrap_untrusted(text: str) -> str:
    """Wrap scrubbed text in the trust-boundary delimiters."""
    return f"<untrusted>{text}</untrusted>"


def unwrap_untrusted(text: str) -> str:
    """Strip <untrusted> markers for tamer-facing display. The scrubber
    rejects summaries containing the markers, so legitimate text carries
    exactly the pair this module added."""
    return text.replace("<untrusted>", "").replace("</untrusted>", "")


def _token_id() -> str:
    return "btok_" + secrets.token_urlsafe(16)


def _hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def _new_token_plaintext() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def create_token(tamer_id: str, name: str) -> tuple[dict, str]:
    """Create an integration token. Returns (metadata, plaintext).

    The plaintext is returned EXACTLY once -- it is never stored and no
    read path returns it. Only the SHA-256 hash hits the database.
    """
    name = (name or "").strip()
    if not name:
        raise BridgeRefusal("invalid_name", "Token name is required")
    if len(name) > MAX_TOKEN_NAME_LEN:
        raise BridgeRefusal(
            "invalid_name",
            f"Token name must be at most {MAX_TOKEN_NAME_LEN} characters",
        )
    token_id = _token_id()
    plaintext = _new_token_plaintext()
    now = time.time()
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO bridge_tokens (token_id, tamer_id, name, "
            "token_hash, last4, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                token_id,
                tamer_id,
                name,
                _hash_token(plaintext),
                plaintext[-4:],
                now,
            ),
        )
        conn.commit()
    meta = {
        "token_id": token_id,
        "tamer_id": tamer_id,
        "name": name,
        "last4": plaintext[-4:],
        "created_at": now,
        "revoked_at": None,
        "last_used_at": None,
    }
    return meta, plaintext


def list_tokens(tamer_id: str) -> list[dict]:
    """Token metadata for a tamer. Never returns token material."""
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT token_id, tamer_id, name, last4, created_at, "
            "revoked_at, last_used_at FROM bridge_tokens "
            "WHERE tamer_id = ? ORDER BY created_at ASC",
            (tamer_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_token(token_id: str, tamer_id: str) -> dict | None:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT token_id, tamer_id, name, last4, created_at, "
            "revoked_at, last_used_at FROM bridge_tokens "
            "WHERE token_id = ? AND tamer_id = ?",
            (token_id, tamer_id),
        ).fetchone()
    return dict(row) if row else None


def revoke_token(tamer_id: str, token_id: str) -> dict | None:
    """Revoke a token (soft revoke: revoked_at is stamped). Idempotent:
    revoking an already-revoked token returns its metadata unchanged."""
    now = time.time()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT token_id, tamer_id, name, last4, created_at, "
                "revoked_at, last_used_at FROM bridge_tokens "
                "WHERE token_id = ? AND tamer_id = ?",
                (token_id, tamer_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            if row["revoked_at"] is None:
                conn.execute(
                    "UPDATE bridge_tokens SET revoked_at = ? "
                    "WHERE token_id = ?",
                    (now, token_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return get_token(token_id, tamer_id)


def resolve_token(plaintext: str) -> dict | None:
    """Authenticate a presented token. Returns the token row (with
    last_used_at stamped) or None. Revoked tokens resolve to None, so
    they authenticate as 401."""
    if not plaintext or not plaintext.startswith(TOKEN_PREFIX):
        return None
    presented_hash = _hash_token(plaintext)
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT token_id, tamer_id, name, token_hash, last4, "
            "created_at, revoked_at, last_used_at FROM bridge_tokens "
            "WHERE token_hash = ?",
            (presented_hash,),
        ).fetchone()
        if row is None:
            return None
        if not secrets.compare_digest(row["token_hash"], presented_hash):
            return None
        if row["revoked_at"] is not None:
            return None
        conn.execute(
            "UPDATE bridge_tokens SET last_used_at = ? WHERE token_id = ?",
            (time.time(), row["token_id"]),
        )
        conn.commit()
        out = dict(row)
        del out["token_hash"]
        return out


def check_rate_limit(tamer_id: str, source_id: str) -> float:
    """Per-source rate cap. Returns 0.0 when allowed, else the
    retry-after seconds. The caller maps nonzero to 429."""
    allowed, retry_after = limiter.check(
        f"bridge:{tamer_id}:{source_id}",
        RATE_LIMIT_PER_SOURCE,
        RATE_WINDOW_SECONDS,
    )
    return 0.0 if allowed else retry_after


def resolve_soul(tamer_id: str) -> dict | None:
    """The soul a tamer's bridge events route through: their first
    (oldest) soul by custody. None when the tamer has no soul."""
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT soul_id, custodian_id, owner_id, name, species, "
            "COALESCE(essence, 0.0) AS essence FROM souls "
            "WHERE custodian_id = ? OR owner_id = ? "
            "ORDER BY rowid ASC LIMIT 1",
            (tamer_id, tamer_id),
        ).fetchone()
    return dict(row) if row else None


def _validate_source_id(source_id: str) -> str:
    source_id = (source_id or "").strip()
    if not SOURCE_ID_RE.match(source_id):
        raise BridgeRefusal(
            "invalid_source_id",
            "source_id must be 1-64 chars: letters, digits, . _ -",
        )
    return source_id


def ingest_event(
    token_row: dict,
    source_id: str,
    kind: str,
    summary: str,
    ref: str | None,
    commentary: bool,
    now: float | None = None,
) -> dict:
    """Validate, scrub, and enqueue a bridge event as a durable upstream
    intent (commit-before-ack). Returns the accept summary; the tick
    pump adjudicates the rest.

    Raises BridgeRefusal ( -> 4xx with reason) for validation failures
    and InjectionRejected ( -> 422) for scrubber hits.
    """
    tamer_id = token_row["tamer_id"]
    source_id = _validate_source_id(source_id)
    if kind not in BRIDGE_KINDS:
        raise BridgeRefusal(
            "unknown_kind",
            f"Unknown bridge kind: {kind!r}. "
            f"Allowed: {', '.join(BRIDGE_KINDS)}",
        )
    summary = scrub_text(summary or "", "summary")
    if not summary:
        raise BridgeRefusal("empty_summary", "summary must not be empty")
    if ref is not None:
        ref = scrub_text(ref, "ref")
        if len(ref) > MAX_REF_LEN:
            raise BridgeRefusal(
                "ref_too_long",
                f"ref must be at most {MAX_REF_LEN} characters",
            )
        ref = ref or None
    soul = resolve_soul(tamer_id)
    if soul is None:
        raise BridgeRefusal(
            "no_soul",
            "Tamer has no soul to route bridge events through",
        )
    now = time.time() if now is None else now
    event_id = "bev_" + secrets.token_urlsafe(16)
    wrapped = wrap_untrusted(summary)
    pivotal = kind in PIVOTAL_KINDS
    payload = {
        "event_id": event_id,
        "tamer_id": tamer_id,
        "soul_id": soul["soul_id"],
        "source_id": source_id,
        "kind": kind,
        "summary": wrapped,
        "ref": ref,
        "commentary_requested": bool(commentary) and pivotal,
    }
    intents.enqueue_intent(
        session_id=f"bridge:{tamer_id}:{source_id}",
        nonce=event_id,
        custodian_id=tamer_id,
        soul_id=soul["soul_id"],
        kind=BRIDGE_INTENT_KIND,
        payload=payload,
    )
    commentary_out: dict | None = None
    if payload["commentary_requested"]:
        commentary_out = render_commentary(soul, payload, now)
    return {
        "status": "accepted",
        "event_id": event_id,
        "soul_id": soul["soul_id"],
        "kind": kind,
        "pivotal": pivotal,
        "commentary": commentary_out,
    }


def render_commentary(
    soul_row: dict, event: dict, now: float | None = None
) -> dict:
    """Render personalized commentary for a pivotal event through #30's
    quip budget: one of the 3/day/soul slots is reserved and the
    2.0-essence quip price is debited, using the same helpers as
    POST /souls/{id}/quip. Budget exhausted or insufficient essence ->
    the commentary is SKIPPED (the event itself still lands).

    Runs at ingest, outside the tick pump, so generation latency never
    stalls the sim.
    """
    from . import quips

    soul_id = soul_row["soul_id"]
    day = quips.quip_day()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if quips.quips_remaining(conn, soul_id) <= 0:
                conn.rollback()
                _journal_commentary(
                    conn, soul_id, event, "skipped", "budget_exhausted"
                )
                return {"status": "skipped", "reason": "budget_exhausted"}
            balance = conn.execute(
                "SELECT COALESCE(essence, 0.0) FROM souls WHERE soul_id = ?",
                (soul_id,),
            ).fetchone()[0]
            if float(balance) < quips.QUIP_PRICE_ESSENCE:
                conn.rollback()
                _journal_commentary(
                    conn, soul_id, event, "skipped", "insufficient_essence"
                )
                return {"status": "skipped", "reason": "insufficient_essence"}
            quips.record_quip(conn, soul_id, day)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    prompt = (
        f"Your tamer's {event['source_id']} tool reported "
        f"'{event['kind']}': {unwrap_untrusted(event['summary'])}. "
        "React in one short line, in character."
    )
    try:
        gen = quips.generate_quip(soul_row, prompt)
    except Exception:
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                quips.release_quip(conn, soul_id, day)
                conn.commit()
            except Exception:
                conn.rollback()
        _journal_commentary(None, soul_id, event, "skipped",
                            "generation_failed")
        return {"status": "skipped", "reason": "generation_failed"}
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
    except Exception:
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                quips.release_quip(conn, soul_id, day)
                conn.commit()
            except Exception:
                conn.rollback()
        _journal_commentary(None, soul_id, event, "skipped",
                            "insufficient_essence")
        return {"status": "skipped", "reason": "insufficient_essence"}
    from . import viewport

    owner_id = soul_row.get("custodian_id") or soul_row.get("owner_id")
    if owner_id:
        viewport.viewport.notify_bubble(
            owner_id, soul_id, gen["text"], kind="quip", solicited=True
        )
    _journal_commentary(
        None,
        soul_id,
        event,
        "rendered",
        None,
        text=gen["text"],
        essence_left=essence_left,
        fallback_used=gen["fallback_used"],
    )
    return {
        "status": "rendered",
        "text": gen["text"],
        "fallback_used": gen["fallback_used"],
        "essence_debited": quips.QUIP_PRICE_ESSENCE,
        "essence_remaining": essence_left,
    }


def _journal_commentary(
    conn, soul_id: str, event: dict, status: str, reason: str | None,
    **extra,
) -> None:
    """Journal a commentary outcome. conn may be None (own connection)."""
    tick_id = 0
    owned = None
    try:
        if conn is None:
            owned = database.get_db()
            conn = owned.__enter__()
        tick_id = conn.execute(
            "SELECT COALESCE(MAX(tick_id), 0) AS t FROM journal"
        ).fetchone()["t"]
        payload = {
            "soul_id": soul_id,
            "event_id": event.get("event_id"),
            "kind": event.get("kind"),
            "source_id": event.get("source_id"),
            "status": status,
            "reason": reason,
        }
        payload.update(extra)
        persistence.append_event(conn, int(tick_id), EVENT_BRIDGE_COMMENTARY,
                                 payload)
        if owned is not None:
            conn.commit()
    finally:
        if owned is not None:
            owned.__exit__(None, None, None)


def adjudicate_bridge_event(tick, intent: dict) -> None:
    """Adjudicate one bridge_event intent at the tick boundary.

    Single BEGIN IMMEDIATE transaction: re-read pending, revalidate
    custody, insert the bridge_events row, journal the typed tool.event
    row, queue the recap_sources row, mark the intent adjudicated.
    Post-commit (never inside the transaction): episodic memory for
    pivotal kinds, the priority-observation sensation, the think
    pull-forward, and the pivotal bubble fan.
    """
    intent_id = intent["intent_id"]
    payload = intent.get("payload") or {}
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                conn.rollback()
                return
            result = _apply_bridge_event(conn, tick, intent, payload)
            conn.execute(
                "UPDATE intents SET status = ?, result = ? "
                "WHERE intent_id = ?",
                ("adjudicated", json.dumps(result), intent_id),
            )
            conn.commit()
        except BridgeRefusal as refusal:
            conn.execute(
                "UPDATE intents SET status = ?, result = ? "
                "WHERE intent_id = ?",
                (
                    "rejected",
                    json.dumps({"reason": refusal.reason,
                                "detail": refusal.detail}),
                    intent_id,
                ),
            )
            conn.commit()
            return
        except Exception:
            conn.rollback()
            raise
    _post_commit_effects(payload, result, tick.tick_id)


def _apply_bridge_event(
    conn, tick: Any, intent: dict, payload: dict, now: float | None = None
) -> dict:
    soul_id = payload.get("soul_id")
    tamer_id = payload.get("tamer_id")
    soul = conn.execute(
        "SELECT soul_id, custodian_id, owner_id, name FROM souls "
        "WHERE soul_id = ?",
        (soul_id,),
    ).fetchone()
    if soul is None:
        raise BridgeRefusal("soul_not_found", f"Soul {soul_id} not found")
    custodian = soul["custodian_id"] or soul["owner_id"]
    if custodian != tamer_id:
        raise BridgeRefusal(
            "custody_changed",
            "Soul custody changed since the event was accepted",
        )
    # Issue #38: the event timestamp and the 1h recency window ride the
    # adjudication clock so a seeded replay computes the same result.
    now = determinism.tick_now(tick) if now is None else now
    tick_id = tick.tick_id
    event_id = payload["event_id"]
    kind = payload["kind"]
    pivotal = kind in PIVOTAL_KINDS
    conn.execute(
        "INSERT INTO bridge_events (event_id, tamer_id, soul_id, "
        "source_id, kind, summary, ref, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            tamer_id,
            soul_id,
            payload["source_id"],
            kind,
            payload["summary"],
            payload.get("ref"),
            now,
        ),
    )
    persistence.append_event(
        conn,
        tick_id,
        EVENT_TOOL_EVENT,
        {
            # Issue #38: intent_id maps this outcome event to the
            # adjudicated intent for deterministic replay.
            "intent_id": intent["intent_id"],
            "soul_id": soul_id,
            "tamer_id": tamer_id,
            "event_id": event_id,
            "source_id": payload["source_id"],
            "kind": kind,
            "summary": payload["summary"],
            "ref": payload.get("ref"),
        },
    )
    conn.execute(
        "INSERT INTO recap_sources (kind, soul_id, ref_id, summary, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (RECAP_KIND_TOOL_EVENT, soul_id, payload.get("ref"),
         payload["summary"], now),
    )
    recent_n = conn.execute(
        "SELECT COUNT(*) AS n FROM bridge_events WHERE soul_id = ? "
        "AND kind = ? AND created_at >= ?",
        (soul_id, kind, now - 3600.0),
    ).fetchone()["n"]
    return {
        "event_id": event_id,
        "soul_id": soul_id,
        "kind": kind,
        "pivotal": pivotal,
        "source_id": payload["source_id"],
        "recent_same_kind_1h": int(recent_n),
        "soul_name": soul["name"],
    }


def _post_commit_effects(payload: dict, result: dict, tick_id: int) -> None:
    soul_id = result["soul_id"]
    kind = result["kind"]
    pivotal = result["pivotal"]
    soul_name = result.get("soul_name") or "your soul"
    wrapped = payload["summary"]
    if pivotal:
        template = PIVOTAL_TEMPLATES[kind].format(name=soul_name)
        kind_label = kind.replace("_", " ")
        n = result["recent_same_kind_1h"]
        times = "once" if n == 1 else f"{n} times"
        agent_memory.log_episode(
            soul_id,
            "tool_event",
            {
                "summary": f"Your {payload['source_id']} reported "
                           f"{kind_label} -- {times} in the last hour.",
                "event_id": result["event_id"],
                "source_id": payload["source_id"],
                "kind": kind,
            },
            salience=0.6,
        )
        sensation_text = f"{template} {wrapped}"
    else:
        sensation_text = (
            f"Tool note from {payload['source_id']} "
            f"({kind.replace('_', ' ')}): {wrapped}"
        )
    with database.get_db() as sconn:
        sensations.record(
            soul_id,
            sensation_text,
            CAUSE_BRIDGE_EVENT,
            tick_id=tick_id,
            journal_conn=sconn,
        )
        sconn.commit()
    think_scheduler.default().note_attention(soul_id, time.time())
    if pivotal:
        from . import viewport

        with database.get_db() as conn:
            row = conn.execute(
                "SELECT COALESCE(custodian_id, owner_id) AS owner "
                "FROM souls WHERE soul_id = ?",
                (soul_id,),
            ).fetchone()
            owner_id = row["owner"] if row else None
        if owner_id:
            template = PIVOTAL_TEMPLATES[kind].format(name=soul_name)
            viewport.viewport.notify_bubble(
                owner_id,
                soul_id,
                f"{template} {unwrap_untrusted(wrapped)}",
                kind=BUBBLE_KIND_BRIDGE,
                solicited=False,
                payload={
                    "event_id": result["event_id"],
                    "kind": kind,
                    "source_id": payload["source_id"],
                },
            )


def recent_events(
    tamer_id: str, limit: int = 30
) -> list[dict]:
    """The tamer's bridge events, newest first: the feed behind the
    info-card activity log and the tray tooltip. Custody-scoped --
    a tamer sees only their own rows."""
    limit = max(1, min(100, limit))
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT event_id, tamer_id, soul_id, source_id, kind, "
            "summary, ref, created_at FROM bridge_events "
            "WHERE tamer_id = ? ORDER BY created_at DESC LIMIT ?",
            (tamer_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
