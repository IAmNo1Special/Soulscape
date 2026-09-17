"""Ambient recap: overnight note from the typed journal (issue #33).

One recap per soul per ~day, generated from the typed journal (#27)
plus the ``recap_sources`` queue (#32). Deterministic selection --
NO LLM (the #26 precedent: recaps are deterministic selection, not
generation). All decisions are documented here and covered by tests.

Generation trigger (decided): LAZY ON UNLOCK, with a tick-sweep
backstop. Rationale: the authoritative tick only runs behind the
HUB_AUTHORITATIVE flag, but the arch doc says "wake triggers
generation if last recap >22h ago" -- a wake/unlock must be able to
mint the recap by itself. So ``on_unlock`` generates when the soul's
last recap is older than RECAP_MIN_AGE_S (22 h, arch doc) and then
emits the morning-note bubble. The world tick additionally sweeps
every RECAP_SWEEP_EVERY_TICKS so souls whose tamers never unlock
still get recaps, and runs retention compaction daily. Both reuse the
existing cadence -- no new threads.

Selection rule (documented): within the window (since the soul's
last recap, else the trailing 24 h), candidate events are journal
rows whose payload names this soul_id plus this soul's unconsumed
recap_sources rows. Sort key, all deterministic:

    (kind_priority DESC, salience DESC, created_at DESC, seq DESC)

kind_priority is the table below; salience is the payload's
"salience" when present else the kind's default; created_at/seq are
the final tiebreaks (newest first). Top RECAP_MAX_LINES events become
the recap lines. Every line cites the real rows it came from
({"type": "journal"|"source", "id": ...}) and the generation itself
is journaled as a typed ``recap_generated`` row, so the recap is
evidence-backed end to end.

Morning-note bubble (decided): emitted on unlock when a FRESH
(unshown) recap exists -- exactly one bubble per unlock cycle,
gated by ``recaps.shown_at``. A second unlock with no new recap emits
nothing; a new recap (new day) re-arms the note. Text is <=3 lines:
up to 2 highlight lines plus the unanswered-mailbag count line;
a quiet night still gets one honest line on the unlock path. (The
tick backstop skips quiet souls instead, so an idle tick writes no
journal row.) The bubble is
``solicited=True``: the tamer's own unlock is the soliciting action,
so it bypasses quiet hours / work mode / hourly caps -- but NOT
per-soul mutes, which the client's noise policy enforces even for
solicited bubbles (#30). shown_at is set on emit regardless of
viewport delivery: the once-per-cycle guarantee is per unlock, not
per delivery (a client that was offline reads the recap in the
dashboard instead).

Retention compaction (decided): 30-day rolling, on the existing tick
cadence (COMPACT_EVERY_TICKS), no new threads.

- Journal rows older than 30 d: salience-weighted. salience(row) =
  payload "salience" if present else the kind default table below.
  >= SALIENCE_KEEP (0.5) kept verbatim; < 0.5 merged into ONE
  per-day ``journal_rollup`` journal row (payload: day, counts per
  type, up to 3 sample texts, rolled seqs), originals deleted.
  ``recap_generated`` rows keep salience 0.9 so recap citations stay
  resolvable; rollup rows themselves are salience 1.0 (never re-rolled).
- recap_sources rows older than 30 d: merged into per-day
  kind='rollup' rows (counts per kind), originals deleted.
- recaps older than 30 d: deleted (arch doc's 30-day recap retention).

Safety: journal deletion never touches rows at or above the oldest
retained snapshot's journal_seq, so snapshot recovery tails stay
gapless (persistence.journal_continuous). Storage bound after
compaction: per day at most 1 rollup + the day's high-salience rows
(high-salience rows are inherently rare by construction).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from . import database, persistence

logger = logging.getLogger("soulscape_hub")

#: Arch doc: wake triggers generation when the last recap is older.
RECAP_MIN_AGE_S = 22.0 * 3600.0

#: Trailing window when the soul has no previous recap.
RECAP_DEFAULT_WINDOW_S = 24.0 * 3600.0

#: Max highlight lines stored per recap.
RECAP_MAX_LINES = 6

#: Max lines in the morning-note bubble.
MORNING_NOTE_MAX_LINES = 3

#: 30-day rolling retention.
RETENTION_S = 30.0 * 86400.0

#: Journal salience >= this survives compaction verbatim.
SALIENCE_KEEP = 0.5

#: Journal event type for a generated recap (typed journal, #27).
EVENT_RECAP_GENERATED = "recap_generated"

#: Journal event type for a compaction rollup row.
EVENT_JOURNAL_ROLLUP = "journal_rollup"

#: recap_sources kind for a compaction rollup row.
SOURCE_KIND_ROLLUP = "rollup"

#: Bubble kind for the morning note (client ui/bubbles.py mirrors this).
BUBBLE_KIND_MORNING_NOTE = "morning_note"

#: World-tick sweep cadence: every 3600 ticks = 12 min at 5 Hz.
RECAP_SWEEP_EVERY_TICKS = 3600

#: World-tick compaction cadence: every 432000 ticks = 24 h at 5 Hz.
COMPACT_EVERY_TICKS = 432000

#: Kind priority for the deterministic selection sort (higher first).
#: Read whatever journal kinds exist; unknown kinds fall to 1.
KIND_PRIORITY = {
    "loyalty_delta": 10,
    "mailbag_unanswered": 9,
    "mailbag_answered": 8,
    "soul_woke": 7,
    "mailbag_expired": 6,
    "metering_debit_settled": 5,
    "mailbag_asked": 4,
    "soul_dormant": 3,
    "recap_generated": 2,
}

#: Default salience per kind when the payload carries none. Same scale
#: as #26 episode salience (mailbag_unanswered 0.6 matches its episode).
SALIENCE_DEFAULTS = {
    "loyalty_delta": 0.7,
    "mailbag_unanswered": 0.6,
    "soul_woke": 0.6,
    "mailbag_answered": 0.5,
    "recap_generated": 0.9,
    "journal_rollup": 1.0,
    "mailbag_expired": 0.4,
    "mailbag_asked": 0.4,
    "metering_debit_settled": 0.3,
    "soul_dormant": 0.3,
}


def _salience(event_type: str, payload: dict) -> float:
    sal = payload.get("salience")
    if isinstance(sal, (int, float)):
        return float(sal)
    return float(SALIENCE_DEFAULTS.get(event_type, 0.2))


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_line(event_type: str, payload: dict) -> str:
    """Human one-liner for a journal event. Reads whatever fields exist."""
    if event_type == "loyalty_delta":
        delta = payload.get("delta", 0.0)
        try:
            delta_f = float(delta)
        except (TypeError, ValueError):
            delta_f = 0.0
        src = payload.get("source", "?")
        return (
            f"Loyalty {delta_f:+.2f} ({src}): "
            f"{payload.get('before', '?')}→{payload.get('after', '?')}"
        )
    if event_type == "mailbag_answered":
        return f"Answered: {_short(payload.get('question', '?'), 90)}"
    if event_type == "mailbag_expired":
        return f"Unanswered: {_short(payload.get('question', '?'), 90)}"
    if event_type == "mailbag_asked":
        return f"Asked: {_short(payload.get('question', '?'), 90)}"
    if event_type == "metering_debit_settled":
        deb = payload.get("debited_essence", "?")
        return f"Debited {deb} essence (metering batch)"
    if event_type == "soul_woke":
        return "Woke from dormancy"
    if event_type == "soul_dormant":
        return "Went dormant"
    if event_type == "recap_generated":
        return f"Overnight recap ({payload.get('n_lines', 0)} highlights)"
    return f"{event_type}: {_short(json.dumps(payload), 90)}"


def _iter_soul_journal(conn, soul_id: str, since: float, until: float) -> list[dict]:
    """Journal rows in [since, until) whose payload names this soul."""
    rows = conn.execute(
        "SELECT seq, type, payload, created_at FROM journal "
        "WHERE created_at >= ? AND created_at < ? ORDER BY seq ASC",
        (since, until),
    ).fetchall()
    out = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("soul_id") != soul_id:
            continue
        out.append(
            {
                "type": "journal",
                "seq": int(row["seq"]),
                "event": str(row["type"]),
                "payload": payload,
                "created_at": float(row["created_at"]),
            }
        )
    return out


def _iter_soul_sources(conn, soul_id: str, since: float, until: float) -> list[dict]:
    rows = conn.execute(
        "SELECT source_id, kind, ref_id, summary, created_at FROM recap_sources "
        "WHERE soul_id = ? AND created_at >= ? AND created_at < ? "
        "ORDER BY source_id ASC",
        (soul_id, since, until),
    ).fetchall()
    return [
        {
            "type": "source",
            "source_id": int(r["source_id"]),
            "kind": str(r["kind"]),
            "ref_id": r["ref_id"],
            "summary": str(r["summary"]),
            "created_at": float(r["created_at"]),
        }
        for r in rows
    ]


def _candidate_sort_key(candidate: dict) -> tuple:
    if candidate["type"] == "journal":
        event = candidate["event"]
        payload = candidate["payload"]
        seq = candidate["seq"]
    else:
        event = candidate["kind"]
        payload = {"summary": candidate["summary"]}
        seq = candidate["source_id"]
    return (
        -KIND_PRIORITY.get(event, 1),
        -_salience(event, payload),
        -candidate["created_at"],
        -seq,
    )


def _render_source_line(candidate: dict) -> str:
    summary = _short(candidate["summary"], 110)
    if candidate["kind"] == "mailbag_unanswered":
        return f"Unanswered: {summary}"
    return f"{candidate['kind']}: {summary}"


def _select_lines(candidates: list[dict], limit: int = RECAP_MAX_LINES) -> list[dict]:
    """Deterministic pick: kind priority × salience, top N."""
    ordered = sorted(candidates, key=_candidate_sort_key)
    lines = []
    for cand in ordered[:limit]:
        if cand["type"] == "journal":
            text = _render_line(cand["event"], cand["payload"])
            cite = {"type": "journal", "seq": cand["seq"], "event": cand["event"]}
        else:
            text = _render_source_line(cand)
            cite = {
                "type": "source",
                "source_id": cand["source_id"],
                "kind": cand["kind"],
            }
        lines.append({"text": text, "cites": [cite]})
    return lines


def _last_recap(conn, soul_id: str) -> dict | None:
    row = conn.execute(
        "SELECT recap_id, generated_at FROM recaps WHERE soul_id = ? "
        "ORDER BY generated_at DESC LIMIT 1",
        (soul_id,),
    ).fetchone()
    return dict(row) if row else None


def maybe_generate_recap(
    soul_id: str,
    now: float | None = None,
    tick_id: int = 0,
    allow_quiet: bool = True,
) -> dict | None:
    """Generate the soul's recap when the last one is >22 h old.

    Returns the recap dict (new or existing), or None when a recent
    recap already exists. Journaled as ``recap_generated`` (the #16
    discipline: recap rows are journaled too).

    ``allow_quiet=False`` (used by the tick backstop) skips souls with
    no journaled activity and no unanswered mailbag -- there is nothing
    to recap, so no journal row is written and the tick stays silent.
    The unlock path keeps ``allow_quiet=True`` so a quiet night still
    gets its one honest line and the morning bubble still fires.
    """
    now = time.time() if now is None else now
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            last = _last_recap(conn, soul_id)
            if last is not None and now - float(last["generated_at"]) < (
                RECAP_MIN_AGE_S
            ):
                conn.rollback()
                return None
            window_start = (
                float(last["generated_at"])
                if last is not None
                else now - RECAP_DEFAULT_WINDOW_S
            )
            events = _iter_soul_journal(conn, soul_id, window_start, now)
            sources = _iter_soul_sources(conn, soul_id, window_start, now)
            lines = _select_lines(events + sources)
            unanswered = [s for s in sources if s["kind"] == "mailbag_unanswered"]
            if not lines and not unanswered and not allow_quiet:
                conn.rollback()
                return None
            day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
            cited = []
            for line in lines:
                cited.extend(line["cites"])
            cursor = conn.execute(
                "INSERT INTO recaps (soul_id, day, lines, generated_at, shown_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (soul_id, day, json.dumps(lines), now),
            )
            recap_id = int(cursor.lastrowid)
            persistence.append_event(
                conn,
                tick_id,
                EVENT_RECAP_GENERATED,
                {
                    "soul_id": soul_id,
                    "recap_id": recap_id,
                    "day": day,
                    "window_start": window_start,
                    "n_lines": len(lines),
                    "line_texts": [line["text"] for line in lines],
                    "cited": cited,
                    "unanswered_mailbag": len(unanswered),
                    "unanswered_texts": [
                        _short(s["summary"], 140) for s in unanswered[:2]
                    ],
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    logger.info("recap generated: soul=%s day=%s lines=%d", soul_id, day, len(lines))
    return get_recap(conn=None, recap_id=recap_id)


def get_recap(conn, recap_id: int) -> dict | None:
    close = conn is None
    if close:
        ctx = database.get_db()
        conn = ctx.__enter__()
    try:
        row = conn.execute(
            "SELECT recap_id, soul_id, day, lines, generated_at, shown_at "
            "FROM recaps WHERE recap_id = ?",
            (recap_id,),
        ).fetchone()
        if row is None:
            return None
        recap = dict(row)
        recap["lines"] = json.loads(recap["lines"])
        return recap
    finally:
        if close:
            ctx.__exit__(None, None, None)


def fresh_recap(conn, soul_id: str) -> dict | None:
    """Newest recap for the soul that hasn't fed a morning note yet."""
    row = conn.execute(
        "SELECT recap_id, soul_id, day, lines, generated_at, shown_at "
        "FROM recaps WHERE soul_id = ? AND shown_at IS NULL "
        "ORDER BY generated_at DESC LIMIT 1",
        (soul_id,),
    ).fetchone()
    if row is None:
        return None
    recap = dict(row)
    recap["lines"] = json.loads(recap["lines"])
    return recap


