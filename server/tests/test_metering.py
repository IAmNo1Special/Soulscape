"""Metering stage-1: usage events, decision traces, batched debits (issue #27).

Acceptance coverage:
- usage-to-debit pipeline idempotent under replay (events, debits,
  crash-style re-runs)
- dispute API walks a sampled spend end to end
- empty-wallet debit flips dormancy with journaled reason
- pricing knob: new settlements only
- full tick-pump dispatch of metering_debit intents
"""

import asyncio
import json
import time

import pytest

from .. import database, dormancy, intents
from ..agents import deliberation, metering, pool
from ..agents.deliberation import Deliberator, EscalationTracker
from ..agents import reflex
from ..market import verify_balances


def _insert_soul(db_conn, soul_id, essence=100.0, **kw):
    cols = {
        "soul_id": soul_id,
        "owner_id": f"owner_{soul_id}",
        "custodian_id": f"owner_{soul_id}",
        "position": json.dumps([100.0, 100.0]),
        "velocity": json.dumps([0.0, 0.0]),
        "essence": essence,
        "satiety": 100.0,
        "hydration": 100.0,
        "hp": 100.0,
        "max_hp": 100.0,
        "state": "normal",
        "nature": "Hardy",
        "name": "TestSoul",
        "species": "wisp",
    }
    cols.update(kw)
    db_conn.execute(
        f"INSERT INTO souls ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        tuple(cols.values()),
    )
    db_conn.commit()


def _seed_usage(
    db_conn,
    soul_id,
    n=1,
    model="gemini-2.5-flash",
    prompt_tokens=800,
    completion_tokens=200,
):
    usage_ids = []
    cost = deliberation.estimate_cost(model, prompt_tokens, completion_tokens)
    for _ in range(n):
        usage_ids.append(
            deliberation.record_usage(
                soul_id,
                "flash",
                "google",
                model,
                prompt_tokens,
                completion_tokens,
                cost,
                12.5,
                False,
            )
        )
    return usage_ids


class _Tick:
    tick_id = 7


def _settle_pending():
    for intent in intents.pending_intents():
        if intent["kind"] == metering.KIND_METERING_DEBIT:
            metering.adjudicate_metering_intent(_Tick(), intent)


def _essence(db_conn, soul_id):
    return float(
        db_conn.execute(
            "SELECT essence FROM souls WHERE soul_id = ?", (soul_id,)
        ).fetchone()["essence"]
    )


def _fund(db_conn):
    return float(
        db_conn.execute(
            "SELECT value FROM globals WHERE key = 'essence_fund'"
        ).fetchone()["value"]
    )


def _journal_reasons(db_conn, event_type):
    return [
        json.loads(row["payload"])
        for row in db_conn.execute(
            "SELECT payload FROM journal WHERE type = ?", (event_type,)
        ).fetchall()
    ]


# ---------------------------------------------------------------- ingest


def test_ingest_idempotent_under_replay(db_conn):
    _insert_soul(db_conn, "s1")
    ids = _seed_usage(db_conn, "s1", n=3)
    assert metering.ingest_usage_records(db_conn) == 3
    assert metering.ingest_usage_records(db_conn) == 0
    db_conn.commit()
    for usage_id in ids:
        assert metering.record_event_for_usage(usage_id, db_conn) is not None
    db_conn.commit()
    events = db_conn.execute(
        "SELECT event_id FROM metering_events WHERE soul_id = 's1'"
    ).fetchall()
    assert sorted(r["event_id"] for r in events) == [
        f"llm_usage:{i}" for i in sorted(ids)
    ]
    assert db_conn.execute(
        "SELECT COUNT(*) AS n FROM metering_events"
    ).fetchone()["n"] == 3


