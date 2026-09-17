"""Memory tiers: working / episodic / semantic (issue #26).

Three tiers feed the #25 deliberation prompt.

working   Volatile per-soul deques (observations, intents,
          deliberation rationales). The #24 sensation ring is the
          sensation tier of working memory: referenced here, not
          duplicated. working_context() merges both, newest last.
          Lost on restart by design.
episodic  Durable `episodes` table. Writers hook the think path:
          deliberations (with rationale), notable reflex firings
          (salience-gated: quiet ticks write nothing), intent
          outcomes, feed_soul events, dormancy transitions, restart
          wakes. Raw rows live 7 days; the nightly summarizer folds
          low-salience rows into `weekly_digests` and keeps
          high-salience (>= 0.7) rows verbatim.
semantic  Durable `semantic_memories` table: a SQLite-backed vector
          store. "Per-soul collections" (arch doc) reads as logical
          partitioning by soul_id -- one table indexed by soul is a
          collection per soul without a second database. ChromaDB was
          the arch's suggestion; this ships SQLite instead: no
          network, no heavy dependency tree, no embedding-provider
          keys, and at game scale (hundreds of souls, thousands of
          memories) brute-force cosine in Python is milliseconds
          (benchmarked in tests/test_memory.py).

Embeddings (v0): deterministic LOCAL hashed token vectors.
Unigrams + bigrams hashed with sha256 -- never Python's salted
hash() -- into 256 float32 dims, L2-normalized, stored as BLOBs.
Limits, stated plainly: this measures token overlap, not meaning;
"the blue gate" and "the azure portal" barely match. Determinism
is the point: replay and eval tests are bit-stable with no keys
and no network. The seam for provider embeddings is
embed_text(): swap its body (keys via the #23 vault, like #25's
providers) and nothing else changes; `dim` is stored per row so
a mixed-dim corpus fails loudly instead of silently.

Metadata-first retrieval: candidates are pre-filtered by metadata
(soul_id, last 30 days, salience >= 0.3) BEFORE any vector math,
then ranked by a combined score:

    combined = 0.45 * cosine + 0.30 * salience
             + 0.15 * recency + 0.10 * kind_match

cosine in [0, 1] (non-negative hashed vectors); recency =
exp(-age_s / 7d); kind_match = 1 when a kind token (feed, trade,
deliberation, ...) appears in the query. The weights say: meaning
first, but fresh and important beats stale and trivial.
tests/test_memory.py shows this beating a pure-cosine baseline on
a hand-built relevance set.

Nightly summarizer: heuristic extractive v0 (deterministic,
free). High-salience episodes stay verbatim and are embedded;
low-salience rows fold into per-kind counts + representative
samples in the weekly digest, whose text is itself embedded as a
digest-fact row. An LLM summarizer can plug into the
_fold_soul() seam later (metered under #27's budget, like #25).
"Nightly" = when a UTC day boundary passes: tick_maintenance()
runs a cheap in-memory-guarded check from the tick loop, and boot
catches up when >24h since the last run. Every run that folds
anything is journaled (memory_summarizer_run).

Restart wake: wake_context() reads the latest weekly digest plus
the last 10 high-salience episodes -- all durable in SQLite, so a
cold process restores identical recall. The first deliberation
after boot injects the wake block into the prompt (once per
process per soul); later deliberations use per-query semantic
retrieval instead.

Every row carries vocab_version (vocab.VOCAB_VERSION): memories
recorded under an older action vocabulary stay interpretable.
"""

import hashlib
import json
import logging
import math
import re
import struct
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from .. import database, persistence
from . import sensations, vocab

logger = logging.getLogger("soulscape_hub")

#: Working-memory kinds and their per-soul caps. Sensations are NOT
#: here: the #24 ring is the sensation tier (working_context merges
#: it); remember_working() rejects kind="sensation" loudly.
WORKING_CAPS = {
    "observation": 16,
    "intent": 16,
    "rationale": 8,
}

#: Canonical episode kinds. Writers must use one of these.
EPISODE_KINDS = (
    "deliberation",
    "reflex",
    "intent_outcome",
    "feed",
    "dormancy",
    "wake",
)

#: Salience >= this: kept verbatim by the summarizer AND written to
#: the semantic store immediately at log time (fresh recall).
HIGH_SALIENCE = 0.7

#: Semantic-write threshold (same as HIGH_SALIENCE today; separate
#: name so the two policies can diverge later).
SEMANTIC_WRITE_SALIENCE = 0.7