def list_recaps(soul_id: str, limit: int = 30) -> list[dict]:
    """Recaps for a soul, newest first (dashboard browse)."""
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT recap_id, soul_id, day, lines, generated_at, shown_at "
            "FROM recaps WHERE soul_id = ? ORDER BY generated_at DESC LIMIT ?",
            (soul_id, limit),
        ).fetchall()
    out = []
    for row in rows:
        recap = dict(row)
        recap["lines"] = json.loads(recap["lines"])
        out.append(recap)
    return out


def _soul_owner(conn, soul_id: str) -> str | None:
    row = conn.execute(
        "SELECT COALESCE(custodian_id, owner_id) AS owner FROM souls WHERE soul_id = ?",
        (soul_id,),
    ).fetchone()
    return row["owner"] if row else None


def compose_morning_note(recap: dict, soul_name: str | None = None) -> list[str]:
    """<=3 lines: up to 2 highlights plus the unanswered-mailbag count."""
    lines = recap.get("lines") or []
    highlights = [_short(line.get("text", ""), 100) for line in lines[:2]]
    highlights = [h for h in highlights if h]
    unanswered = sum(
        1
        for line in lines
        for cite in line.get("cites", [])
        if cite.get("type") == "source" and cite.get("kind") == "mailbag_unanswered"
    )
    note = list(highlights)
    if unanswered:
        note.append(
            f"mailbag: {unanswered} unanswered question{'s' if unanswered != 1 else ''}"
        )
    note = note[:MORNING_NOTE_MAX_LINES]
    if not note:
        name = f" {soul_name}" if soul_name else ""
        note = [f"quiet night{name} — nothing new"]
    return note


