"""Issue #38: deterministic replay CLI (`uv run --package server python -m server.replay`).

A seeded multi-subsystem scenario is recorded (snapshot + pumped
intents), then replayed through the CLI. The replayed outcomes must
match the recorded ones bit-for-bit; a deliberate change or a
different seed must produce exit 1 with exact field-level diffs.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import zlib

import pytest

from .. import (
    affection,
    biology,
    database,
    determinism,
    intents,
    market,
    persistence,
    plots,
    sim_commands,
    social,
    world_tick,
)
from ..agents import metering

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SEED = "replay-test-seed-1"


def _mk_soul(conn, soul_id, x=500.0, y=500.0, essence=1000.0, **over):
    cols = {
        "soul_id": soul_id,
        "owner_id": "C1",
        "custodian_id": "C1",
        "name": soul_id,
        "essence": essence,
        "satiety": 100.0,
        "hydration": 100.0,
        "state": "normal",
        "fed_flag": 0,
        "loyalty": 0.5,
        "position": json.dumps([x, y]),
        "velocity": json.dumps([0.0, 0.0]),
        "move_target": None,
    }
    cols.update(over)
    conn.execute(
        f"INSERT INTO souls ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        tuple(cols.values()),
    )


def _record_scenario(sleep_escape=False):
    """Build souls, snapshot, enqueue + pump intents (the recorded run)."""
    affection.reset_carry_state()
    persistence.dirty.clear()
    with database.get_db() as conn:
        for soul_id in ("S_SELLER", "S_BUYER", "S_FED", "S_METER"):
            _mk_soul(conn, soul_id)
        _mk_soul(conn, "S_COLLAPSED", state="collapsed")
        _mk_soul(conn, "S_PET", x=100.0, y=100.0)
        _mk_soul(conn, "S_GATHER", x=200.0, y=200.0)
        plot_id = conn.execute(
            "SELECT plot_id FROM plots WHERE owner_id IS NULL LIMIT 1"
        ).fetchone()["plot_id"]
        conn.execute(
            "INSERT INTO resource_nodes "
            "(node_id, plot_id, kind, x, y, amount, capacity, state, created_at) "
            "VALUES (?,?,?,?,?,?,?,?, ?)",
            (
                "node_replay1",
                plot_id,
                "food",
                205.0,
                200.0,
                10,
                10,
                "ready",
                time.time(),
            ),
        )
        conn.execute(
            "INSERT INTO soul_inventory (soul_id, item_name, quantity) "
            "VALUES (?, ?, ?)",
            ("S_SELLER", "Test Widget", 1),
        )
        conn.commit()
        snap_id = persistence.take_snapshot(conn, 0, scenario_seed=SEED)

    tick = world_tick.WorldTick(scenario_seed=SEED)
    tick.tick_id = 7

    market.enqueue_market_intent(
        "sess", "n1", "C1", "S_SELLER", market.KIND_MARKET_LIST,
        {
            "seller_soul_id": "S_SELLER",
            "item": {"name": "Test Widget", "kind": "trinket"},
            "price": 25.0,
        },
    )
    intents.enqueue_intent(
        "sess", "n2", "C1", "S_BUYER", "move_to", {"x": 600.0, "y": 600.0}
    )
    social.enqueue_social_intent(
        "sess", "n3", "C1", "S_SELLER", social.KIND_SOCIAL_POST,
        {
            "author_type": "soul",
            "author_id": "S_SELLER",
            "title": "Hello",
            "body": "world",
        },
    )
    plots.enqueue_plot_intent(
        "sess", "n4", "C1", "S_BUYER", plots.KIND_PLOT_CLAIM,
        {"claimant_soul_id": "S_BUYER"},
    )
    biology.enqueue_feed_soul(
        "sess", "n5", "C1", "S_FED", biology.KIND_FEED_SOUL,
        {"feeder_soul_id": "S_FED", "recipient_soul_id": "S_COLLAPSED"},
    )
    tick.pump_intents()

    with database.get_db() as conn:
        listing_id = conn.execute(
            "SELECT listing_id FROM marketplace"
        ).fetchone()["listing_id"]
    market.enqueue_market_intent(
        "sess", "n6", "C1", "S_BUYER", market.KIND_MARKET_BUY,
        {"listing_id": listing_id, "buyer_soul_id": "S_BUYER"},
    )
    intents.enqueue_intent(
        "sess", "n7", "C1", "S_GATHER", "gather", {"node_id": "node_replay1"}
    )
    intents.enqueue_intent(
        "sess", "n8", "C1", "S_GATHER", "gather", {"node_id": "node_nope"}
    )
    intents.enqueue_intent(
        "sess", "n9", "C1", "S_PET", "carry_move", {"phase": "grab"}
    )
    tick.pump_intents()

    if sleep_escape:
        time.sleep(5.2)
    move_rec = intents.enqueue_intent(
        "sess", "n10", "C1", "S_PET", "carry_move",
        {"phase": "move", "x": 105.0, "y": 105.0},
    )
    tick.pump_intents()
    intents.enqueue_intent(
        "sess", "n11", "C1", "S_PET", "carry_move", {"phase": "release"}
    )
    intents.enqueue_intent("sess", "n12", "C1", "S_PET", "affection_pet", {})

    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO metering_events "
            "(event_id, soul_id, tier, provider, model, prompt_tokens, "
            "completion_tokens, cost_usd_estimate, created_at) "
            "VALUES (?,?,?,?,?,?,?,?, ?)",
            (
                "evt_replay1",
                "S_METER",
                "standard",
                "prov",
                "model",
                100,
                50,
                1.0,
                time.time(),
            ),
        )
        conn.commit()
    metering.maybe_run_batch(force=True)
    tick.pump_intents()

    sim_commands.plot_set_policy(
        {"plot_id": plot_id, "access_policy": "closed", "operator_id": "op-test"},
        tick,
    )

    with database.get_db() as conn:
        head = persistence.journal_head(conn)
        placeholders = ", ".join(
            "?" for _ in persistence.ADJUDICATION_OUTCOME_TYPES
        )
        n_adj = conn.execute(
            f"SELECT COUNT(*) c FROM journal WHERE type IN ({placeholders})",
            tuple(sorted(persistence.ADJUDICATION_OUTCOME_TYPES)),
        ).fetchone()["c"]
    return {
        "snapshot_id": snap_id,
        "journal_head": head,
        "n_adjudicated": n_adj,
        "move_intent_id": move_rec["intent_id"],
        "plot_id": plot_id,
    }


def _run_cli(*args):
    proc = subprocess.run(
        [sys.executable, "-m", "server.replay", *args],
        cwd=REPO_ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc


def test_replay_matches_recorded_outcomes():
    scenario = _record_scenario()
    assert scenario["n_adjudicated"] == 13

    proc = _run_cli(
        "--snapshot", str(scenario["snapshot_id"]),
        "--db", database.DB_PATH, "--json",
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout)

    assert report["identical"] is True
    assert report["n_divergent"] == 0
    assert report["n_intents"] == 13
    assert report["seed"] == SEED
    assert report["snapshot"]["id"] == scenario["snapshot_id"]

    canonical = json.dumps(
        [
            {
                "intent_id": r["intent_id"],
                "status": r["replayed_status"],
                "result": r["replayed_result"],
            }
            for r in report["intents"]
        ],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert report["outcome_hash"] == hashlib.sha256(canonical).hexdigest()

    assert report["live_db"]["untouched"] is True
    assert (
        report["live_db"]["sha256_before"] == report["live_db"]["sha256_after"]
    )

    op = next(
        e for e in report["interventions"] if e["action"] == "plot_policy"
    )
    assert op["operator_id"] == "op-test"
    assert op["target_type"] == "plot"
    assert op["target_id"] == scenario["plot_id"]


def test_replay_diverges_on_tampered_snapshot():
    scenario = _record_scenario()
    with database.get_db() as conn:
        blob = conn.execute(
            "SELECT blob FROM snapshots WHERE snapshot_id = ?",
            (scenario["snapshot_id"],),
        ).fetchone()["blob"]
    try:
        doc = json.loads(zlib.decompress(bytes(blob)))
    except zlib.error:
        doc = json.loads(bytes(blob))
    assert doc["v"] == 2
    assert doc["scenario_seed"] == SEED
    assert doc["derivation"] == determinism.DERIVATION_VERSION
    tampered = False
    for row in doc["tables"]["souls"]:
        if row["soul_id"] == "S_PET":
            row["loyalty"] = 0.99
            tampered = True
    assert tampered
    tampered_path = os.path.join(REPO_ROOT, "server", "tests", ".snap_tampered.json")
    with open(tampered_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)

    try:
        proc = _run_cli(
            "--snapshot", tampered_path, "--db", database.DB_PATH, "--json"
        )
    finally:
        os.remove(tampered_path)
    assert proc.returncode == 1, proc.stderr[-2000:]
    report = json.loads(proc.stdout)
    assert report["identical"] is False
    assert report["n_divergent"] == 1
    divergent = next(r for r in report["intents"] if not r["identical"])
    assert divergent["kind"] == "affection_pet"
    loyalty_diffs = [
        d for d in divergent["diffs"] if d["field"] == "result.loyalty"
    ]
    assert len(loyalty_diffs) == 1
    assert loyalty_diffs[0]["recorded"] == pytest.approx(0.52)
    assert loyalty_diffs[0]["replayed"] == pytest.approx(1.0)


def test_replay_diverges_on_different_seed():
    scenario = _record_scenario(sleep_escape=True)
    proc = _run_cli(
        "--snapshot", str(scenario["snapshot_id"]),
        "--db", database.DB_PATH, "--json",
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    base = json.loads(proc.stdout)
    move_id = scenario["move_intent_id"]
    move_rec = next(r for r in base["intents"] if r["intent_id"] == move_id)
    assert move_rec["identical"] is True

    cap = affection.MAX_ESCAPE_DT_S
    rate = affection.DEFAULT_ESCAPE_RATE
    escape_p = 1.0 - (1.0 - rate) ** cap
    recorded_escaped = bool(move_rec["recorded_result"]["escaped"])
    flip_seed = None
    for i in range(200):
        candidate = f"replay-flip-seed-{i}"
        draw = determinism.derive_rng(candidate, "affection", move_id).random()
        if bool(draw < escape_p) != recorded_escaped:
            flip_seed = candidate
            break
    assert flip_seed is not None

    proc = _run_cli(
        "--snapshot", str(scenario["snapshot_id"]),
        "--db", database.DB_PATH, "--json", "--seed", flip_seed,
    )
    assert proc.returncode == 1, proc.stderr[-2000:]
    report = json.loads(proc.stdout)
    assert report["identical"] is False
    move_out = next(r for r in report["intents"] if r["intent_id"] == move_id)
    assert move_out["identical"] is False
    escape_diffs = [
        d for d in move_out["diffs"] if d["field"] == "result.escaped"
    ]
    assert len(escape_diffs) == 1
    assert escape_diffs[0]["recorded"] == recorded_escaped
    assert escape_diffs[0]["replayed"] == (not recorded_escaped)


def test_replay_explicit_subrange_warms_up():
    scenario = _record_scenario()
    outcome_types = tuple(sorted(persistence.ADJUDICATION_OUTCOME_TYPES))
    placeholders = ", ".join("?" for _ in outcome_types)
    with database.get_db() as conn:
        seqs = [
            r["seq"]
            for r in conn.execute(
                f"SELECT seq FROM journal WHERE type IN ({placeholders}) "
                "ORDER BY seq",
                outcome_types,
            ).fetchall()
        ]
    assert len(seqs) == 13
    lo, hi = seqs[-3], seqs[-1]
    proc = _run_cli(
        "--snapshot", str(scenario["snapshot_id"]),
        "--db", database.DB_PATH, "--json",
        "--journal-tail", f"{lo}-{hi}",
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout)
    assert report["identical"] is True
    assert report["n_intents"] == 3
    assert report["n_warmup"] == 10
    assert report["prefix_divergent"] == []
    assert report["journal_tail"] == {"lo": lo, "hi": hi}


def test_metering_dispute_replay_handoff():
    scenario = _record_scenario()
    with database.get_db() as conn:
        lines = metering.soul_line_items(conn, "S_METER")
    assert lines, "expected at least one settled metering batch"
    line = lines[0]
    assert "replay" in line
    replay = line["replay"]
    assert replay["snapshot_id"] == scenario["snapshot_id"]
    lo, hi = replay["journal_seq_range"]
    outcome_types = tuple(sorted(persistence.ADJUDICATION_OUTCOME_TYPES))
    placeholders = ", ".join("?" for _ in outcome_types)
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT seq, type, payload FROM journal "
            f"WHERE type IN ({placeholders}) AND seq BETWEEN ? AND ?",
            (*outcome_types, lo, hi),
        ).fetchall()
        debit_intent_id = conn.execute(
            "SELECT intent_id FROM intents WHERE kind = 'metering_debit' "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()["intent_id"]
    kinds = {
        (json.loads(r["payload"]).get("kind"), r["type"]) for r in rows
    }
    intent_ids = {json.loads(r["payload"]).get("intent_id") for r in rows}
    assert debit_intent_id in intent_ids
    assert any(
        t == persistence.EVENT_METERING_DEBIT_SETTLED for _, t in kinds
    )
    assert "server.replay" in replay["cli"]
    assert f"--journal-tail {lo}-{hi}" in replay["cli"]


def test_derive_rng_stable_and_scoped():
    first = [determinism.derive_rng(SEED, "affection", "i1").random() for _ in range(1)]
    second = [determinism.derive_rng(SEED, "affection", "i1").random() for _ in range(1)]
    assert first == second
    other = [
        determinism.derive_rng(SEED, "affection", "i2").random() for _ in range(10)
    ]
    same = [
        determinism.derive_rng(SEED, "affection", "i1").random() for _ in range(10)
    ]
    assert other != same
    other_scope = [
        determinism.derive_rng(SEED, "market", "i1").random() for _ in range(10)
    ]
    assert other_scope != same


def test_run_start_header_is_idempotent():
    with database.get_db() as conn:
        assert persistence.append_run_start(conn, 0, SEED, 5.0) is True
        conn.commit()
        assert persistence.append_run_start(conn, 0, SEED, 5.0) is False
        conn.commit()
        assert persistence.append_run_start(conn, 1, "other-seed", 5.0) is True
        conn.commit()
        rows = conn.execute(
            "SELECT payload FROM journal WHERE type = 'run_start' ORDER BY seq"
        ).fetchall()
    assert len(rows) == 2
    first = json.loads(rows[0]["payload"])
    assert first["seed"] == SEED
    assert first["derivation"] == determinism.DERIVATION_VERSION
    assert first["snapshot_format"] == 2
    assert first["tick_dt"] == 5.0


def test_tick_gen_id_restores_recorded_id():
    ctx = determinism.ReplayContext(
        seed=SEED,
        recorded={"i1": {"result": {"listing_id": "lst_recorded"}}},
    )
    tick = world_tick.WorldTick(scenario_seed=SEED)
    tick.replay = ctx
    got = determinism.tick_gen_id(
        tick, "i1", "listing_id", lambda: "lst_fresh"
    )
    assert got == "lst_recorded"
    assert determinism.tick_gen_id(
        tick, "i9", "listing_id", lambda: "lst_fresh"
    ) == "lst_fresh"


def test_tick_now_frozen_in_replay():
    ctx = determinism.ReplayContext(
        seed=SEED, recorded={"i1": {"created_at": 1234.5}}
    )
    tick = world_tick.WorldTick(scenario_seed=SEED)
    tick.replay = ctx
    ctx.begin_intent("i1")
    assert determinism.tick_now(tick) == pytest.approx(1234.5)
    ctx.begin_intent("unknown-intent")
    assert determinism.tick_now(tick) != pytest.approx(1234.5)