#: Retrieval metadata pre-filter: last 30d, salience >= 0.3.
RETRIEVAL_MIN_SALIENCE = 0.3
RETRIEVAL_RECENCY_WINDOW_S = 30 * 86400

#: Default top-k for semantic retrieval.
RETRIEVAL_TOP_K = 5

#: Embedding dimension (hashed vectors, v0).
EMBED_DIM = 256

#: Summarizer folds unsummarized episodes older than this.
SUMMARIZE_MIN_AGE_S = 24 * 3600

#: Raw (summarized) episode rows are pruned past this age.
EPISODE_RAW_RETENTION_S = 7 * 86400

#: High-salience episodes in the restart wake block.
WAKE_EPISODES_N = 10

#: Clamp on the wake block / full prompt memory block (chars).
WAKE_BLOCK_MAX_CHARS = 1500
MEMORY_BLOCK_MAX_CHARS = 1600

#: Journal event type for a summarizer run that folded episodes.
EVENT_SUMMARIZER_RUN = "memory_summarizer_run"

#: Globals key for the last summarizer run timestamp.
_GLOBAL_LAST_RUN = "memory_last_summarize_at"

#: Maintenance re-check interval (the in-memory cheap guard).
_MAINT_CHECK_INTERVAL_S = 3600.0

_working: dict[str, dict[str, deque]] = {}
_wake_delivered: set[str] = set()
_next_check_at = 0.0

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ------------------------------------------------------------------
# working memory (volatile)


def remember_working(soul_id: str, item: dict) -> dict:
    """Push one working-memory item for a soul.

    item: {"kind": "observation"|"intent"|"rationale", "text": str}.
    Bounded per kind (WORKING_CAPS); oldest falls off. Sensations go
    through sensations.record() (#24 ring), not here.
    """
    kind = item.get("kind")
    if kind not in WORKING_CAPS:
        raise ValueError(
            f"unknown working-memory kind: {kind!r}; sensations live "
            "in the #24 ring (sensations.record)"
        )
    entry = {
        "kind": kind,
        "text": str(item.get("text", ""))[:280],
        "at": float(item.get("at") or time.time()),
    }
    soul = _working.setdefault(soul_id, {})
    ring = soul.setdefault(kind, deque(maxlen=WORKING_CAPS[kind]))
    ring.append(entry)
    return entry


def working_context(soul_id: str, limit: int = 24) -> list[dict]:
    """Volatile working context, newest last: the #24 sensation ring
    merged with the observation/intent/rationale deques."""
    items = [
        {"kind": "sensation", "text": s["text"], "at": float(s["at"])}
        for s in sensations.recent(soul_id, limit=sensations.RING_SIZE)
    ]
    for ring in _working.get(soul_id, {}).values():
        items.extend(ring)
    items.sort(key=lambda e: e["at"])
    return items[-limit:] if limit else []


def working_notes(soul_id: str, limit: int = 6) -> list[str]:
    """Working-memory items (sans sensations) as short prompt lines."""
    lines = [
        f"[{e['kind']}] {e['text'][:160]}"
        for e in working_context(soul_id, limit=64)
        if e["kind"] != "sensation"
    ]
    return lines[-limit:] if limit else []


def reset_volatile(soul_id: str | None = None) -> None:
    """Drop process-volatile memory state (tests / boot). Clears the
    working deques AND the per-process wake-delivered set AND the
    maintenance cheap-guard, so a fresh process behaves identically."""
    global _next_check_at
    if soul_id is None:
        _working.clear()
        _wake_delivered.clear()
        _next_check_at = 0.0
    else:
        _working.pop(soul_id, None)
        _wake_delivered.discard(soul_id)


# ------------------------------------------------------------------
# deterministic local embeddings (v0)


def _token_counts(text: str) -> dict[str, float]:
    tokens = _TOKEN_RE.findall((text or "").lower())
    counts: dict[str, float] = {}
    for i, tok in enumerate(tokens):
        counts[tok] = counts.get(tok, 0.0) + 1.0
        if i + 1 < len(tokens):
            bigram = tok + "\x00" + tokens[i + 1]
            counts[bigram] = counts.get(bigram, 0.0) + 0.7
    return counts


def embed_text(text: str) -> bytes:
    """Deterministic local embedding (v0 seam for provider vectors).

    Hashed unigram+bigram token counts into EMBED_DIM float32 dims,
    L2-normalized, packed little-endian. sha256 -- never hash() --
    so vectors are bit-stable across processes and machines.
    """
    vec = [0.0] * EMBED_DIM
    for token, weight in _token_counts(text).items():
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vec[int.from_bytes(digest[:8], "little") % EMBED_DIM] += weight
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0.0:
        vec = [v / norm for v in vec]
    return struct.pack(f"<{EMBED_DIM}f", *vec)