def maybe_morning_note(soul_id: str, now: float | None = None) -> dict | None:
    """Emit ONE morning-note bubble when a fresh (unshown) recap exists.

    Returns {"recap_id", "lines", "sessions"} or None. Idempotent per
    cycle: the second call without a new recap finds nothing fresh.
    """
    from . import viewport

    now = time.time() if now is None else now
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            recap = fresh_recap(conn, soul_id)
            if recap is None:
                conn.rollback()
                return None
            name_row = conn.execute(
                "SELECT COALESCE(name, soul_id) AS name, "
                "COALESCE(custodian_id, owner_id) AS owner FROM souls "
                "WHERE soul_id = ?",
                (soul_id,),
            ).fetchone()
            owner = name_row["owner"] if name_row else None
            name = name_row["name"] if name_row else soul_id
            note_lines = compose_morning_note(recap, name)
            text = "\n".join(note_lines)
            sessions = viewport.viewport.notify_bubble(
                owner or "",
                soul_id,
                text,
                kind=BUBBLE_KIND_MORNING_NOTE,
                solicited=True,
            )
            conn.execute(
                "UPDATE recaps SET shown_at = ? WHERE recap_id = ?",
                (now, recap["recap_id"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    logger.info(
        "morning note: soul=%s recap=%d sessions=%d",
        soul_id,
        recap["recap_id"],
        sessions,
    )
    return {"recap_id": recap["recap_id"], "lines": note_lines, "sessions": sessions}


def on_unlock(tamer_id: str, now: float | None = None) -> list[dict]:
    """Unlock-cycle entry point: generate-if-due + maybe morning note.

    One bubble per soul per cycle at most. Returns the emitted notes.
    """
    now = time.time() if now is None else now
    emitted = []
    with database.get_db() as conn:
        souls = conn.execute(
            "SELECT soul_id FROM souls WHERE COALESCE(custodian_id, owner_id) = ?",
            (tamer_id,),
        ).fetchall()
        soul_ids = [r["soul_id"] for r in souls]
    for soul_id in soul_ids:
        try:
            maybe_generate_recap(soul_id, now=now)
        except Exception:
            logger.exception("recap generation failed: soul=%s", soul_id)
            continue
        try:
            note = maybe_morning_note(soul_id, now=now)
        except Exception:
            logger.exception("morning note failed: soul=%s", soul_id)
            continue
        if note is not None:
            emitted.append({"soul_id": soul_id, **note})
    return emitted


def sweep_recaps(now: float | None = None) -> int:
    """Tick backstop: generate due recaps for every soul. Returns count.

    Souls with no journaled activity and no unanswered mailbag are
    skipped (``allow_quiet=False``) so an idle tick writes nothing --
    the unlock path still gives quiet souls their one honest line.
    """
    now = time.time() if now is None else now
    with database.get_db() as conn:
        soul_ids = [
            r["soul_id"] for r in conn.execute("SELECT soul_id FROM souls").fetchall()
        ]
    made = 0
    for soul_id in soul_ids:
        try:
            if maybe_generate_recap(soul_id, now=now, allow_quiet=False) is not None:
                made += 1
        except Exception:
            logger.exception("recap sweep failed: soul=%s", soul_id)
    return made


def _snapshot_floor(conn) -> float:
    """Lowest journal seq any retained snapshot still needs. +inf when none."""
    row = conn.execute(
        "SELECT COALESCE(MIN(journal_seq), 0) AS floor FROM snapshots"
    ).fetchone()
    floor = int(row["floor"]) if row else 0
    if floor <= 0:
        row = conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()
        if int(row["n"]) == 0:
            return float("inf")
    return float(floor)


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def compact_journal(conn, now: float, cutoff: float) -> dict:
    """Salience-weighted compaction of old journal rows.

    Rows with salience >= SALIENCE_KEEP survive verbatim; the rest are
    merged into one per-day ``journal_rollup`` row. Never touches rows
    at/above the oldest snapshot's journal_seq (recovery tail stays
    gapless).
    """
    floor = _snapshot_floor(conn)
    rows = conn.execute(
        "SELECT seq, tick_id, type, payload, created_at FROM journal "
        "WHERE created_at < ? AND seq < ? ORDER BY seq ASC",
        (cutoff, floor),
    ).fetchall()
    kept = 0
    by_day: dict[str, list[dict]] = {}
    max_tick = 0
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        sal = _salience(str(row["type"]), payload)
        if sal >= SALIENCE_KEEP or str(row["type"]) == EVENT_JOURNAL_ROLLUP:
            kept += 1
            continue
        day = _utc_day(float(row["created_at"]))
        by_day.setdefault(day, []).append(
            {
                "seq": int(row["seq"]),
                "type": str(row["type"]),
                "payload": payload,
            }
        )
        max_tick = max(max_tick, int(row["tick_id"]))
    rolled = 0
    deleted = 0
    for day in sorted(by_day):
        members = by_day[day]
        counts: dict[str, int] = {}
        samples = []
        seqs = []
        for m in members:
            counts[m["type"]] = counts.get(m["type"], 0) + 1
            seqs.append(m["seq"])
            if len(samples) < 3:
                samples.append(_render_line(m["type"], m["payload"]))
        conn.execute(
            "INSERT INTO journal (tick_id, type, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                max_tick,
                EVENT_JOURNAL_ROLLUP,
                json.dumps(
                    {
                        "rollup": True,
                        "day": day,
                        "n": len(members),
                        "types": counts,
                        "samples": samples,
                        "rolled_seqs": seqs,
                        "salience": 1.0,
                    }
                ),
                now,
            ),
        )
        conn.execute(
            "DELETE FROM journal WHERE seq IN (%s)" % ",".join("?" * len(seqs)),
            seqs,
        )
        rolled += 1
        deleted += len(seqs)
    return {"kept": kept, "rollups": rolled, "deleted": deleted}


def compact_sources(conn, now: float, cutoff: float) -> dict:
    """Merge recap_sources rows older than the cutoff into per-day rollups."""
    rows = conn.execute(
        "SELECT source_id, kind, soul_id, created_at FROM recap_sources "
        "WHERE created_at < ? ORDER BY source_id ASC",
        (cutoff,),
    ).fetchall()
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        day = _utc_day(float(row["created_at"]))
        by_day.setdefault(day, []).append(dict(row))
    rolled = 0
    deleted = 0
    for day in sorted(by_day):
        members = by_day[day]
        counts: dict[str, int] = {}
        ids = []
        souls = set()
        for m in members:
            counts[m["kind"]] = counts.get(m["kind"], 0) + 1
            ids.append(m["source_id"])
            souls.add(m["soul_id"])
        conn.execute(
            "INSERT INTO recap_sources (kind, soul_id, ref_id, summary, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (
                SOURCE_KIND_ROLLUP,
                sorted(souls)[0] if len(souls) == 1 else "*",
                None,
                f"Rolled up {len(members)} recap sources from {day}: "
                + json.dumps(counts, sort_keys=True),
                now,
            ),
        )
        conn.execute(
            "DELETE FROM recap_sources WHERE source_id IN (%s)"
            % ",".join("?" * len(ids)),
            ids,
        )
        rolled += 1
        deleted += len(ids)
    return {"rollups": rolled, "deleted": deleted}


def compact_recaps(conn, cutoff: float) -> int:
    """Delete recaps older than the 30-day retention (arch doc)."""
    cursor = conn.execute("DELETE FROM recaps WHERE generated_at < ?", (cutoff,))
    return int(cursor.rowcount)


def compact_retention(now: float | None = None) -> dict:
    """30-day rolling compaction: journal + recap_sources + recaps."""
    now = time.time() if now is None else now
    cutoff = now - RETENTION_S
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            journal_report = compact_journal(conn, now, cutoff)
            sources_report = compact_sources(conn, now, cutoff)
            recaps_deleted = compact_recaps(conn, cutoff)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    report = {
        "journal": journal_report,
        "recap_sources": sources_report,
        "recaps_deleted": recaps_deleted,
    }
    logger.info("retention compaction: %s", report)
    return report
