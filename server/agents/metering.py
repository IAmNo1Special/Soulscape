"""LLM metering stage-1: usage events, decision traces, batched debits (issue #27).

Every #25 llm_usage row becomes exactly one metering_event (stable id
"llm_usage:<usage_id>", INSERT OR IGNORE), and every deliberation
writes one decision_trace joining deliberation -> usage event ->
intents -> adjudication outcomes.

Settlement form: system-authored `metering_debit` intent kinds (the
arch doc's "debit intents consumed by sim at tick boundary"; the issue
left the form to the implementer). The batcher claims unsettled events
into one intent per soul per batch; the tick pump adjudicates each
intent in a single BEGIN IMMEDIATE transaction: per-event ledger debit
(soul) + tax (Essence Fund) rows, cached balance updates, events marked
settled, trace outcomes materialized, dormancy transition journaled
with reason when the debit is unpayable. No escrow -- the system is the
author, so there is no buyer-side hold to protect.

Pricing: per-model USD/1k-token rates seeded from #25's MODEL_COSTS
plus an essence_per_usd conversion (default 100). Stored in
metering_config and operator-updatable. New settlements price at the
CURRENT knob; settled events keep their recorded essence_charged.

Unpayable debits: debited = min(balance, due); no debt is carried. The
shortfall is recorded on every event of the batch and in the journal,
and the soul flips dormant via #22's derived rule with journaled
reason "unpayable_llm_debit".

Idempotency: stable event/trace ids, INSERT OR IGNORE everywhere,
pending-only intent adjudication, and events marked settled inside the
settlement transaction. Batcher re-runs and crash restarts are no-ops.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from .. import database, determinism, dormancy, persistence

logger = logging.getLogger("soulscape_hub")

#: System-authored intent kind for batched LLM debits.
KIND_METERING_DEBIT = "metering_debit"
METERING_KINDS = {KIND_METERING_DEBIT}

#: Stable id prefixes (derived from the #25 llm_usage row).
EVENT_ID_PREFIX = "llm_usage:"
TRACE_ID_PREFIX = "trace:"

#: metering_config keys.
CONFIG_PRICING = "pricing"
CONFIG_LAST_BATCH_AT = "last_batch_at"
CONFIG_BATCH_SEQ = "batch_seq"

#: Default conversion rate: essence per USD of LLM spend.
#: *** TUNABLE -- Malcom: retune here if 100 is wrong. ***
DEFAULT_ESSENCE_PER_USD = 100.0

#: Batcher cadence: at most one debit batch per soul per this many seconds.
SETTLEMENT_CADENCE_S = 300.0

#: A soul whose unsettled accrued essence reaches this triggers an
#: immediate batch even inside the cadence window.
UNSETTLED_THRESHOLD_ESSENCE = 5.0

#: Journal event type for settled metering batches (canonical home:
#: persistence.EVENT_METERING_DEBIT_SETTLED; re-exported here).
EVENT_METERING_DEBIT_SETTLED = persistence.EVENT_METERING_DEBIT_SETTLED

#: Journaled reason when an unpayable debit freezes a soul.
REASON_UNPAYABLE_LLM_DEBIT = "unpayable_llm_debit"

#: Ledger entry types (reuse the #17 market vocabulary: a metering
#: debit drains the soul wallet, the matching tax row feeds the
#: Essence Fund -- verify_balances() already conserves both).
LEDGER_DEBIT = "debit"
LEDGER_TAX = "tax"


def _default_pricing() -> dict:
    """Seed pricing from #25's MODEL_COSTS. Lazy import: deliberation
    imports this module, so the import must stay deferred."""
    from . import deliberation as _delib

    return {
        "essence_per_usd": DEFAULT_ESSENCE_PER_USD,
        "model_rates": {
            model: [float(rates[0]), float(rates[1])]
            for model, rates in _delib.MODEL_COSTS.items()
        },
    }


def _get_config(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM metering_config WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row is not None else None


def _set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO metering_config (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_pricing(conn: sqlite3.Connection) -> dict:
    """Current pricing knob; lazily seeded from #25's MODEL_COSTS."""
    raw = _get_config(conn, CONFIG_PRICING)
    if raw is None:
        pricing = _default_pricing()
        _set_config(conn, CONFIG_PRICING, json.dumps(pricing))
        return pricing
    try:
        pricing = json.loads(raw)
        assert isinstance(pricing, dict)
        assert float(pricing.get("essence_per_usd", 0)) > 0
        assert isinstance(pricing.get("model_rates"), dict)
        return pricing
    except (ValueError, TypeError, AssertionError, KeyError):
        logger.warning("metering: corrupt pricing config; using defaults")
        return _default_pricing()


