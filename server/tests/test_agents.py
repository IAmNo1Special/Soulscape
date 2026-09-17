import asyncio
import json
import random
import time

import pytest

from .. import database
from .. import dormancy
from .. import intents
from .. import persistence
from ..agents import consume, drives, pool, reflex, scheduler, sensations, vocab
from ..world_tick import WorldTick


@pytest.fixture(autouse=True)
def clear_agent_state():
    sensations.clear()
    reflex._firings.clear()
    reflex._next_emote_at.clear()
    reflex._current_emote.clear()
    sched = scheduler.default()
    sched._next.clear()
    sched._last.clear()
    yield
    sensations.clear()
    reflex._firings.clear()
    reflex._next_emote_at.clear()
    reflex._current_emote.clear()
    sched._next.clear()
    sched._last.clear()


def _insert_soul(db_conn, soul_id, x=100.0, y=100.0, **kw):
    cols = {
        "soul_id": soul_id,
        "owner_id": f"owner_{soul_id}",
        "position": json.dumps([x, y]),
        "velocity": json.dumps([0.0, 0.0]),
        "essence": 100.0,
        "satiety": 100.0,
        "hydration": 100.0,
        "hp": 100.0,
        "max_hp": 100.0,
        "state": "normal",
        "nature": "Hardy",
    }
    cols.update(kw)
    names = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    db_conn.execute(
        f"INSERT INTO souls ({names}) VALUES ({placeholders})",
        tuple(
            json.dumps(v) if isinstance(v, (list, dict)) else v for v in cols.values()
        ),
    )
    db_conn.commit()


def _journal_types(db_conn):
    rows = db_conn.execute("SELECT type FROM journal").fetchall()
    return [r["type"] for r in rows]