def test_event_fields_match_usage_row(db_conn):
    _insert_soul(db_conn, "s1")
    (usage_id,) = _seed_usage(
        db_conn, "s1", prompt_tokens=800, completion_tokens=200
    )
    metering.record_event_for_usage(usage_id, db_conn)
    event = db_conn.execute(
        "SELECT * FROM metering_events WHERE event_id = ?",
        (f"llm_usage:{usage_id}",),
    ).fetchone()
    assert event["soul_id"] == "s1"
    assert event["tier"] == "flash"
    assert event["model"] == "gemini-2.5-flash"
    assert event["prompt_tokens"] == 800
    assert event["completion_tokens"] == 200
    assert event["cost_usd_estimate"] == pytest.approx(0.00074)
    assert event["essence_charged"] is None
    assert event["settled_at"] is None


# ------------------------------------------------------- traces at write


class _StubVision:
    def detail_observations(self, soul_id):
        return []


def _soul_row(db_conn, soul_id):
    return dict(
        db_conn.execute(
            "SELECT * FROM souls WHERE soul_id = ?", (soul_id,)
        ).fetchone()
    )


def _fake_ok(provider, model, prompt, timeout_s, key):
    return json.dumps(
        {
            "intents": [
                {"action": "move_to", "params": {"x": 120.0, "y": 100.0}}
            ],
            "rationale": "Exploring east for resources.",
        }
    )


def test_deliberated_path_writes_event_and_trace(db_conn):
    from .. import key_vault

    _insert_soul(db_conn, "s1")
    nonce, ciphertext = key_vault.encrypt_key("fake-google-key-value")
    key_vault.store_key(
        "owner_s1", key_vault.new_key_id(), "google", "test", "1234",
        nonce, ciphertext,
    )
    p = pool.AgentPool()
    d = Deliberator(
        p, tracker=EscalationTracker(), call_provider=_fake_ok
    )
    result = d.deliberate(
        "s1", _soul_row(db_conn, "s1"), _StubVision(),
        reflex.NullProvider(), 1000.0, "reflex_rate", tick_id=1,
    )
    assert result["status"] == "deliberated"
    trace_id = result["trace_id"]
    trace = db_conn.execute(
        "SELECT * FROM decision_traces WHERE trace_id = ?", (trace_id,)
    ).fetchone()
    assert trace is not None
    assert trace["soul_id"] == "s1"
    assert trace["status"] == "deliberated"
    assert trace["rationale"] == "Exploring east for resources."
    intents_out = json.loads(trace["intents_json"])
    assert intents_out == [
        {"action": "move_to", "params": {"x": 120.0, "y": 100.0}}
    ]
    event = db_conn.execute(
        "SELECT * FROM metering_events WHERE event_id = ?",
        (trace["usage_event_id"],),
    ).fetchone()
    assert event is not None and event["soul_id"] == "s1"
    assert trace["deliberation_id"] is not None


def test_heuristic_path_writes_event_and_trace(db_conn):
    _insert_soul(db_conn, "s1")
    p = pool.AgentPool()
    d = Deliberator(
        p,
        tracker=EscalationTracker(),
        call_provider=lambda *a: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )
    result = d.deliberate(
        "s1", _soul_row(db_conn, "s1"), _StubVision(),
        reflex.NullProvider(), 1000.0, "reflex_rate", tick_id=1,
    )
    assert result["status"] == "heuristic"
    trace = db_conn.execute(
        "SELECT * FROM decision_traces WHERE trace_id = ?",
        (result["trace_id"],),
    ).fetchone()
    assert trace["status"] == "heuristic"
    assert json.loads(trace["intents_json"]) == []
    event = db_conn.execute(
        "SELECT * FROM metering_events WHERE event_id = ?",
        (trace["usage_event_id"],),
    ).fetchone()
    assert event["cost_usd_estimate"] == 0.0


def test_attach_intent_ids_merges_idempotently(db_conn):
    _insert_soul(db_conn, "s1")
    (usage_id,) = _seed_usage(db_conn, "s1")
    metering.record_event_for_usage(usage_id, db_conn)
    metering.record_decision_trace(
        db_conn,
        trace_id="trace:x",
        soul_id="s1",
        deliberation_id=1,
        usage_event_id=f"llm_usage:{usage_id}",
        status="deliberated",
        rationale="r",
        intents=[],
    )
    db_conn.commit()
    metering.attach_intent_ids("trace:x", ["int_a", "int_b"])
    metering.attach_intent_ids("trace:x", ["int_b", "int_c"])
    row = db_conn.execute(
        "SELECT intent_ids_json FROM decision_traces WHERE trace_id = 'trace:x'"
    ).fetchone()
    assert json.loads(row["intent_ids_json"]) == ["int_a", "int_b", "int_c"]


