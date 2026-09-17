"""Sim-side executors for IPC ``intent_submit`` and ``command`` messages.

Issue #37: these run IN THE SIM PROCESS, under the tick's step lock
(taken by SimDispatcher). They are the only writers of sim-owned
tables. The API process never calls them directly -- it goes through
the IPC gateway.

Sim-owned tables (sole writer: sim process):
  souls, soul_inventory, intents, journal, snapshots, escrows, ledger,
  plots, soul_home_plots, marketplace, messages, mailbag, recaps,
  recap_sources, expeditions, resource_nodes, tamer_presence,
  bridge_events, pet_cooldowns, quip_budgets, llm_usage,
  metering_events, metering_config, decision_traces, episodes,
  weekly_digests, semantic_memories, globals.

API-owned tables (sole writer: API process; the sim never touches
them): tamers, tamer_sessions, ws_sessions, ws_tickets, llm_keys,
audit_log, rate_limits, bridge_tokens.

Write-path inventory (every path that wrote sim tables before the
split, and where it lives now):

  REST/WS intent ingress (routers/marketplace.py, routers/social.py,
  routers/plots.py, routers/presence.py, routers/souls.py::feed_soul,
  routers/websockets.py):
    was: kind-specific enqueue_*() writing intents (+ escrow/ledger
      rows) inline in the request handler, then tick.pump_intents().
    now: ``intent_submit`` -> submit_intent() below (same enqueue
      functions, commit-before-ack preserved); the API awaits
      adjudication via ``intent_status`` polls. Adjudication still
      happens at tick boundaries in the sim.

  POST /souls upsert (routers/souls.py):
    was: DELETE+INSERT souls/soul_inventory, newborn mint ledger rows,
      all inline.
    now: ``command souls_upsert``. Secret-rotation ws_sessions cleanup
      stays API-side (API-owned table); the command reports which
      souls rotated.

  POST /souls/{id}/quip (routers/souls.py):
    was: budget reservation + charge + usage rows inline (3 phases
      around LLM generation).
    now: ``command quip_reserve`` / ``quip_release`` / ``quip_charge``.
      Generation stays in the API between reserve and charge.

  POST /plots/{id}/policy (routers/plots.py):
    was: UPDATE plots inline.
    now: ``command plot_set_policy``.

  POST /mailbag/answer (routers/mailbag.py):
    was: mailbag.answer_question() inline (writes mailbag + pulls the
      soul's think forward).
    now: ``command mailbag_answer``.

  POST /metering/pricing, POST /metering/settle (routers/metering.py):
    was: metering.set_pricing() / maybe_run_batch(force=True) inline.
    now: ``command metering_set_pricing`` / ``metering_settle``.

  POST /bridge/events (routers/bridge.py):
    was: bridge.ingest_event() inline (validation, soul resolve,
      durable intent enqueue) plus render_commentary() (quip budget
      reserve/charge/usage + journal rows around LLM generation).
    now: ``command bridge_ingest`` (everything except generation) and
      ``command bridge_commentary_finalize`` (charge/release + journal
      after the API generates). Token create/revoke/list stay
      API-side: bridge_tokens is API-owned (revocation must work even
      when the sim is down).

  Tick loop + adjudication + sweeps (world_tick.py and the modules it
    drives: market, social, plots, biology, metering, consume,
    resources, presence, affection, bridge, expeditions, recap,
    mailbag, agents.pool/memory/deliberation, persistence,
    dormancy, world):
    unchanged -- they already ran in what is now the sim process.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from . import database
from .sim_ipc import SimCommandError

COMMANDS: dict[str, Any] = {}


def command(name: str):  # decorator registering a command implementation
    def _wrap(fn):
        COMMANDS[name] = fn
        return fn

    return _wrap


def submit_intent(
    session_id: str,
    nonce: str,
    custodian_id: str | None,
    soul_id: str,
    kind: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Durable intent enqueue, executed in the sim (commit-before-ack).

    Dispatches ``kind`` to the same kind-specific enqueue functions
    the routers used to call inline, so escrow/debit semantics are
    unchanged. Returns (record, created). Raises the domain
    ``*Refusal`` on business-logic rejection; the dispatcher maps it
    to a ``refusal`` response.
    """
    from . import biology
    from . import intents
    from . import market
    from . import plots
    from . import presence as presence_module
    from . import social as social_module

    if kind in market.MARKET_KINDS:
        return market.enqueue_market_intent(
            session_id, nonce, custodian_id, soul_id, kind, payload
        )
    if kind in social_module.SOCIAL_KINDS:
        return social_module.enqueue_social_intent(
            session_id, nonce, custodian_id, soul_id, kind, payload
        )
    if kind in plots.PLOT_KINDS:
        return plots.enqueue_plot_intent(
            session_id, nonce, custodian_id, soul_id, kind, payload
        )
    if kind in biology.BIOLOGY_KINDS:
        return biology.enqueue_feed_soul(
            session_id, nonce, custodian_id, soul_id, kind, payload
        )
    if kind == presence_module.KIND_TAMER_PRESENCE:
        record = presence_module.enqueue_presence_intent(
            session_id, nonce, custodian_id or "", payload
        )
        return record, False
    return intents.enqueue_intent(
        session_id, nonce, custodian_id, soul_id, kind, payload
    ), True