def set_pricing(
    conn: sqlite3.Connection,
    *,
    essence_per_usd: float | None = None,
    model_rates: dict[str, list[float]] | None = None,
) -> dict:
    """Operator pricing update. Affects NEW settlements only: settled
    events keep their recorded essence_charged."""
    pricing = get_pricing(conn)
    if essence_per_usd is not None:
        if not isinstance(essence_per_usd, (int, float)) or not (
            essence_per_usd > 0
        ):
            raise ValueError("essence_per_usd must be a positive number")
        pricing["essence_per_usd"] = float(essence_per_usd)
    if model_rates is not None:
        if not isinstance(model_rates, dict):
            raise ValueError("model_rates must be a mapping")
        cleaned: dict[str, list[float]] = {}
        for model, rates in model_rates.items():
            if (
                not isinstance(model, str)
                or not isinstance(rates, (list, tuple))
                or len(rates) != 2
            ):
                raise ValueError(f"bad rates for model {model!r}")
            in_rate, out_rate = float(rates[0]), float(rates[1])
            if in_rate < 0 or out_rate < 0:
                raise ValueError(f"negative rates for model {model!r}")
            cleaned[model] = [in_rate, out_rate]
        pricing["model_rates"] = cleaned
    _set_config(conn, CONFIG_PRICING, json.dumps(pricing))
    return pricing


def compute_essence(
    pricing: dict,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Essence for one usage event at the given pricing. Unknown
    models price at 0 (same convention as #25's estimate_cost)."""
    rates = (pricing.get("model_rates") or {}).get(model)
    if not rates:
        return 0.0
    usd = (
        prompt_tokens / 1000.0 * float(rates[0])
        + completion_tokens / 1000.0 * float(rates[1])
    )
    return round(usd * float(pricing["essence_per_usd"]), 6)


def record_event_for_usage(
    usage_id: int, conn: sqlite3.Connection | None = None
) -> str | None:
    """Turn one #25 llm_usage row into its metering event. Idempotent:
    the stable event id makes re-ingest a no-op."""
    event_id = f"{EVENT_ID_PREFIX}{usage_id}"
    if conn is None:
        with database.get_db() as owned:
            result = _record_event(owned, event_id, usage_id)
            owned.commit()
            return result
    return _record_event(conn, event_id, usage_id)


def _record_event(
    conn: sqlite3.Connection, event_id: str, usage_id: int
) -> str | None:
    row = conn.execute(
        "SELECT soul_id, tier, provider, model, prompt_tokens, "
        "completion_tokens, estimated_cost_usd, created_at "
        "FROM llm_usage WHERE usage_id = ?",
        (usage_id,),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "INSERT OR IGNORE INTO metering_events "
        "(event_id, soul_id, tier, provider, model, prompt_tokens, "
        "completion_tokens, cost_usd_estimate, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            row["soul_id"],
            row["tier"],
            row["provider"],
            row["model"],
            int(row["prompt_tokens"]),
            int(row["completion_tokens"]),
            float(row["estimated_cost_usd"]),
            float(row["created_at"]),
        ),
    )
    return event_id


def ingest_usage_records(conn: sqlite3.Connection) -> int:
    """Backfill metering events for llm_usage rows that have none
    (e.g. rows written before #27 deployed). Idempotent."""
    rows = conn.execute(
        "SELECT u.usage_id FROM llm_usage u "
        "LEFT JOIN metering_events e "
        "ON e.event_id = ? || CAST(u.usage_id AS TEXT) "
        "WHERE e.event_id IS NULL",
        (EVENT_ID_PREFIX,),
    ).fetchall()
    for row in rows:
        _record_event(conn, f"{EVENT_ID_PREFIX}{row['usage_id']}", row["usage_id"])
    return len(rows)


def record_decision_trace(
    conn: sqlite3.Connection,
    *,
    trace_id: str,
    soul_id: str,
    deliberation_id: int | None,
    usage_event_id: str | None,
    status: str,
    rationale: str,
    intents: list[dict],
) -> str:
    """One trace per deliberation: rationale + produced intents.
    Intent ids and adjudication outcomes join later (attach_intent_ids
    at enqueue; outcomes materialize at settlement). Idempotent."""
    conn.execute(
        "INSERT OR IGNORE INTO decision_traces "
        "(trace_id, soul_id, deliberation_id, usage_event_id, status, "
        "rationale, intents_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            trace_id,
            soul_id,
            deliberation_id,
            usage_event_id,
            status,
            (rationale or "")[:2000],
            json.dumps(intents),
            time.time(),
        ),
    )
    return trace_id