def _journal_payloads(db_conn, event_type):
    rows = db_conn.execute(
        "SELECT payload FROM journal WHERE type = ?", (event_type,)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


# ---------------------------------------------------------------- vocab


def test_vocab_version_and_legal_set():
    assert vocab.VOCAB_VERSION == 1
    assert len(vocab.LEGAL_ACTIONS) == 18
    for action in (
        "move_to",
        "look",
        "gather",
        "eat",
        "drink",
        "flee",
        "give",
        "offer_trade",
        "accept_trade",
        "reject_trade",
        "post",
        "reply",
        "converse_say",
        "converse_leave",
        "rest",
        "claim_plot",
        "follow",
        "wait",
    ):
        assert vocab.is_legal(action), action


def test_vocab_illegal_off_menu():
    name, ok = vocab.validate("attack")
    assert not ok and name is None
    name, ok = vocab.validate("kill")
    assert not ok
    name, ok = vocab.validate(None)
    assert not ok
    assert vocab.PEACEFUL_SENSATION == (
        "You feel peaceful; violence isn't possible here."
    )


def test_vocab_canonical_normalizes_case():
    name, ok = vocab.validate("  Move_To ")
    assert ok and name == "move_to"


def test_vocab_alias_table_append_only_shape():
    assert isinstance(vocab.ALIASES, dict)
    name, ok = vocab.validate("move_to")
    assert ok


# ---------------------------------------------------------------- drives


def test_survival_drive_spikes_when_starving():
    assert drives.survival_drive(10.0, 100.0, 100.0, 100.0) == pytest.approx(0.9)
    assert drives.survival_drive(100.0, 100.0, 100.0, 100.0) == pytest.approx(0.0)
    assert drives.survival_drive(None, None, None, None) == pytest.approx(0.0)
    assert drives.survival_drive(100.0, 100.0, 0.0, 100.0) == pytest.approx(1.0)


def test_fear_zero_without_threat_kinds():
    obs = [{"kind": "soul", "distance": 5.0, "position": [1, 1]}]
    assert drives.fear_from_observations(obs) == 0.0
    assert drives.fear_from_observations([]) == 0.0


def test_nature_baselines_documented_defaults():
    assert drives.nature_baseline("Jolly", "social") == pytest.approx(0.7)
    assert drives.nature_baseline("Calm", "aggression") == pytest.approx(0.35)
    assert drives.nature_baseline("Nope", "wealth") == pytest.approx(0.5)
    assert drives.nature_baseline(None, "loyalty") == pytest.approx(0.5)
    assert 0.0 <= drives.nature_baseline("Jolly", "social") <= 1.0


def test_compute_drives_vector_shape():
    vec = drives.compute_drives("Hardy", 10.0, 90.0, 100.0, 100.0, [])
    assert set(vec) >= set(drives.DRIVES)
    assert vec["survival"] == pytest.approx(0.9)
    assert all(0.0 <= v <= 1.0 for v in vec.values())


# ---------------------------------------------------------------- scheduler


def test_scheduler_budget_and_rollover():
    sched = scheduler.ThinkScheduler(seed=7)
    now = 1000.0
    souls = [f"s{i}" for i in range(10)]
    sched.rebuild_on_boot(souls, now - 1000.0)
    first = sched.due(now, souls)
    assert len(first) == scheduler.THINK_MAX_PER_TICK == 4
    for sid in first:
        sched.schedule_next(sid, now)
    second = sched.due(now, souls)
    assert len(second) == 4
    assert not set(first) & set(second)


def test_scheduler_jitter_spread():
    sched = scheduler.ThinkScheduler(seed=3)
    intervals = [sched.schedule_next("s", 0.0) for _ in range(200)]
    lo = scheduler.THINK_BASE_INTERVAL * (1 - scheduler.THINK_JITTER)
    hi = scheduler.THINK_BASE_INTERVAL * (1 + scheduler.THINK_JITTER)
    assert all(lo <= i <= hi for i in intervals)
    assert max(intervals) - min(intervals) > 100.0


def test_scheduler_minimum_gap_binds_pull_forward():
    sched = scheduler.ThinkScheduler(seed=3)
    sched.schedule_next("s", 0.0)
    nxt = sched.pull_forward("s", 10.0)
    assert nxt == pytest.approx(0.0 + scheduler.THINK_MIN_GAP)


def test_scheduler_vision_enter_pulls_forward():
    sched = scheduler.ThinkScheduler(seed=3)
    sched.schedule_next("s", 1000.0)
    before = sched.next_think("s")
    assert before > 1400.0
    nxt = sched.note_vision_enter("s", 1010.0)
    assert nxt == pytest.approx(max(1015.0, 1000.0 + scheduler.THINK_MIN_GAP))
    assert nxt < before


def test_scheduler_pull_forward_never_pushes_later():
    sched = scheduler.ThinkScheduler(seed=3)
    sched.schedule_next("s", 0.0)
    sched._next["s"] = 50.0
    assert sched.pull_forward("s", 1000.0) == 50.0


def test_scheduler_boot_rebuild_staggers():
    sched = scheduler.ThinkScheduler(seed=11)
    souls = [f"s{i}" for i in range(50)]
    sched.rebuild_on_boot(souls, 0.0)
    times = [sched.next_think(s) for s in souls]
    assert all(0.0 <= t <= scheduler.THINK_BASE_INTERVAL for t in times)
    assert max(times) - min(times) > 100.0


def test_dormancy_wake_resets_think_schedule():
    sched = scheduler.default()
    now = time.time()
    sched.schedule_next("w1", now - 3600.0)
    assert sched.next_think("w1") < now
    dormancy.reset_think_schedule("w1")
    after = sched.next_think("w1")
    assert now <= after <= now + 2.0


# ---------------------------------------------------------------- reflex


def _drive_vec(satiety=100.0, hydration=100.0):
    return {
        "survival": drives.survival_drive(satiety, hydration, 100.0, 100.0),
        "fear": 0.0,
    }


def test_reflex_starving_seeks_food():
    provider = reflex.StubProvider(food=[(500.0, 500.0)])
    out = reflex.evaluate(
        "s1",
        100.0,
        100.0,
        10.0,
        90.0,
        _drive_vec(10.0, 90.0),
        [],
        provider,
        random.Random(1),
        0.0,
    )
    assert len(out["intents"]) == 1
    intent = out["intents"][0]
    assert intent["action"] == "move_to"
    assert intent["payload"]["x"] == pytest.approx(500.0)
    assert intent["payload"]["target_ref"]["kind"] == "food"


def test_reflex_at_food_emits_gather():
    provider = reflex.StubProvider(food=[(104.0, 100.0)])
    out = reflex.evaluate(
        "s1",
        100.0,
        100.0,
        10.0,
        90.0,
        _drive_vec(10.0, 90.0),
        [],
        provider,
        random.Random(1),
        0.0,
    )
    assert out["intents"][0]["action"] == "gather"
    assert out["intents"][0]["payload"]["node_id"].startswith("stub:food:")


def test_reflex_eats_from_inventory_when_stocked():
    provider = reflex.StubProvider(food=[(104.0, 100.0)])
    out = reflex.evaluate(
        "s1",
        100.0,
        100.0,
        10.0,
        90.0,
        _drive_vec(10.0, 90.0),
        [],
        provider,
        random.Random(1),
        0.0,
        inventory={"food": 2},
    )
    assert out["intents"][0]["action"] == "eat"


def test_reflex_drinks_from_inventory_when_stocked():
    provider = reflex.StubProvider(water=[(200.0, 200.0)])
    out = reflex.evaluate(
        "s1",
        100.0,
        100.0,
        90.0,
        10.0,
        _drive_vec(90.0, 10.0),
        [],
        provider,
        random.Random(1),
        0.0,
        inventory={"water": 1},
    )
    assert out["intents"][0]["action"] == "drink"


def test_reflex_no_food_degrades_gracefully():
    out = reflex.evaluate(
        "s1",
        100.0,
        100.0,
        10.0,
        90.0,
        _drive_vec(10.0, 90.0),
        [],
        reflex.NullProvider(),
        random.Random(1),
        0.0,
    )
    assert out["intents"] == []
    assert out["sensations"] == [
        {"text": reflex.NO_FOOD_SENSATION, "cause": "eat_reflex"}
    ]


def test_reflex_drink_threshold():
    provider = reflex.StubProvider(water=[(200.0, 200.0)])
    out = reflex.evaluate(
        "s1",
        100.0,
        100.0,
        90.0,
        10.0,
        _drive_vec(90.0, 10.0),
        [],
        provider,
        random.Random(1),
        0.0,
    )
    assert out["intents"][0]["action"] == "move_to"
    assert out["intents"][0]["payload"]["target_ref"]["kind"] == "water"


def test_reflex_emote_selection_jittered():
    rng = random.Random(5)
    reflex.schedule_emote("s1", 0.0, rng)
    due_at = reflex._next_emote_at["s1"]
    assert 120.0 <= due_at <= 300.0
    out = reflex.evaluate(
        "s1",
        0.0,
        0.0,
        10.0,
        90.0,
        _drive_vec(10.0, 90.0),
        [],
        reflex.NullProvider(),
        rng,
        due_at + 1.0,
    )
    assert out["emote"] == "hungry"
    assert reflex.emote_of("s1") == "hungry"


def test_reflex_firing_rate_hook_for_25():
    now = 5000.0
    reflex.note_firing("s1", now - 120.0)
    reflex.note_firing("s1", now - 20.0)
    reflex.note_firing("s1", now - 10.0)
    assert reflex.firing_rate("s1", now=now) == 2


# ---------------------------------------------------------------- sensations


def test_sensation_ring_bounded():
    for i in range(40):
        sensations.record("s1", f"n{i}", "test")
    assert len(sensations.recent("s1", limit=100)) == sensations.RING_SIZE
    texts = [s["text"] for s in sensations.recent("s1", limit=100)]
    assert "n0" not in texts and "n39" in texts


def test_sensation_journal_row(db_conn):
    with database.get_db() as conn:
        sensations.record("s1", "hello", "test", tick_id=3, journal_conn=conn)
        conn.commit()
    payloads = _journal_payloads(db_conn, sensations.EVENT_AGENT_SENSATION)
    assert len(payloads) == 1
    assert payloads[0]["text"] == "hello"
    assert payloads[0]["soul_id"] == "s1"


# ---------------------------------------------------------------- pool: scenario sim


def _think_pool():
    return pool.AgentPool(think_scheduler=scheduler.ThinkScheduler(seed=9))


def _insert_node(db_conn, node_id, kind, x, y, amount=10):
    from .. import resources

    resources.ensure_schema(db_conn)
    db_conn.execute(
        "INSERT INTO resource_nodes "
        "(node_id, plot_id, kind, x, y, amount, capacity, "
        "respawns_at, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'ready', ?)",
        (node_id, "0:0", kind, x, y, amount, resources.NODE_CAPACITY, time.time()),
    )
    db_conn.commit()


def test_starving_soul_seeks_food_then_gathers_then_eats(db_conn):
    from .. import resources
    from .. import world as world_mod

    _insert_soul(db_conn, "s1", x=100.0, y=100.0, satiety=10.0)
    _insert_node(db_conn, "node:food:t1", "food", 500.0, 500.0)
    vision = world_mod.WorldVision()
    vision.rebuild()
    provider = resources.node_provider()
    p = _think_pool()
    now = time.time()

    result = asyncio.run(p.think("s1", vision, provider, 0, now))
    assert result["status"] == "thought"
    pending = intents.pending_intents()
    assert len(pending) == 1
    assert pending[0]["kind"] == "move_to"
    assert pending[0]["payload"]["x"] == pytest.approx(500.0)
    assert pending[0]["payload"]["y"] == pytest.approx(500.0)

    db_conn.execute(
        "UPDATE souls SET position = ? WHERE soul_id = 's1'",
        (json.dumps([500.0, 500.0]),),
    )
    db_conn.commit()
    vision.rebuild()
    result = asyncio.run(p.think("s1", vision, provider, 0, now + 1.0))
    assert result["status"] == "thought"
    kinds = [i["kind"] for i in intents.pending_intents()]
    assert "gather" in kinds

    tick = WorldTick()
    tick.pump_intents()
    inv = db_conn.execute(
        "SELECT quantity FROM soul_inventory "
        "WHERE soul_id = 's1' AND item_name = 'food'"
    ).fetchone()
    assert int(inv["quantity"]) == 1

    result = asyncio.run(p.think("s1", vision, provider, 0, now + 2.0))
    assert result["status"] == "thought"
    kinds = [i["kind"] for i in intents.pending_intents()]
    assert "eat" in kinds
    tick.pump_intents()
    row = db_conn.execute(
        "SELECT satiety, xp FROM souls WHERE soul_id = 's1'"
    ).fetchone()
    assert float(row["satiety"]) > 10.0
    assert float(row["satiety"]) <= 100.0
    assert int(row["xp"]) == resources.XP_GATHER + resources.XP_EAT


def test_starving_no_food_sensation_no_crash(db_conn):
    _insert_soul(db_conn, "s1", satiety=5.0)
    from .. import world as world_mod

    vision = world_mod.WorldVision()
    vision.rebuild()
    p = _think_pool()
    result = asyncio.run(p.think("s1", vision, reflex.NullProvider(), 0, time.time()))
    assert result["status"] == "thought"
    assert intents.pending_intents() == []
    recent = sensations.recent("s1")
    assert any(s["text"] == reflex.NO_FOOD_SENSATION for s in recent)


def test_illegal_action_peaceful_sensation(db_conn):
    _insert_soul(db_conn, "s1")
    p = _think_pool()
    assert p.validate_and_enqueue("s1", "attack", {}, reflex.NullProvider(), 0) is None
    assert intents.pending_intents() == []
    recent = sensations.recent("s1")
    assert recent and recent[-1]["text"] == vocab.PEACEFUL_SENSATION
    payloads = _journal_payloads(db_conn, sensations.EVENT_AGENT_SENSATION)
    assert any(pl["text"] == vocab.PEACEFUL_SENSATION for pl in payloads)


def test_stale_intent_rejected_silently_into_journal(db_conn):
    _insert_soul(db_conn, "s1", x=100.0, y=100.0, satiety=10.0)
    provider = reflex.StubProvider(food=[(500.0, 500.0)])
    p = _think_pool()
    payload = {
        "x": 500.0,
        "y": 500.0,
        "target_ref": {"kind": "food", "at": [500.0, 500.0]},
    }
    assert p.validate_and_enqueue("s1", "move_to", payload, provider, 0)
    assert len(intents.pending_intents()) == 1

    provider.remove_food(500.0, 500.0)
    assert p.validate_and_enqueue("s1", "move_to", payload, provider, 1) is None
    assert len(intents.pending_intents()) == 1
    payloads = _journal_payloads(db_conn, sensations.EVENT_AGENT_STALE_REJECT)
    assert len(payloads) == 1
    assert payloads[0]["reason"] == "target_gone"
    assert payloads[0]["action"] == "move_to"


def test_execution_revalidate_reasons():
    provider = reflex.NullProvider()
    row = {
        "soul_id": "s1",
        "state": "normal",
        "essence": 5.0,
        "custodian_id": None,
        "owner_id": "o1",
    }
    assert pool.execution_revalidate(row, "post", {}, provider) == (
        False,
        "insufficient_essence",
    )
    assert pool.execution_revalidate(None, "wait", {}, provider) == (
        False,
        "soul_not_found",
    )
    collapsed = dict(row, state="collapsed")
    assert pool.execution_revalidate(collapsed, "wait", {}, provider) == (
        False,
        "collapsed",
    )
    dormant = dict(row, essence=0.0)
    assert pool.execution_revalidate(dormant, "wait", {}, provider) == (
        False,
        "soul_dormant",
    )
    assert pool.execution_revalidate(
        row, "move_to", {"x": float("nan"), "y": 0}, provider
    ) == (False, "bad_payload")
    assert pool.execution_revalidate(row, "wait", {}, provider) == (True, "")


def test_think_skips_dormant_and_collapsed(db_conn):
    _insert_soul(db_conn, "d1", essence=0.0)
    _insert_soul(db_conn, "c1", state="collapsed")
    from .. import world as world_mod

    vision = world_mod.WorldVision()
    vision.rebuild()
    p = _think_pool()
    now = time.time()
    assert (
        asyncio.run(p.think("d1", vision, reflex.NullProvider(), 0, now))["status"]
        == "dormant"
    )
    assert (
        asyncio.run(p.think("c1", vision, reflex.NullProvider(), 0, now))["status"]
        == "collapsed"
    )
    assert intents.pending_intents() == []


def test_observation_carries_recent_sensations(db_conn):
    _insert_soul(db_conn, "s1", satiety=5.0)
    from .. import world as world_mod

    vision = world_mod.WorldVision()
    vision.rebuild()
    p = _think_pool()
    asyncio.run(p.think("s1", vision, reflex.NullProvider(), 0, time.time()))
    obs = p.last_observation("s1")
    assert obs is not None
    assert any(s["text"] == reflex.NO_FOOD_SENSATION for s in obs["sensations"])
    assert "drives" in obs and "observations" in obs


def test_think_batch_bounded(db_conn):
    for i in range(6):
        _insert_soul(db_conn, f"b{i}")
    from .. import world as world_mod

    vision = world_mod.WorldVision()
    vision.rebuild()
    p = pool.AgentPool(
        max_concurrent=2, think_scheduler=scheduler.ThinkScheduler(seed=1)
    )
    results = asyncio.run(
        p.think_batch(
            [f"b{i}" for i in range(6)],
            vision,
            reflex.NullProvider(),
            0,
            time.time(),
        )
    )
    assert len(results) == 6
    assert all(r["status"] == "thought" for r in results)


# ---------------------------------------------------------------- consume


def test_consume_eats_from_inventory(db_conn):
    from .. import resources

    _insert_soul(db_conn, "s1", satiety=10.0)
    db_conn.execute(
        "INSERT INTO soul_inventory (soul_id, item_name, quantity, metadata) "
        "VALUES ('s1', 'food', 2, NULL)"
    )
    db_conn.commit()
    record = intents.enqueue_intent("test", "n1", None, "s1", "eat", {})
    tick = WorldTick()
    consume.adjudicate_consume(tick, record)
    row = db_conn.execute(
        "SELECT status FROM intents WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert row["status"] == "adjudicated"
    soul = db_conn.execute(
        "SELECT satiety, xp FROM souls WHERE soul_id = 's1'"
    ).fetchone()
    assert float(soul["satiety"]) == pytest.approx(50.0)
    assert int(soul["xp"]) == resources.XP_EAT
    qty = db_conn.execute(
        "SELECT quantity FROM soul_inventory "
        "WHERE soul_id = 's1' AND item_name = 'food'"
    ).fetchone()
    assert int(qty["quantity"]) == 1


def test_consume_rejects_empty_inventory(db_conn):
    _insert_soul(db_conn, "s1", satiety=10.0)
    record = intents.enqueue_intent("test", "n1", None, "s1", "eat", {})
    tick = WorldTick()
    consume.adjudicate_consume(tick, record)
    row = db_conn.execute(
        "SELECT status, result FROM intents WHERE intent_id = ?",
        (record["intent_id"],),
    ).fetchone()
    assert row["status"] == "rejected"
    assert json.loads(row["result"])["reason"] == "no_food"
    types = _journal_types(db_conn)
    assert persistence.EVENT_INTENT_REJECTED in types


def test_consume_rejects_collapsed_and_dormant(db_conn):
    _insert_soul(db_conn, "c1", state="collapsed")
    _insert_soul(db_conn, "d1", essence=0.0)
    tick = WorldTick()
    for sid, nonce in (("c1", "n1"), ("d1", "n2")):
        record = intents.enqueue_intent(
            "test", nonce, None, sid, "eat", {"food": [1.0, 1.0]}
        )
        consume.adjudicate_consume(tick, record)
        row = db_conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?",
            (record["intent_id"],),
        ).fetchone()
        assert row["status"] == "rejected"


# ---------------------------------------------------------------- tick wiring


def test_scheduler_earliest_due_and_prune():
    sched = scheduler.ThinkScheduler(seed=3)
    assert sched.earliest_due() is None
    sched.schedule_next("a", 0.0)
    sched.schedule_next("b", 0.0)
    assert sched.earliest_due() == min(sched.next_think("a"), sched.next_think("b"))
    sched.prune({"a"})
    assert sched.next_think("b") is None
    assert sched.next_think("a") is not None


def test_tick_agent_phase_boots_scheduler_and_skips_statues(db_conn):
    _insert_soul(db_conn, "a1", satiety=10.0)
    _insert_soul(db_conn, "a2", essence=0.0)
    tick = WorldTick()
    tick.step()
    sched = scheduler.default()
    assert not sched.needs_boot()
    assert sched.next_think("a1") is not None
    assert sched.next_think("a2") is None
    sched._next["a1"] = time.time() - 1.0
    tick.step()
    assert intents.pending_intents() == []
    recent = sensations.recent("a1")
    assert any(s["text"] == reflex.NO_FOOD_SENSATION for s in recent)


def test_tick_pump_routes_consume_kinds(db_conn):
    _insert_soul(db_conn, "s1", satiety=10.0)
    intents.enqueue_intent("test", "n1", None, "s1", "eat", {"food": [1, 1]})
    tick = WorldTick()
    tick.pump_intents()
    row = db_conn.execute(
        "SELECT status FROM intents WHERE session_id = 'test'"
    ).fetchone()
    assert row["status"] == "rejected"