def test_pool_deliberate_attaches_intent_ids(db_conn):
    from .. import key_vault

    _insert_soul(db_conn, "s1")
    nonce, ciphertext = key_vault.encrypt_key("fake-google-key-value")
    key_vault.store_key(
        "owner_s1", key_vault.new_key_id(), "google", "test", "1234",
        nonce, ciphertext,
    )
    p = pool.AgentPool()
    d = Deliberator(
        p, tracker=EscalationTracker(), call_provider=_fake_ok
    )
    result = asyncio.run(
        p.deliberate(
            "s1", _StubVision(), reflex.NullProvider(), 1, 1000.0,
            "reflex_rate", deliberator=d,
        )
    )
    assert result["status"] == "deliberated"
    assert result["enqueued"], "move_to should have enqueued"
    trace = db_conn.execute(
        "SELECT intent_ids_json FROM decision_traces WHERE trace_id = ?",
        (result["trace_id"],),
    ).fetchone()
    assert json.loads(trace["intent_ids_json"]) == result["enqueued"]


# ------------------------------------------------------------- settlement


def test_batch_settle_idempotent_and_balances_exact(db_conn):
    _insert_soul(db_conn, "s1", essence=100.0)
    _seed_usage(db_conn, "s1", n=2)
    with database.get_db() as conn:
        # True baselines before any metering ledger rows exist.
        assert verify_balances(conn)["ok"]
    first = metering.maybe_run_batch(force=True)
    assert first["created"] == 1
    # Batcher re-run before settlement: no new intents (events claimed).
    second = metering.maybe_run_batch(force=True)
    assert second["created"] == 0
    pricing = metering.get_pricing(db_conn)
    db_conn.commit()
    due = 2 * metering.compute_essence(
        pricing, "gemini-2.5-flash", 800, 200
    )
    assert due == pytest.approx(0.148)
    _settle_pending()
    # Re-run settlement after "restart": no-op.
    _settle_pending()
    assert _essence(db_conn, "s1") == pytest.approx(100.0 - due)
    assert _fund(db_conn) == pytest.approx(due)
    events = db_conn.execute(
        "SELECT essence_charged, settled_at, settled_by, shortfall_essence "
        "FROM metering_events WHERE soul_id = 's1'"
    ).fetchall()
    assert len(events) == 2
    for event in events:
        assert event["essence_charged"] == pytest.approx(due / 2)
        assert event["settled_at"] is not None
        assert event["settled_by"] is not None
        assert event["shortfall_essence"] == 0.0
    # Exactly one debit row per event, all under the batch intent family.
    debits = db_conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0.0) AS total FROM ledger "
        "WHERE entry_type = 'debit' AND soul_id = 's1'"
    ).fetchone()
    assert debits["n"] == 2
    assert debits["total"] == pytest.approx(due)
    taxes = db_conn.execute(
        "SELECT COALESCE(SUM(amount), 0.0) AS total FROM ledger "
        "WHERE entry_type = 'tax'"
    ).fetchone()["total"]
    assert taxes == pytest.approx(due)
    with database.get_db() as conn:
        report = verify_balances(conn)
    assert report["ok"], report["soul_drifts"]


def test_tick_pump_dispatches_metering_intents(db_conn):
    from ..world_tick import WorldTick

    _insert_soul(db_conn, "s1", essence=50.0)
    _seed_usage(db_conn, "s1", n=1)
    report = metering.maybe_run_batch(force=True)
    assert report["created"] == 1
    tick = WorldTick()
    tick.tick_id = 3
    settled = tick.pump_intents()
    assert settled >= 1
    event = db_conn.execute(
        "SELECT settled_at FROM metering_events WHERE soul_id = 's1'"
    ).fetchone()
    assert event["settled_at"] is not None
    assert _essence(db_conn, "s1") < 50.0