def attach_intent_ids(
    trace_id: str,
    intent_ids: list[str],
    conn: sqlite3.Connection | None = None,
) -> None:
    """Link a trace to the intent ids the pool enqueued for it.
    Merges, so double-attach is a no-op."""
    if conn is None:
        with database.get_db() as owned:
            _attach(owned, trace_id, intent_ids)
            owned.commit()
    else:
        _attach(conn, trace_id, intent_ids)


def _attach(
    conn: sqlite3.Connection, trace_id: str, intent_ids: list[str]
) -> None:
    row = conn.execute(
        "SELECT intent_ids_json FROM decision_traces WHERE trace_id = ?",
        (trace_id,),
    ).fetchone()
    if row is None:
        return
    have = json.loads(row["intent_ids_json"] or "[]")
    merged = list(dict.fromkeys([*have, *intent_ids]))
    if merged != have:
        conn.execute(
            "UPDATE decision_traces SET intent_ids_json = ? WHERE trace_id = ?",
            (json.dumps(merged), trace_id),
        )


def _unsettled_accrued(
    conn: sqlite3.Connection, pricing: dict
) -> dict[str, tuple[int, float]]:
    """Per-soul (event count, accrued essence at current pricing) for
    unsettled, unclaimed events. Claimed events are already in flight
    under a batch intent; the pump settles them."""
    rows = conn.execute(
        "SELECT soul_id, model, prompt_tokens, completion_tokens "
        "FROM metering_events WHERE settled_at IS NULL AND batch_id IS NULL"
    ).fetchall()
    accrued: dict[str, tuple[int, float]] = {}
    for row in rows:
        essence = compute_essence(
            pricing, row["model"], row["prompt_tokens"], row["completion_tokens"]
        )
        count, total = accrued.get(row["soul_id"], (0, 0.0))
        accrued[row["soul_id"]] = (count + 1, round(total + essence, 6))
    return accrued


def _next_batch_id(conn: sqlite3.Connection) -> str:
    raw = _get_config(conn, CONFIG_BATCH_SEQ)
    seq = int(float(raw)) + 1 if raw is not None else 1
    _set_config(conn, CONFIG_BATCH_SEQ, str(seq))
    return f"batch:{seq}"


def maybe_run_batch(
    conn: sqlite3.Connection | None = None,
    *,
    force: bool = False,
    now: float | None = None,
) -> dict:
    """The batcher. Ingests backfill usage, then claims unsettled
    events into one system-authored metering_debit intent per soul.

    Runs when the cadence elapsed OR some soul's unsettled accrued
    essence hit the threshold (force bypasses both). The tick pump
    settles the new intents at the tick boundary. Idempotent: events
    already claimed or settled are never re-batched.
    """
    now = time.time() if now is None else now
    if conn is None:
        with database.get_db() as owned:
            report = _maybe_run_batch(owned, force=force, now=now)
            owned.commit()
            return report
    return _maybe_run_batch(conn, force=force, now=now)