def _cosine_packed(query: tuple[float, ...], blob: bytes) -> float:
    """Cosine between an unpacked query vector and a packed BLOB."""
    if not blob or len(blob) != EMBED_DIM * 4:
        return 0.0
    other = struct.unpack(f"<{EMBED_DIM}f", blob)
    dot = sum(a * b for a, b in zip(query, other))
    return max(0.0, min(1.0, dot))


# ------------------------------------------------------------------
# episodic log


def content_line(content: object) -> str:
    """One-line human rendering of episode content for digests,
    wake blocks, and semantic texts. Writers put a "summary" key in
    their content dict; that wins, JSON fallback otherwise."""
    data = content
    if isinstance(content, str):
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return content[:200]
    if isinstance(data, dict):
        summary = data.get("summary")
        if summary:
            return str(summary)[:200]
        rationale = data.get("rationale")
        if rationale:
            intents = data.get("intents") or []
            tail = f" (intents: {', '.join(map(str, intents))})" if intents else ""
            return (str(rationale)[:160] + tail)[:200]
    return (
        json.dumps(data, separators=(",", ":"), default=str)[:200]
        if not isinstance(content, str)
        else content[:200]
    )


def _insert_episode(
    conn,
    soul_id: str,
    kind: str,
    text: str,
    salience: float,
    ts: float,
    vocab_version: int,
) -> int:
    cursor = conn.execute(
        "INSERT INTO episodes "
        "(soul_id, ts, kind, salience, content, vocab_version, "
        "summarized) VALUES (?, ?, ?, ?, ?, ?, 0)",
        (soul_id, ts, kind, salience, text, vocab_version),
    )
    episode_id = int(cursor.lastrowid)
    if salience >= SEMANTIC_WRITE_SALIENCE:
        _upsert_semantic(
            conn,
            memory_id=f"ep:{episode_id}",
            soul_id=soul_id,
            episode_id=episode_id,
            text=content_line(text),
            embedding=embed_text(content_line(text)),
            salience=salience,
            kind=kind,
            created_at=ts,
            vocab_version=vocab_version,
        )
    return episode_id


def log_episode(
    soul_id: str,
    kind: str,
    content: dict | str,
    salience: float = 0.5,
    *,
    ts: float | None = None,
    conn=None,
    vocab_version: int | None = None,
) -> int:
    """Write one episodic row (vocab-stamped). High-salience rows
    (>= 0.7) are also embedded into the semantic store immediately
    so recall is fresh before the nightly run. With conn, the write
    joins the caller's transaction (no commit); otherwise it opens
    and commits its own."""
    if kind not in EPISODE_KINDS:
        raise ValueError(f"unknown episode kind: {kind!r}")
    salience = min(1.0, max(0.0, float(salience)))
    ts = time.time() if ts is None else float(ts)
    text = (
        content
        if isinstance(content, str)
        else json.dumps(content, separators=(",", ":"), default=str)
    )
    vv = vocab.VOCAB_VERSION if vocab_version is None else int(vocab_version)
    if conn is None:
        with database.get_db() as owned:
            episode_id = _insert_episode(
                owned, soul_id, kind, text, salience, ts, vv
            )
            owned.commit()
        return episode_id
    return _insert_episode(conn, soul_id, kind, text, salience, ts, vv)


def _upsert_semantic(
    conn,
    *,
    memory_id: str,
    soul_id: str,
    episode_id: int | None,
    text: str,
    embedding: bytes,
    salience: float,
    kind: str,
    created_at: float,
    vocab_version: int,
    tags: tuple = (),
) -> None:
    conn.execute(
        "INSERT INTO semantic_memories "
        "(memory_id, soul_id, episode_id, text, embedding, dim, "
        "salience, kind, tags, created_at, vocab_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(memory_id) DO UPDATE SET "
        "text=excluded.text, embedding=excluded.embedding, "
        "salience=excluded.salience, kind=excluded.kind, "
        "tags=excluded.tags, created_at=excluded.created_at, "
        "vocab_version=excluded.vocab_version",
        (
            memory_id,
            soul_id,
            episode_id,
            text[:2000],
            embedding,
            EMBED_DIM,
            salience,
            kind,
            json.dumps(list(tags)),
            created_at,
            vocab_version,
        ),
    )