def test_cadence_guard_skips_without_force(db_conn):
    _insert_soul(db_conn, "s1")
    _seed_usage(db_conn, "s1", n=1)
    assert metering.maybe_run_batch(force=True)["created"] == 1
    _settle_pending()
    _seed_usage(db_conn, "s1", n=1)
    # Last batch just ran: a non-forced run skips on cadence...
    skipped = metering.maybe_run_batch()
    assert skipped["created"] == 0
    assert skipped["reason"] == "cadence"
    # ...but an explicit force still batches.
    assert metering.maybe_run_batch(force=True)["created"] == 1


def test_threshold_triggers_batch_inside_cadence(db_conn):
    _insert_soul(db_conn, "s1")
    _seed_usage(db_conn, "s1", n=100)
    report = metering.maybe_run_batch(now=time.time())
    assert report["created"] == 1


# ------------------------------------------------------- dispute API chain


def test_dispute_line_item_walks_end_to_end(db_conn):
    _insert_soul(db_conn, "s1", essence=100.0)
    (usage_id,) = _seed_usage(db_conn, "s1")
    metering.record_event_for_usage(usage_id, db_conn)
    metering.record_decision_trace(
        db_conn,
        trace_id=f"trace:{usage_id}",
        soul_id="s1",
        deliberation_id=42,
        usage_event_id=f"llm_usage:{usage_id}",
        status="deliberated",
        rationale="Exploring east for resources.",
        intents=[{"action": "move_to", "params": {"x": 1.0, "y": 2.0}}],
    )
    db_conn.commit()
    metering.attach_intent_ids(f"trace:{usage_id}", ["int_probe"])
    db_conn.execute(
        "INSERT INTO intents (intent_id, session_id, nonce, custodian_id, "
        "soul_id, kind, payload, status, created_at, result) "
        "VALUES ('int_probe', 'probe', 'n1', NULL, 's1', 'move_to', '{}', "
        "'adjudicated', ?, ?)",
        (time.time(), json.dumps({"velocity": [1.0, 0.0]})),
    )
    db_conn.commit()
    metering.maybe_run_batch(force=True)
    _settle_pending()
    lines = metering.soul_line_items(db_conn, "s1")
    assert len(lines) == 1
    line = lines[0]
    assert line["model"] == "gemini-2.5-flash"
    assert line["prompt_tokens"] == 800
    assert line["completion_tokens"] == 200
    assert line["cost_usd_estimate"] == pytest.approx(0.00074)
    assert line["essence_charged"] == pytest.approx(0.074)
    trace = line["trace"]
    assert trace["trace_id"] == f"trace:{usage_id}"
    assert trace["deliberation_id"] == 42
    assert trace["rationale"] == "Exploring east for resources."
    assert trace["intents"] == [
        {"action": "move_to", "params": {"x": 1.0, "y": 2.0}}
    ]
    assert trace["intent_ids"] == ["int_probe"]
    assert trace["outcomes"]["int_probe"]["status"] == "adjudicated"
    assert trace["outcomes"]["int_probe"]["result"] == {
        "velocity": [1.0, 0.0]
    }
    kinds = {r["entry_type"] for r in line["ledger_rows"]}
    assert kinds == {"debit", "tax"}
    debit = next(r for r in line["ledger_rows"] if r["entry_type"] == "debit")
    assert debit["soul_id"] == "s1"
    assert debit["amount"] == pytest.approx(line["essence_charged"])
    assert debit["intent_id"] == line["debit_intent_id"]
    summary = metering.soul_summary(db_conn, "s1")
    db_conn.commit()
    assert summary["calls"] == 1
    assert summary["essence_charged"] == pytest.approx(0.074)
    assert summary["unsettled_events"] == 0
    assert summary["unsettled_accrued_essence"] == 0.0


# ------------------------------------------------- unpayable -> dormancy