def _maybe_run_batch(
    conn: sqlite3.Connection, *, force: bool, now: float
) -> dict:
    ingested = ingest_usage_records(conn)
    pricing = get_pricing(conn)
    accrued = _unsettled_accrued(conn, pricing)
    if not accrued:
        conn.execute("SELECT 1")
        return {"created": 0, "ingested": ingested, "reason": "no_unsettled"}
    last_raw = _get_config(conn, CONFIG_LAST_BATCH_AT)
    last_batch_at = float(last_raw) if last_raw is not None else 0.0
    max_accrued = max(total for _, total in accrued.values())
    if (
        not force
        and now - last_batch_at < SETTLEMENT_CADENCE_S
        and max_accrued < UNSETTLED_THRESHOLD_ESSENCE
    ):
        return {
            "created": 0,
            "ingested": ingested,
            "reason": "cadence",
            "max_unsettled_accrued": max_accrued,
        }
    batch_id = _next_batch_id(conn)
    created: list[dict] = []
    for soul_id in sorted(accrued):
        count, due = accrued[soul_id]
        intent_id = f"metering:{batch_id}:{soul_id}"
        nonce = f"{batch_id}:{soul_id}"
        payload = {
            "batch_id": batch_id,
            "soul_id": soul_id,
            "n_events": count,
            "due_essence_estimate": due,
        }
        cursor = conn.execute(
            "INSERT OR IGNORE INTO intents "
            "(intent_id, session_id, nonce, custodian_id, soul_id, kind, "
            "payload, status, created_at) "
            "VALUES (?, 'metering', ?, NULL, ?, ?, ?, 'pending', ?)",
            (intent_id, nonce, soul_id, KIND_METERING_DEBIT, json.dumps(payload), now),
        )
        if cursor.rowcount:
            conn.execute(
                "UPDATE metering_events SET batch_id = ? "
                "WHERE soul_id = ? AND settled_at IS NULL AND batch_id IS NULL",
                (batch_id, soul_id),
            )
            created.append(
                {"soul_id": soul_id, "intent_id": intent_id, "events": count}
            )
    _set_config(conn, CONFIG_LAST_BATCH_AT, str(now))
    return {
        "created": len(created),
        "ingested": ingested,
        "batch_id": batch_id,
        "intents": created,
    }


def _write_ledger(
    conn: sqlite3.Connection,
    tick_id: int,
    intent_id: str,
    rows: list[tuple[str, str | None, float]],
    now: float | None = None,
) -> None:
    # Issue #38: ledger created_at rides the adjudication clock so a
    # seeded replay writes identical rows.
    now = time.time() if now is None else now
    conn.executemany(
        "INSERT INTO ledger "
        "(tick_id, intent_id, entry_type, soul_id, amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(tick_id, intent_id, entry_type, soul_id, amount, now)
         for entry_type, soul_id, amount in rows],
    )