# ------------------------------------------------------------------
# nightly summarizer


def _week_start(ts: float) -> float:
    monday = datetime.fromtimestamp(ts, tz=timezone.utc) - timedelta(
        days=datetime.fromtimestamp(ts, tz=timezone.utc).weekday()
    )
    return monday.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _fold_soul(conn, soul_id: str, rows: list, now: float) -> tuple[int, int]:
    """Fold one soul's due episodes. Returns (folded, kept_verbatim).

    High-salience rows stay verbatim (and are ensured in the semantic
    store); low-salience rows fold into per-week digests: per-kind
    counts + up to 2 representative samples per kind. Marks every
    processed row summarized=1. Idempotent: re-running finds no
    unsummarized rows and touches nothing.
    """
    kept = [r for r in rows if float(r["salience"]) >= HIGH_SALIENCE]
    low = [r for r in rows if float(r["salience"]) < HIGH_SALIENCE]
    for r in kept:
        line = content_line(r["content"])
        _upsert_semantic(
            conn,
            memory_id=f"ep:{r['episode_id']}",
            soul_id=soul_id,
            episode_id=int(r["episode_id"]),
            text=line,
            embedding=embed_text(line),
            salience=float(r["salience"]),
            kind=str(r["kind"]),
            created_at=float(r["ts"]),
            vocab_version=int(r["vocab_version"]),
        )
    weeks: dict[float, dict[str, list]] = {}
    for r in low:
        week = _week_start(float(r["ts"]))
        weeks.setdefault(week, {}).setdefault(str(r["kind"]), []).append(r)
    for week, by_kind in sorted(weeks.items()):
        day = datetime.fromtimestamp(week, tz=timezone.utc).strftime("%Y-%m-%d")
        parts = [
            f"Weekly digest -- week of {day} (UTC). "
            f"Folded {len(low)} low-salience episodes; "
            f"{len(kept)} high-salience kept verbatim."
        ]
        for kind in sorted(by_kind):
            kind_rows = by_kind[kind]
            samples = "; ".join(
                f'"{content_line(r["content"])}"' for r in kind_rows[:2]
            )
            parts.append(f"- {kind}: {len(kind_rows)}. e.g. {samples}")
        if kept:
            kept_lines = "; ".join(
                f'[{r["kind"]}] {content_line(r["content"])}' for r in kept
            )
            parts.append(f"High-salience kept verbatim: {kept_lines}")
        digest_text = "\n".join(parts)
        conn.execute(
            "INSERT INTO weekly_digests "
            "(soul_id, week_start, digest_text, episode_count, "
            "kept_count, vocab_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(soul_id, week_start) DO UPDATE SET "
            "digest_text=weekly_digests.digest_text || char(10) || "
            "excluded.digest_text, "
            "episode_count=weekly_digests.episode_count + "
            "excluded.episode_count, "
            "kept_count=weekly_digests.kept_count + excluded.kept_count",
            (
                soul_id,
                week,
                digest_text,
                len(low),
                len(kept),
                vocab.VOCAB_VERSION,
                now,
            ),
        )
        mem_id = f"digest:{soul_id}:{week:.0f}"
        _upsert_semantic(
            conn,
            memory_id=mem_id,
            soul_id=soul_id,
            episode_id=None,
            text=digest_text[:1200],
            embedding=embed_text(digest_text[:1200]),
            salience=0.6,
            kind="digest",
            created_at=now,
            vocab_version=vocab.VOCAB_VERSION,
        )
    ids = [int(r["episode_id"]) for r in rows]
    conn.execute(
        f"UPDATE episodes SET summarized = 1 WHERE episode_id IN "
        f"({','.join('?' for _ in ids)})",
        ids,
    )
    return len(low), len(kept)


def _tick_id(conn) -> int:
    row = conn.execute(
        "SELECT value FROM globals WHERE key = 'last_tick_id'"
    ).fetchone()
    return int(row["value"]) if row and row["value"] is not None else 0