def test_unpayable_debit_drains_and_freezes_with_reason(db_conn):
    _insert_soul(db_conn, "s1", essence=0.05)
    _seed_usage(db_conn, "s1", n=1)
    with database.get_db() as conn:
        # Capture true baselines BEFORE any metering ledger rows exist;
        # a later verify must still agree with the caches.
        assert verify_balances(conn)["ok"]
    metering.maybe_run_batch(force=True)
    _settle_pending()
    due = metering.compute_essence(
        metering.get_pricing(db_conn), "gemini-2.5-flash", 800, 200
    )
    db_conn.commit()
    assert due == pytest.approx(0.074)
    assert _essence(db_conn, "s1") == pytest.approx(0.0)
    assert _fund(db_conn) == pytest.approx(0.05)
    event = db_conn.execute(
        "SELECT shortfall_essence, essence_charged FROM metering_events "
        "WHERE soul_id = 's1'"
    ).fetchone()
    assert event["shortfall_essence"] == pytest.approx(due - 0.05)
    assert event["essence_charged"] == pytest.approx(0.05)
    assert dormancy.soul_is_dormant(db_conn, "s1")
    dormant_journal = _journal_reasons(db_conn, dormancy.EVENT_SOUL_DORMANT)
    assert len(dormant_journal) == 1
    payload = dormant_journal[0]
    assert payload["reason"] == metering.REASON_UNPAYABLE_LLM_DEBIT
    assert payload["shortfall"] == pytest.approx(due - 0.05)
    assert payload["essence_after"] == pytest.approx(0.0)
    with database.get_db() as conn:
        report = verify_balances(conn)
    assert report["ok"], report["soul_drifts"]


def test_partial_payment_allocates_across_events_in_order(db_conn):
    # Two events (0.074 each); the wallet covers the first fully and
    # only part of the second. Ledger rows must move exactly what was
    # taken -- no drift -- and each event records its own shortfall.
    _insert_soul(db_conn, "s1", essence=0.10)
    _seed_usage(db_conn, "s1", n=2)
    with database.get_db() as conn:
        assert verify_balances(conn)["ok"]
    metering.maybe_run_batch(force=True)
    _settle_pending()
    assert _essence(db_conn, "s1") == pytest.approx(0.0)
    assert _fund(db_conn) == pytest.approx(0.10)
    events = db_conn.execute(
        "SELECT essence_charged, shortfall_essence FROM metering_events "
        "WHERE soul_id = 's1' ORDER BY created_at, event_id"
    ).fetchall()
    assert len(events) == 2
    assert events[0]["essence_charged"] == pytest.approx(0.074)
    assert events[0]["shortfall_essence"] == pytest.approx(0.0)
    assert events[1]["essence_charged"] == pytest.approx(0.026)
    assert events[1]["shortfall_essence"] == pytest.approx(0.048)
    debit_total = db_conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM ledger "
        "WHERE entry_type = 'debit' AND soul_id = 's1'"
    ).fetchone()[0]
    tax_total = db_conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM ledger "
        "WHERE entry_type = 'tax'"
    ).fetchone()[0]
    assert debit_total == pytest.approx(0.10)
    assert tax_total == pytest.approx(0.10)
    with database.get_db() as conn:
        report = verify_balances(conn)
    assert report["ok"], report["soul_drifts"]


def test_no_debt_carried_after_unpayable(db_conn):
    _insert_soul(db_conn, "s1", essence=0.0)
    _seed_usage(db_conn, "s1", n=1)
    metering.maybe_run_batch(force=True)
    _settle_pending()
    assert _essence(db_conn, "s1") == pytest.approx(0.0)
    summary = metering.soul_summary(db_conn, "s1")
    db_conn.commit()
    assert summary["unsettled_events"] == 0
    assert summary["dormant"] is True


# ------------------------------------------------------------ pricing knob