def refresh_trace_outcomes(
    conn: sqlite3.Connection, event_ids: list[str]
) -> None:
    """Materialize adjudication outcomes for traces whose usage events
    are listed. Called at settlement, when the spend finalizes."""
    if not event_ids:
        return
    placeholders = ",".join("?" for _ in event_ids)
    traces = conn.execute(
        "SELECT trace_id, intent_ids_json FROM decision_traces "
        f"WHERE usage_event_id IN ({placeholders})",
        event_ids,
    ).fetchall()
    for trace in traces:
        intent_ids = json.loads(trace["intent_ids_json"] or "[]")
        outcomes: dict[str, dict] = {}
        for intent_id in intent_ids:
            row = conn.execute(
                "SELECT status, result FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                continue
            try:
                result = json.loads(row["result"]) if row["result"] else None
            except (ValueError, TypeError):
                result = None
            outcomes[intent_id] = {"status": row["status"], "result": result}
        conn.execute(
            "UPDATE decision_traces SET outcomes_json = ? WHERE trace_id = ?",
            (json.dumps(outcomes), trace["trace_id"]),
        )


def _settle_once(tick: Any, intent: dict[str, Any]) -> None:
    """Adjudicate one metering_debit intent in a single BEGIN IMMEDIATE
    transaction: per-event ledger debit/tax rows, cached balance and
    fund updates, events marked settled, trace outcomes materialized,
    intent status, and the journal event all commit together (RPO = 0).

    The intent row is re-read inside the write transaction and settled
    only when still pending, so two racing pumps can never
    double-settle the same batch. Crash before commit leaves events
    claimed-but-unsettled; the next pump settles them exactly once.
    """
    intent_id = intent["intent_id"]
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            status_row = conn.execute(
                "SELECT status FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if status_row is None or status_row["status"] != "pending":
                conn.rollback()
                return
            payload = intent.get("payload") or {}
            batch_id = payload.get("batch_id")
            soul_id = intent["soul_id"]
            pricing = get_pricing(conn)
            events = conn.execute(
                "SELECT event_id, model, prompt_tokens, completion_tokens "
                "FROM metering_events "
                "WHERE batch_id = ? AND settled_at IS NULL "
                "ORDER BY created_at, event_id",
                (batch_id,),
            ).fetchall()
            charges = [
                (
                    row["event_id"],
                    compute_essence(
                        pricing,
                        row["model"],
                        row["prompt_tokens"],
                        row["completion_tokens"],
                    ),
                )
                for row in events
            ]
            due = round(sum(charge for _, charge in charges), 6)
            essence_before = dormancy.cached_essence(conn, soul_id)
            soul_missing = essence_before is None
            balance = essence_before if essence_before is not None else 0.0
            debited = min(balance, due)
            shortfall = round(due - debited, 6)
            # Issue #38: settled_at + ledger timestamps ride the
            # adjudication clock so a seeded replay writes identical
            # rows.
            now = determinism.tick_now(tick)
            # No debt carry: the actually-available debit is allocated
            # across events in order. Ledger rows move only what was
            # really taken, so verify_balances() never drifts; each
            # event records its own charged amount and shortfall.
            remaining = debited
            journal_events = []
            for event_id, charge in charges:
                actual = round(min(charge, remaining), 6)
                remaining = round(remaining - actual, 6)
                event_shortfall = round(charge - actual, 6)
                debit_intent_id = f"{intent_id}:evt:{event_id}"
                _write_ledger(
                    conn,
                    tick.tick_id,
                    debit_intent_id,
                    [
                        (LEDGER_DEBIT, soul_id, actual),
                        (LEDGER_TAX, None, actual),
                    ],
                    now=now,
                )
                conn.execute(
                    "UPDATE metering_events SET essence_charged = ?, "
                    "shortfall_essence = ?, settled_at = ?, settled_by = ? "
                    "WHERE event_id = ?",
                    (actual, event_shortfall, now, intent_id, event_id),
                )
                journal_events.append(
                    {
                        "event_id": event_id,
                        "due_essence": charge,
                        "essence_charged": actual,
                        "shortfall_essence": event_shortfall,
                    }
                )
            if not soul_missing and (debited > 0 or due > 0):
                conn.execute(
                    "UPDATE souls SET essence = essence - ? WHERE soul_id = ?",
                    (debited, soul_id),
                )
                conn.execute(
                    "UPDATE globals SET value = value + ? "
                    "WHERE key = 'essence_fund'",
                    (debited,),
                )
            refresh_trace_outcomes(conn, [event_id for event_id, _ in charges])
            essence_after = balance - debited
            if shortfall > 1e-9:
                dormancy.note_essence_change(
                    conn,
                    soul_id,
                    balance,
                    tick.tick_id,
                    reason=REASON_UNPAYABLE_LLM_DEBIT,
                    shortfall=shortfall,
                    # Issue #38: adjudication clock, not wall clock.
                    now=now,
                )
            elif not soul_missing:
                dormancy.note_essence_change(
                    conn, soul_id, balance, tick.tick_id, now=now
                )
            result = {
                "batch_id": batch_id,
                "n_events": len(charges),
                "due_essence": due,
                "debited_essence": round(debited, 6),
                "shortfall_essence": shortfall,
                "essence_before": round(balance, 6),
                "essence_after": round(essence_after, 6),
                "soul_missing": soul_missing,
                "essence_per_usd": pricing["essence_per_usd"],
            }
            conn.execute(
                "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                ("adjudicated", json.dumps(result), intent_id),
            )
            persistence.append_event(
                conn,
                tick.tick_id,
                EVENT_METERING_DEBIT_SETTLED,
                {
                    "intent_id": intent_id,
                    "batch_id": batch_id,
                    "soul_id": soul_id,
                    **result,
                    "events": journal_events,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _compensate_reject(tick: Any, intent: dict[str, Any]) -> None:
    """Last-resort settlement when adjudication raised unexpectedly:
    mark the intent rejected and release its event claims in one
    transaction, so a later batch re-claims the events -- no spend is
    ever lost or double-counted."""
    intent_id = intent["intent_id"]
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                conn.rollback()
                return
            batch_id = (intent.get("payload") or {}).get("batch_id")
            if batch_id is not None:
                conn.execute(
                    "UPDATE metering_events SET batch_id = NULL "
                    "WHERE batch_id = ? AND settled_at IS NULL",
                    (batch_id,),
                )
            conn.execute(
                "UPDATE intents SET status = 'rejected', result = ? "
                "WHERE intent_id = ?",
                (json.dumps({"reason": "internal"}), intent_id),
            )
            persistence.append_event(
                conn,
                tick.tick_id,
                persistence.EVENT_INTENT_REJECTED,
                {
                    "intent_id": intent_id,
                    "kind": intent["kind"],
                    "soul_id": intent["soul_id"],
                    "reason": "internal",
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception(
                "metering compensate failed for %s", intent["intent_id"]
            )


def adjudicate_metering_intent(tick: Any, intent: dict[str, Any]) -> None:
    """Tick-pump entry point for metering_debit intents. Never raises."""
    try:
        _settle_once(tick, intent)
    except Exception:
        logger.exception(
            "metering adjudication failed: %s", intent["intent_id"]
        )
        _compensate_reject(tick, intent)


def soul_summary(conn: sqlite3.Connection, soul_id: str) -> dict:
    """Dispute API: per-soul totals. Unsettled accrued essence is
    priced at the CURRENT knob (what the next settlement will charge)."""
    pricing = get_pricing(conn)
    row = conn.execute(
        "SELECT COUNT(*) AS calls, "
        "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
        "COALESCE(SUM(cost_usd_estimate), 0.0) AS cost_usd, "
        "COALESCE(SUM(CASE WHEN settled_at IS NOT NULL "
        "THEN essence_charged ELSE 0.0 END), 0.0) AS charged, "
        "COUNT(CASE WHEN settled_at IS NULL THEN 1 END) AS unsettled "
        "FROM metering_events WHERE soul_id = ?",
        (soul_id,),
    ).fetchone()
    unsettled = conn.execute(
        "SELECT model, prompt_tokens, completion_tokens "
        "FROM metering_events WHERE soul_id = ? AND settled_at IS NULL",
        (soul_id,),
    ).fetchall()
    accrued = round(
        sum(
            compute_essence(
                pricing, r["model"], r["prompt_tokens"], r["completion_tokens"]
            )
            for r in unsettled
        ),
        6,
    )
    return {
        "soul_id": soul_id,
        "calls": int(row["calls"]),
        "prompt_tokens": int(row["prompt_tokens"]),
        "completion_tokens": int(row["completion_tokens"]),
        "cost_usd_estimate": round(float(row["cost_usd"]), 6),
        "essence_charged": round(float(row["charged"]), 6),
        "unsettled_events": int(row["unsettled"]),
        "unsettled_accrued_essence": accrued,
        "essence_per_usd": pricing["essence_per_usd"],
        "dormant": dormancy.soul_is_dormant(conn, soul_id),
    }


def soul_line_items(
    conn: sqlite3.Connection, soul_id: str, limit: int = 50
) -> list[dict]:
    """Dispute API: each line walks usage event -> decision trace ->
    rationale + intents + outcomes -> ledger debit rows, the full
    end-to-end explanation of one spend."""
    events = conn.execute(
        "SELECT event_id, soul_id, tier, provider, model, prompt_tokens, "
        "completion_tokens, cost_usd_estimate, essence_charged, "
        "shortfall_essence, batch_id, created_at, settled_at, settled_by "
        "FROM metering_events WHERE soul_id = ? "
        "ORDER BY created_at DESC, event_id DESC LIMIT ?",
        (soul_id, limit),
    ).fetchall()
    # Outcomes are materialized at settlement, but an intent may still
    # have been pending then. Rebuild from current intent rows so a
    # dispute always shows the latest known outcome.
    refresh_trace_outcomes(conn, [e["event_id"] for e in events])
    lines = []
    for event in events:
        line: dict[str, Any] = {
            "event_id": event["event_id"],
            "tier": event["tier"],
            "provider": event["provider"],
            "model": event["model"],
            "prompt_tokens": int(event["prompt_tokens"]),
            "completion_tokens": int(event["completion_tokens"]),
            "cost_usd_estimate": float(event["cost_usd_estimate"]),
            "essence_charged": (
                float(event["essence_charged"])
                if event["essence_charged"] is not None
                else None
            ),
            "shortfall_essence": float(event["shortfall_essence"]),
            "batch_id": event["batch_id"],
            "created_at": float(event["created_at"]),
            "settled_at": (
                float(event["settled_at"]) if event["settled_at"] else None
            ),
            "debit_intent_id": (
                f"{event['settled_by']}:evt:{event['event_id']}"
                if event["settled_by"]
                else None
            ),
            "trace": None,
            "ledger_rows": [],
        }
        trace = conn.execute(
            "SELECT trace_id, deliberation_id, status, rationale, "
            "intents_json, intent_ids_json, outcomes_json "
            "FROM decision_traces WHERE usage_event_id = ?",
            (event["event_id"],),
        ).fetchone()
        if trace is not None:
            line["trace"] = {
                "trace_id": trace["trace_id"],
                "deliberation_id": trace["deliberation_id"],
                "status": trace["status"],
                "rationale": trace["rationale"],
                "intents": json.loads(trace["intents_json"] or "[]"),
                "intent_ids": json.loads(trace["intent_ids_json"] or "[]"),
                "outcomes": json.loads(trace["outcomes_json"] or "{}"),
            }
        if line["debit_intent_id"] is not None:
            rows = conn.execute(
                "SELECT ledger_id, tick_id, intent_id, entry_type, soul_id, "
                "amount, created_at FROM ledger WHERE intent_id = ?",
                (line["debit_intent_id"],),
            ).fetchall()
            line["ledger_rows"] = [dict(r) for r in rows]
        # Issue #38: forensic handoff -- the exact journal window the
        # replay CLI needs to re-run this line's settlement, plus the
        # latest snapshot anchor. The dispute endpoint identifies the
        # intent/window; the CLI performs the rerun (the API never
        # shells out).
        line["replay"] = _replay_handoff(conn, event["settled_by"])
        lines.append(line)
    return lines


def _replay_handoff(conn: sqlite3.Connection, settlement_intent_id: str | None) -> dict:
    """Replay metadata for one settled line (issue #38)."""
    seq_range = (
        persistence.journal_seq_range_for_intent(conn, settlement_intent_id)
        if settlement_intent_id
        else None
    )
    # The snapshot must precede the replay window: newest snapshot
    # with journal_seq strictly before the window start.
    snap = (
        persistence.newest_snapshot_before(conn, seq_range[0])
        if seq_range is not None
        else None
    )
    snapshot_id = snap["snapshot_id"] if snap is not None else None
    tail = (
        f"{seq_range[0]}-{seq_range[1]}"
        if seq_range is not None
        else "<journal-seq-lo>-<journal-seq-hi>"
    )
    return {
        "settlement_intent_id": settlement_intent_id,
        "journal_seq_range": list(seq_range) if seq_range else None,
        "snapshot_id": snapshot_id,
        "cli": (
            "uv run --package server python -m server.replay "
            f"--snapshot {snapshot_id if snapshot_id is not None else '<id>'} "
            f"--journal-tail {tail} --diff-against recorded"
        ),
    }