@command("souls_upsert")
def souls_upsert(params: dict[str, Any], tick: Any) -> dict[str, Any]:
    """Execute the POST /souls write section in the sim.

    params: {"custodian_id": str,
             "entries": [{"soul_id": str, "validated": {...},
                          "fields": {...raw insert fields...},
                          "stored": {...} | None, "is_newborn": bool,
                          "secret": {"hash":..., "prefix":...,
                                     "token_expiry":...} | None,
                          "secret_rotated": bool,
                          "essence": float}],
             "delete_inventory_ids": [...], "delete_soul_ids": [...],
             "stale_ids": [...]}
    """
    from . import dormancy
    from . import persistence

    custodian_id = params["custodian_id"]
    entries = params["entries"]
    tick_id = tick.tick_id
    secret_rotated: list[str] = []
    saved_ids: list[str] = []
    with database.get_db() as conn:
        cursor = conn.cursor()
        if params.get("delete_inventory_ids"):
            placeholders = ",".join("?" for _ in params["delete_inventory_ids"])
            cursor.execute(
                f"DELETE FROM soul_inventory WHERE soul_id IN ({placeholders})",
                params["delete_inventory_ids"],
            )
        if params.get("delete_soul_ids"):
            placeholders = ",".join("?" for _ in params["delete_soul_ids"])
            cursor.execute(
                "DELETE FROM souls WHERE COALESCE(custodian_id, owner_id) = ? "
                f"AND soul_id IN ({placeholders})",
                [custodian_id, *params["delete_soul_ids"]],
            )
        if params.get("stale_ids"):
            placeholders = ",".join("?" for _ in params["stale_ids"])
            cursor.execute(
                f"DELETE FROM soul_inventory WHERE soul_id IN ({placeholders})",
                params["stale_ids"],
            )
            cursor.execute(
                "DELETE FROM souls WHERE COALESCE(custodian_id, owner_id) = ? "
                f"AND soul_id IN ({placeholders})",
                [custodian_id, *params["stale_ids"]],
            )
        for entry in entries:
            soul_id = entry["soul_id"]
            validated = entry["validated"]
            fields = entry["fields"]
            secret = entry.get("secret")
            if entry.get("secret_rotated"):
                secret_rotated.append(soul_id)
            cursor.execute(
                """
                INSERT OR REPLACE INTO souls (
                    soul_id, owner_id, custodian_id, name, first_name,
                    family_name, species, gender, level, xp, mother_id,
                    father_id, hp, max_hp, satiety, hydration, essence,
                    position, velocity, hometown, birth_date, activity,
                    orb_color, aura_color, aura_visible,
                    stat_hp_base, stat_atk_base, stat_def_base,
                    stat_spa_base, stat_spd_base, stat_spe_base,
                    stat_vis_base, stat_hp_iv, stat_atk_iv, stat_def_iv,
                    stat_spa_iv, stat_spd_iv, stat_spe_iv, stat_vis_iv,
                    stat_hp_ev, stat_atk_ev, stat_def_ev, stat_spa_ev,
                    stat_spd_ev, stat_spe_ev, stat_vis_ev,
                    nature, secret_hash, secret_prefix, updated_at,
                    token_expiry, is_revoked
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    soul_id,
                    custodian_id,
                    custodian_id,
                    fields.get("name"),
                    fields.get("first_name"),
                    fields.get("family_name"),
                    fields.get("species"),
                    fields.get("gender"),
                    validated["level"],
                    validated["xp"],
                    fields.get("mother_id"),
                    fields.get("father_id"),
                    validated["hp"],
                    validated["max_hp"],
                    validated["satiety"],
                    validated["hydration"],
                    entry["essence"],
                    json.dumps(validated["position"]),
                    json.dumps(validated["velocity"]),
                    json.dumps(fields.get("hometown")),
                    fields.get("birth_date"),
                    fields.get("activity"),
                    json.dumps(fields.get("orb_color", [1, 1, 1])),
                    json.dumps(fields.get("aura_color", [1, 1, 1])),
                    1 if fields.get("aura_visible") else 0,
                    validated["stat_hp_base"],
                    validated["stat_atk_base"],
                    validated["stat_def_base"],
                    validated["stat_spa_base"],
                    validated["stat_spd_base"],
                    validated["stat_spe_base"],
                    validated["stat_vis_base"],
                    validated["stat_hp_iv"],
                    validated["stat_atk_iv"],
                    validated["stat_def_iv"],
                    validated["stat_spa_iv"],
                    validated["stat_spd_iv"],
                    validated["stat_spe_iv"],
                    validated["stat_vis_iv"],
                    validated["stat_hp_ev"],
                    validated["stat_atk_ev"],
                    validated["stat_def_ev"],
                    validated["stat_spa_ev"],
                    validated["stat_spd_ev"],
                    validated["stat_spe_ev"],
                    validated["stat_vis_ev"],
                    fields.get("nature", "Hardy"),
                    secret["hash"] if secret else None,
                    secret["prefix"] if secret else None,
                    entry["now"],
                    secret["token_expiry"] if secret else None,
                    0,
                ),
            )
            for item in entry.get("inventory", []):
                cursor.execute(
                    "INSERT INTO soul_inventory (soul_id, item_name, quantity)"
                    " VALUES (?, ?, ?)",
                    (soul_id, item["name"], item["quantity"]),
                )
            saved_ids.append(soul_id)
        for soul_id in params.get("newborn_ids", []):
            dormancy.mint_starter_grant(
                conn, tick_id, database.ACTOR_SOUL, soul_id
            )
        conn.commit()
    for soul_id in saved_ids:
        persistence.invalidate(soul_id)
    return {
        "saved": len(saved_ids),
        "saved_ids": saved_ids,
        "secret_rotated": secret_rotated,
    }


@command("quip_reserve")
def quip_reserve(params: dict[str, Any], tick: Any) -> dict[str, Any]:
    """Phase 1 of POST /souls/{id}/quip: reserve a budget slot."""
    from datetime import datetime, time as dtime, timedelta, timezone

    from . import quips

    soul_id = params["soul_id"]
    day = params["day"]
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if quips.quips_remaining(conn, soul_id) <= 0:
                conn.rollback()
                resets_at = datetime.combine(
                    datetime.now(timezone.utc).date() + timedelta(days=1),
                    dtime.min,
                    tzinfo=timezone.utc,
                ).isoformat()
                return {
                    "reserved": False,
                    "reason": "budget_exhausted",
                    "resets_at": resets_at,
                }
            balance = conn.execute(
                "SELECT COALESCE(essence, 0.0) FROM souls WHERE soul_id = ?",
                (soul_id,),
            ).fetchone()[0]
            if float(balance) < quips.QUIP_PRICE_ESSENCE:
                conn.rollback()
                return {
                    "reserved": False,
                    "reason": "insufficient_essence",
                    "price": quips.QUIP_PRICE_ESSENCE,
                    "essence": float(balance),
                }
            quips.record_quip(conn, soul_id, day)
            used = quips.get_quip_count(conn, soul_id, day)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"reserved": True, "quips_used_today": used}


@command("quip_release")
def quip_release(params: dict[str, Any], tick: Any) -> dict[str, Any]:
    """Release a reserved quip budget slot (generation/charge failed)."""
    from . import quips

    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            quips.release_quip(conn, params["soul_id"], params["day"])
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return {"released": True}


@command("quip_charge")
def quip_charge(params: dict[str, Any], tick: Any) -> dict[str, Any]:
    """Phase 3 of POST /souls/{id}/quip: debit + usage row."""
    from fastapi import HTTPException

    from . import quips

    soul_id = params["soul_id"]
    gen = params["gen"]
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            essence_left = quips.charge_for_quip(conn, soul_id)
            quips.write_usage_row(conn, soul_id, gen)
            conn.commit()
        except HTTPException as exc:
            conn.rollback()
            if "Insufficient essence" in str(exc.detail):
                return {"charged": False, "reason": "insufficient_essence"}
            raise
        except Exception:
            conn.rollback()
            raise
    return {"charged": True, "essence_left": essence_left}


@command("plot_set_policy")
def plot_set_policy(params: dict[str, Any], tick: Any) -> dict[str, Any]:
    """POST /plots/{id}/policy: the UPDATE plots write."""
    from . import persistence

    with database.get_db() as conn:
        conn.execute(
            "UPDATE plots SET access_policy = ? WHERE plot_id = ?",
            (params["access_policy"], params["plot_id"]),
        )
        # Issue #38: operator interventions journal as typed events so
        # dispute replays can show "why did X happen".
        persistence.append_operator_event(
            conn,
            tick.tick_id,
            operator_id=params.get("operator_id") or "operator",
            action="plot_policy",
            target_type="plot",
            target_id=params["plot_id"],
            details=f"access_policy={params['access_policy']}",
        )
        conn.commit()
    return {
        "status": "success",
        "plot_id": params["plot_id"],
        "access_policy": params["access_policy"],
    }


@command("mailbag_answer")
def mailbag_answer(params: dict[str, Any], tick: Any) -> dict[str, Any]:
    """POST /mailbag/answer: record the tamer's answer in the sim."""
    from . import mailbag

    return mailbag.answer_question(
        params["question_id"],
        params["soul_id"],
        params["answer"],
        params["now"],
        tick.tick_id,
    )


@command("metering_set_pricing")
def metering_set_pricing(params: dict[str, Any], tick: Any) -> dict[str, Any]:
    """POST /metering/pricing: operator pricing update."""
    from . import persistence
    from .agents import metering

    try:
        with database.get_db() as conn:
            pricing = metering.set_pricing(
                conn,
                essence_per_usd=params.get("essence_per_usd"),
                model_rates=params.get("model_rates"),
            )
            # Issue #38: typed operator journal event (see
            # plot_set_policy).
            persistence.append_operator_event(
                conn,
                tick.tick_id,
                operator_id=params.get("operator_id") or "operator",
                action="pricing_update",
                target_type="metering",
                target_id="pricing",
                details=(
                    f"essence_per_usd={params.get('essence_per_usd')} "
                    f"model_rates={'set' if params.get('model_rates') else 'unchanged'}"
                ),
            )
            conn.commit()
    except ValueError as exc:
        raise SimCommandError("bad_request", str(exc)) from exc
    return pricing


@command("metering_settle")
def metering_settle(params: dict[str, Any], tick: Any) -> dict[str, Any]:
    """POST /metering/settle: force a metering batch (operator)."""
    from . import persistence
    from .agents import metering

    report = metering.maybe_run_batch(force=True)
    # Issue #38: typed operator journal event (see plot_set_policy).
    with database.get_db() as conn:
        persistence.append_operator_event(
            conn,
            tick.tick_id,
            operator_id=params.get("operator_id") or "operator",
            action="metering_settle",
            target_type="metering",
            target_id=str(report.get("batch_id") or ""),
            details=f"created={report.get('created')}",
        )
        conn.commit()
    return report


@command("bridge_ingest")
def bridge_ingest(params: dict[str, Any], tick: Any) -> dict[str, Any]:
    """POST /bridge/events: validate, resolve soul, durable enqueue.

    Everything except the LLM commentary generation, which the API
    performs after this returns and finalizes via
    ``bridge_commentary_finalize``. Raises BridgeRefusal /
    InjectionRejected as command errors (reason/detail preserved).
    """
    from . import bridge
    from . import intents

    def _injection_error(exc: Any) -> SimCommandError:
        return SimCommandError(
            "injection_rejected",
            json.dumps(
                {
                    "pattern_class": exc.pattern_class,
                    "pattern": exc.pattern,
                }
            ),
        )

    tamer_id = params["tamer_id"]
    try:
        source_id = bridge._validate_source_id(params["source_id"])
        kind = params["kind"]
        if kind not in bridge.BRIDGE_KINDS:
            raise bridge.BridgeRefusal(
                "unknown_kind",
                f"Unknown bridge kind: {kind!r}. "
                f"Allowed: {', '.join(bridge.BRIDGE_KINDS)}",
            )
        try:
            summary = bridge.scrub_text(params.get("summary") or "", "summary")
        except bridge.InjectionRejected as exc:
            raise _injection_error(exc) from exc
        if not summary:
            raise bridge.BridgeRefusal("empty_summary", "summary must not be empty")
        ref = params.get("ref")
        if ref is not None:
            try:
                ref = bridge.scrub_text(ref, "ref")
            except bridge.InjectionRejected as exc:
                raise _injection_error(exc) from exc
            if len(ref) > bridge.MAX_REF_LEN:
                raise bridge.BridgeRefusal(
                    "ref_too_long",
                    f"ref must be at most {bridge.MAX_REF_LEN} characters",
                )
            ref = ref or None
    except bridge.BridgeRefusal as refusal:
        raise SimCommandError(refusal.reason, refusal.detail)
    soul = bridge.resolve_soul(tamer_id)
    if soul is None:
        raise SimCommandError(
            "no_soul", "Tamer has no soul to route bridge events through"
        )
    now = params.get("now") or time.time()
    event_id = "bev_" + secrets.token_urlsafe(16)
    wrapped = bridge.wrap_untrusted(summary)
    pivotal = kind in bridge.PIVOTAL_KINDS
    commentary_requested = bool(params.get("commentary")) and pivotal
    payload = {
        "event_id": event_id,
        "tamer_id": tamer_id,
        "soul_id": soul["soul_id"],
        "source_id": source_id,
        "kind": kind,
        "summary": wrapped,
        "ref": ref,
        "commentary_requested": commentary_requested,
    }
    intents.enqueue_intent(
        session_id=f"bridge:{tamer_id}:{source_id}",
        nonce=event_id,
        custodian_id=tamer_id,
        soul_id=soul["soul_id"],
        kind=bridge.BRIDGE_INTENT_KIND,
        payload=payload,
    )
    commentary: dict[str, Any] | None = None
    if commentary_requested:
        commentary = _bridge_commentary_reserve(soul, payload, now, tick.tick_id)
    return {
        "status": "accepted",
        "event_id": event_id,
        "soul_id": soul["soul_id"],
        "kind": kind,
        "pivotal": pivotal,
        "commentary": commentary,
        "commentary_soul": soul,
        "commentary_event": {
            "event_id": event_id,
            "kind": kind,
            "source_id": source_id,
        },
    }


def _bridge_commentary_reserve(
    soul: dict, event: dict, now: float, tick_id: int
) -> dict[str, Any]:
    """Quip-budget reservation for bridge commentary (was render_commentary
    phase 1). Journals skip outcomes; returns a reservation the API can
    generate against, or a skip descriptor."""
    from . import bridge
    from . import quips

    soul_id = soul["soul_id"]
    day = quips.quip_day()
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if quips.quips_remaining(conn, soul_id) <= 0:
                conn.rollback()
                bridge._journal_commentary(
                    conn, soul_id, event, "skipped", "budget_exhausted"
                )
                conn.commit()
                return {"status": "skipped", "reason": "budget_exhausted"}
            balance = conn.execute(
                "SELECT COALESCE(essence, 0.0) FROM souls WHERE soul_id = ?",
                (soul_id,),
            ).fetchone()[0]
            if float(balance) < quips.QUIP_PRICE_ESSENCE:
                conn.rollback()
                bridge._journal_commentary(
                    conn, soul_id, event, "skipped", "insufficient_essence"
                )
                conn.commit()
                return {"status": "skipped", "reason": "insufficient_essence"}
            quips.record_quip(conn, soul_id, day)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    prompt = (
        f"Your tamer's {event['source_id']} tool reported "
        f"'{event['kind']}': {bridge.unwrap_untrusted(event['summary'])}. "
        "React in one short line, in character."
    )
    return {"status": "reserved", "day": day, "prompt": prompt}


@command("bridge_commentary_finalize")
def bridge_commentary_finalize(params: dict[str, Any], tick: Any) -> dict[str, Any]:
    """Finalize bridge commentary after API-side generation.

    params: {"soul_id", "event": {...}, "gen": {...} | None,
             "failed": bool}
    gen=None (or failed=True) releases the reservation and journals a
    skip; otherwise charges the quip price, writes the usage row, and
    journals the rendered commentary.
    """
    from . import bridge
    from . import quips

    soul_id = params["soul_id"]
    event = params["event"]
    gen = params.get("gen")
    day = params.get("day") or quips.quip_day()
    if not gen or params.get("failed"):
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                quips.release_quip(conn, soul_id, day)
                conn.commit()
            except Exception:
                conn.rollback()
        bridge._journal_commentary(None, soul_id, event, "skipped", "generation_failed")
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
        bridge._journal_commentary(
            None, soul_id, event, "skipped", "insufficient_essence"
        )
        return {"status": "skipped", "reason": "insufficient_essence"}
    bridge._journal_commentary(
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