def run_summarizer(
    now: float | None = None, min_age_s: float = SUMMARIZE_MIN_AGE_S
) -> dict:
    """Nightly summarizer: fold unsummarized episodes older than
    min_age_s into weekly digests, keep high-salience verbatim,
    prune summarized rows older than 7d. Returns a report dict.
    Idempotent: a second run with no new due episodes is a no-op
    (no digest touch, no journal row)."""
    now = time.time() if now is None else now
    cutoff = now - min_age_s
    report: dict = {
        "souls": 0,
        "episodes_folded": 0,
        "kept_verbatim": 0,
        "digests": 0,
        "pruned": 0,
    }
    with database.get_db() as conn:
        soul_ids = [
            r["soul_id"]
            for r in conn.execute(
                "SELECT DISTINCT soul_id FROM episodes "
                "WHERE summarized = 0 AND ts < ?",
                (cutoff,),
            )
        ]
        for soul_id in soul_ids:
            rows = conn.execute(
                "SELECT episode_id, ts, kind, salience, content, "
                "vocab_version FROM episodes "
                "WHERE soul_id = ? AND summarized = 0 AND ts < ? "
                "ORDER BY ts",
                (soul_id, cutoff),
            ).fetchall()
            if not rows:
                continue
            folded, kept = _fold_soul(conn, soul_id, rows, now)
            report["souls"] += 1
            report["episodes_folded"] += folded
            report["kept_verbatim"] += kept
            report["digests"] += 1
        pruned = conn.execute(
            "DELETE FROM episodes WHERE summarized = 1 AND ts < ?",
            (now - EPISODE_RAW_RETENTION_S,),
        ).rowcount
        report["pruned"] = pruned
        if report["episodes_folded"] or report["kept_verbatim"]:
            persistence.append_event(
                conn, _tick_id(conn), EVENT_SUMMARIZER_RUN, dict(report)
            )
        conn.commit()
    logger.info("summarizer run: %s", report)
    return report


def tick_maintenance(now: float | None = None) -> dict:
    """Cheap nightly check for the tick loop + boot catch-up.

    An in-memory guard limits this to one globals read per hour;
    the summarizer itself runs only when >24h elapsed since the
    last run. Returns {"ran": bool, ...report}.
    """
    global _next_check_at
    now = time.time() if now is None else now
    if now < _next_check_at:
        return {"ran": False}
    _next_check_at = now + _MAINT_CHECK_INTERVAL_S
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT value FROM globals WHERE key = ?", (_GLOBAL_LAST_RUN,)
        ).fetchone()
        last = float(row["value"]) if row and row["value"] is not None else None
    if last is not None and now - last < SUMMARIZE_MIN_AGE_S:
        return {"ran": False}
    report = run_summarizer(now=now)
    with database.get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO globals (key, value) VALUES (?, ?)",
            (_GLOBAL_LAST_RUN, now),
        )
        conn.commit()
    return {"ran": True, "report": report}


# ------------------------------------------------------------------
# semantic retrieval (metadata-first)


def _retrieve_scored(
    soul_id: str,
    query: str = "",
    k: int = RETRIEVAL_TOP_K,
    now: float | None = None,
) -> list[tuple]:
    """Metadata-first retrieval. Pre-filter (soul, 30d, salience >=
    0.3) BEFORE vector math, then combined ranking. Returns
    (combined, text, cosine, salience, age_s, kind) tuples, best
    first."""
    now = time.time() if now is None else now
    qvec = struct.unpack(f"<{EMBED_DIM}f", embed_text(query or ""))
    cutoff = now - RETRIEVAL_RECENCY_WINDOW_S
    qtokens = set(_TOKEN_RE.findall((query or "").lower()))
    scored = []
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT text, embedding, dim, salience, kind, created_at "
            "FROM semantic_memories "
            "WHERE soul_id = ? AND created_at >= ? AND salience >= ?",
            (soul_id, cutoff, RETRIEVAL_MIN_SALIENCE),
        ).fetchall()
    for r in rows:
        if int(r["dim"]) != EMBED_DIM:
            continue
        cosine = _cosine_packed(qvec, bytes(r["embedding"]))
        age = max(0.0, now - float(r["created_at"]))
        recency = math.exp(-age / (7 * 86400))
        kind_match = 1.0 if str(r["kind"]) in qtokens else 0.0
        combined = (
            0.45 * cosine
            + 0.30 * float(r["salience"])
            + 0.15 * recency
            + 0.10 * kind_match
        )
        scored.append(
            (combined, str(r["text"]), cosine, float(r["salience"]), age,
             str(r["kind"]))
        )
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[: max(k, 0)]


def retrieve_semantic(
    soul_id: str, query: str = "", k: int = RETRIEVAL_TOP_K
) -> list[str]:
    """Top-k semantic memory texts for a soul, metadata-first
    ranked. Empty query degrades gracefully to salience/recency
    ranking (cosine is 0 for all)."""
    return [text for _, text, _, _, _, _ in _retrieve_scored(soul_id, query, k)]