def test_pricing_knob_affects_new_settlements_only(db_conn):
    _insert_soul(db_conn, "s1", essence=100.0)
    (old_id,) = _seed_usage(db_conn, "s1")
    metering.record_event_for_usage(old_id, db_conn)
    pricing = metering.get_pricing(db_conn)
    db_conn.commit()
    assert pricing["essence_per_usd"] == 100.0
    assert "gemini-2.5-flash" in pricing["model_rates"]
    metering.maybe_run_batch(force=True)
    _settle_pending()
    old_charge = db_conn.execute(
        "SELECT essence_charged FROM metering_events WHERE event_id = ?",
        (f"llm_usage:{old_id}",),
    ).fetchone()["essence_charged"]
    assert old_charge == pytest.approx(0.074)
    with database.get_db() as conn:
        metering.set_pricing(conn, essence_per_usd=200.0)
        conn.commit()
    (new_id,) = _seed_usage(db_conn, "s1")
    metering.record_event_for_usage(new_id, db_conn)
    db_conn.commit()
    summary = metering.soul_summary(db_conn, "s1")
    db_conn.commit()
    assert summary["unsettled_accrued_essence"] == pytest.approx(0.148)
    metering.maybe_run_batch(force=True)
    _settle_pending()
    new_charge = db_conn.execute(
        "SELECT essence_charged FROM metering_events WHERE event_id = ?",
        (f"llm_usage:{new_id}",),
    ).fetchone()["essence_charged"]
    assert new_charge == pytest.approx(0.148)
    kept = db_conn.execute(
        "SELECT essence_charged FROM metering_events WHERE event_id = ?",
        (f"llm_usage:{old_id}",),
    ).fetchone()["essence_charged"]
    assert kept == pytest.approx(old_charge)
    with pytest.raises(ValueError):
        with database.get_db() as conn:
            metering.set_pricing(conn, essence_per_usd=-5.0)


def test_unknown_model_prices_zero(db_conn):
    pricing = metering.get_pricing(db_conn)
    assert (
        metering.compute_essence(pricing, "nope-9000", 800, 200) == 0.0
    )


# ------------------------------------------------------------- HTTP layer


def test_metering_http_summary_and_line_items(client, db_conn):
    _insert_soul(db_conn, "http1", essence=100.0)
    _seed_usage(db_conn, "http1", n=1)
    metering.maybe_run_batch(force=True)
    _settle_pending()
    resp = client.get("/metering/souls/http1/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["soul_id"] == "http1"
    assert body["calls"] == 1
    assert body["essence_charged"] == pytest.approx(0.074)
    resp = client.get("/metering/souls/http1/line-items")
    assert resp.status_code == 200
    lines = resp.json()
    assert len(lines) == 1
    assert lines[0]["essence_charged"] == pytest.approx(0.074)
    assert len(lines[0]["ledger_rows"]) == 2
    assert client.get("/metering/souls/nope/summary").status_code == 404


def test_metering_http_cross_custody_denied(client, db_conn):
    _insert_soul(db_conn, "http2", essence=100.0)
    _seed_usage(db_conn, "http2", n=1)
    r = client.post(
        "/tamers/register",
        json={"username": "intruder", "password": "s3cur3pass"},
    )
    assert r.status_code in (200, 201)
    r = client.post(
        "/tamers/login",
        json={"username": "intruder", "password": "s3cur3pass"},
    )
    assert r.status_code == 200
    token = r.json()["token"]
    resp = client.get(
        "/metering/souls/http2/summary",
        headers={"X-Hub-Secret": token},
    )
    assert resp.status_code == 403
    resp = client.get(
        "/metering/souls/http2/line-items",
        headers={"X-Hub-Secret": token},
    )
    assert resp.status_code == 403


def test_metering_http_pricing_operator_only(client):
    resp = client.get("/metering/pricing")
    assert resp.status_code == 200
    assert resp.json()["essence_per_usd"] == 100.0
    resp = client.post("/metering/pricing", json={"essence_per_usd": 250.0})
    assert resp.status_code == 200
    assert resp.json()["essence_per_usd"] == 250.0
    resp = client.post("/metering/pricing", json={"essence_per_usd": -1})
    assert resp.status_code == 400
    resp = client.post("/metering/settle")
    assert resp.status_code == 200
    assert "created" in resp.json()