def baseline_retrieve(
    soul_id: str, query: str = "", k: int = RETRIEVAL_TOP_K
) -> list[str]:
    """Pure-cosine baseline over ALL of the soul's memories (no
    metadata pre-filter, no combined score). Exists for the eval in
    tests/test_memory.py: metadata-first must beat this."""
    qvec = struct.unpack(f"<{EMBED_DIM}f", embed_text(query or ""))
    scored = []
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT text, embedding, dim FROM semantic_memories "
            "WHERE soul_id = ?",
            (soul_id,),
        ).fetchall()
    for r in rows:
        if int(r["dim"]) != EMBED_DIM:
            continue
        scored.append((_cosine_packed(qvec, bytes(r["embedding"])), str(r["text"])))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [text for _, text in scored[: max(k, 0)]]


# ------------------------------------------------------------------
# restart wake


def wake_context(soul_id: str) -> dict:
    """Restart wake state, from durable SQLite only: the latest
    weekly digest plus the last WAKE_EPISODES_N high-salience
    episodes. Identical before and after a process restart."""
    with database.get_db() as conn:
        drow = conn.execute(
            "SELECT week_start, digest_text FROM weekly_digests "
            "WHERE soul_id = ? ORDER BY week_start DESC LIMIT 1",
            (soul_id,),
        ).fetchone()
        erows = conn.execute(
            "SELECT ts, kind, content FROM episodes "
            "WHERE soul_id = ? AND salience >= ? "
            "ORDER BY ts DESC LIMIT ?",
            (soul_id, HIGH_SALIENCE, WAKE_EPISODES_N),
        ).fetchall()
    episodes = [
        {
            "ts": float(r["ts"]),
            "kind": str(r["kind"]),
            "line": content_line(r["content"]),
        }
        for r in reversed(erows)
    ]
    return {
        "soul_id": soul_id,
        "digest": drow["digest_text"] if drow else None,
        "week_start": float(drow["week_start"]) if drow else None,
        "episodes": episodes,
    }


def wake_block(soul_id: str) -> str:
    """The exact LONG-TERM MEMORY block the deliberation prompt
    consumes on restart wake. Empty string when the soul has no
    durable memories yet."""
    ctx = wake_context(soul_id)
    if not ctx["digest"] and not ctx["episodes"]:
        return ""
    lines = ["LONG-TERM MEMORY (restored after restart):"]
    if ctx["digest"]:
        day = datetime.fromtimestamp(
            ctx["week_start"], tz=timezone.utc
        ).strftime("%Y-%m-%d")
        lines.append(f"Weekly digest (week of {day}):")
        lines.append(ctx["digest"][:900])
    if ctx["episodes"]:
        lines.append("Salient episodes:")
        lines.extend(
            f"- [{e['kind']}] {e['line'][:160]}" for e in ctx["episodes"]
        )
    return "\n".join(lines)[:WAKE_BLOCK_MAX_CHARS]


def pop_wake(soul_id: str) -> str | None:
    """Once-per-process restart wake: returns the wake block the
    first time it is called for a soul, None after. Logs a
    low-salience wake episode so the restore itself is episodic."""
    if soul_id in _wake_delivered:
        return None
    _wake_delivered.add(soul_id)
    block = wake_block(soul_id)
    if not block:
        return None
    ctx = wake_context(soul_id)
    log_episode(
        soul_id,
        "wake",
        {
            "summary": "restart wake: digest + "
            f"{len(ctx['episodes'])} salient episodes restored",
            "episodes": len(ctx["episodes"]),
            "digest": ctx["digest"] is not None,
        },
        salience=0.3,
    )
    return block


def memory_block_for_prompt(soul_id: str, query: str = "") -> str:
    """Compose the full LONG-TERM MEMORY prompt section: the
    once-per-process wake block, top-k semantic memories for the
    query, and recent working notes. Clamped to
    MEMORY_BLOCK_MAX_CHARS. Empty string when there is nothing."""
    parts = []
    wake = pop_wake(soul_id)
    if wake:
        parts.append(wake)
    mems = retrieve_semantic(soul_id, query)
    if mems:
        parts.append(
            "Notable memories:\n"
            + "\n".join(f"- {m[:200]}" for m in mems)
        )
    notes = working_notes(soul_id)
    if notes:
        parts.append("Working notes:\n" + "\n".join(f"- {n}" for n in notes))
    return "\n\n".join(parts)[:MEMORY_BLOCK_MAX_CHARS]
