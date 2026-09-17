"""Memory tiers: working / episodic / semantic (issue #26).

Verification for every acceptance criterion, with real evidence:
- restart recall: scenario test with a simulated cold restart
- summarizer: 200 low + 10 high salience episodes -> shrinks,
  keeps salient facts, idempotent
- retrieval: metadata-first vs pure-cosine baseline on a hand-built
  12-query relevance set (MRR)
- perf: retrieve_semantic timed over a 10k-memory corpus
"""

import asyncio
import json
import time

import pytest

from .. import biology, database, dormancy
from ..agents import deliberation, memory, pool, reflex, scheduler, sensations, vocab


@pytest.fixture(autouse=True)
def _clear_agent_state():
    deliberation.reset_tracker()
    deliberation.clear_identity_cache()
    sensations.clear()
    reflex._firings.clear()
    sched = scheduler.default()
    sched._next.clear()
    sched._last.clear()
    yield
    deliberation.reset_tracker()
    deliberation.clear_identity_cache()
    sensations.clear()
    reflex._firings.clear()
    sched._next.clear()
    sched._last.clear()


def _insert_soul(db_conn, soul_id, **over):
    cols = {
        "soul_id": soul_id,
        "owner_id": f"owner_{soul_id}",
        "custodian_id": f"owner_{soul_id}",
        "name": "TestSoul",
        "species": "wisp",
        "nature": "Hardy",
        "level": 1,
        "satiety": 100.0,
        "hydration": 100.0,
        "hp": 100.0,
        "max_hp": 100.0,
        "essence": 100.0,
        "state": "normal",
        "fed_flag": 0,
        "position": json.dumps([100.0, 100.0]),
        "velocity": json.dumps([0.0, 0.0]),
        "move_target": None,
    }
    cols.update(over)
    db_conn.execute(
        f"INSERT INTO souls ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        tuple(cols.values()),
    )
    db_conn.commit()


def _store_key(tamer_id, provider="google"):
    from .. import key_vault

    nonce, ciphertext = key_vault.encrypt_key(f"fake-{provider}-key-value")
    key_vault.store_key(
        tamer_id,
        key_vault.new_key_id(),
        provider,
        "test",
        "1234",
        nonce,
        ciphertext,
    )


def _vision():
    from .. import world as world_mod

    vision = world_mod.WorldVision()
    vision.rebuild()
    return vision


# ---------------------------------------------------------- working


def test_working_memory_bounded_newest_last():
    for i in range(30):
        memory.remember_working("s1", {"kind": "observation", "text": f"obs {i}"})
    ctx = memory.working_context("s1", limit=100)
    obs = [e for e in ctx if e["kind"] == "observation"]
    assert len(obs) == memory.WORKING_CAPS["observation"]
    assert obs[0]["text"] == "obs 14"
    assert obs[-1]["text"] == "obs 29"


def test_working_context_merges_sensation_ring_newest_last():
    memory.remember_working("s1", {"kind": "intent", "text": "enqueued wait"})
    sensations.record("s1", "a sensation", "test")
    memory.remember_working("s1", {"kind": "rationale", "text": "why"})
    ctx = memory.working_context("s1")
    kinds = [e["kind"] for e in ctx]
    assert kinds == ["intent", "sensation", "rationale"]
    ats = [e["at"] for e in ctx]
    assert ats == sorted(ats)


def test_remember_working_rejects_sensations():
    with pytest.raises(ValueError):
        memory.remember_working("s1", {"kind": "sensation", "text": "x"})


# ---------------------------------------------------------- episodic


def test_episode_vocab_stamped(db_conn):
    eid = memory.log_episode("s1", "reflex", {"summary": "ate"}, salience=0.5)
    row = db_conn.execute(
        "SELECT vocab_version, summarized, kind FROM episodes WHERE episode_id = ?",
        (eid,),
    ).fetchone()
    assert row["vocab_version"] == vocab.VOCAB_VERSION
    assert row["summarized"] == 0
    assert row["kind"] == "reflex"


def test_high_salience_episode_writes_semantic_immediately(db_conn):
    eid = memory.log_episode(
        "s1",
        "deliberation",
        {
            "summary": "RATIONALE-ZETA-9 the blue gate leads north",
            "rationale": "RATIONALE-ZETA-9 the blue gate leads north",
        },
        salience=0.9,
    )
    row = db_conn.execute(
        "SELECT text, vocab_version, dim, salience FROM semantic_memories "
        "WHERE memory_id = ?",
        (f"ep:{eid}",),
    ).fetchone()
    assert row is not None
    assert "RATIONALE-ZETA-9" in row["text"]
    assert row["vocab_version"] == vocab.VOCAB_VERSION
    assert row["dim"] == memory.EMBED_DIM
    assert row["salience"] == pytest.approx(0.9)


def test_low_salience_episode_not_in_semantic_store(db_conn):
    memory.log_episode("s1", "reflex", {"summary": "routine"}, salience=0.2)
    n = db_conn.execute("SELECT COUNT(*) AS c FROM semantic_memories").fetchone()["c"]
    assert n == 0


def test_log_episode_rejects_unknown_kind():
    with pytest.raises(ValueError):
        memory.log_episode("s1", "dream", {"summary": "x"})


# ------------------------------------------------------- summarizer


def _seed_shrink_corpus(base_ts):
    kinds = ["reflex", "feed", "dormancy"]
    for i in range(200):
        memory.log_episode(
            "s1",
            kinds[i % 3],
            {"summary": f"routine {kinds[i % 3]} event number {i}"},
            salience=0.2,
            ts=base_ts + i,
        )
    facts = [f"FACT-ALPHA-{i}" for i in range(10)]
    for i, fact in enumerate(facts):
        memory.log_episode(
            "s1",
            "deliberation",
            {
                "summary": f"{fact}: the blue gate leads north",
                "rationale": f"{fact}: the blue gate leads north",
            },
            salience=0.9,
            ts=base_ts + 1000 + i,
        )
    return facts


def test_summarizer_shrinks_raw_rows_retains_salient_facts(db_conn):
    now = time.time()
    # Saturday 01:00 UTC, ~2-5 days ago: safely past min age, inside
    # the 7d raw retention, mid-week so one digest row results.
    base = memory._week_start(now) - 2 * 86400 + 3600
    facts = _seed_shrink_corpus(base)

    report = memory.run_summarizer(now=now, min_age_s=86400)
    assert report["episodes_folded"] == 200
    assert report["kept_verbatim"] == 10
    assert report["souls"] == 1

    # raw unsummarized rows collapse to zero
    n = db_conn.execute(
        "SELECT COUNT(*) AS c FROM episodes WHERE summarized = 0"
    ).fetchone()["c"]
    assert n == 0

    # digest carries the per-kind counts and the salient facts
    digest = db_conn.execute(
        "SELECT digest_text, episode_count, kept_count, vocab_version "
        "FROM weekly_digests WHERE soul_id = 's1'"
    ).fetchone()
    assert digest is not None
    assert digest["episode_count"] == 200
    assert digest["kept_count"] == 10
    assert digest["vocab_version"] == vocab.VOCAB_VERSION
    assert "- reflex: 67" in digest["digest_text"]
    assert "- feed: 67" in digest["digest_text"]
    assert "- dormancy: 66" in digest["digest_text"]
    for fact in facts:
        assert fact in digest["digest_text"]

    # the run itself is journaled
    n_journal = db_conn.execute(
        "SELECT COUNT(*) AS c FROM journal WHERE type = ?",
        (memory.EVENT_SUMMARIZER_RUN,),
    ).fetchone()["c"]
    assert n_journal == 1

    # idempotent: a second run marks nothing new, touches nothing
    report2 = memory.run_summarizer(now=now, min_age_s=86400)
    assert report2["episodes_folded"] == 0
    assert report2["kept_verbatim"] == 0
    n_journal2 = db_conn.execute(
        "SELECT COUNT(*) AS c FROM journal WHERE type = ?",
        (memory.EVENT_SUMMARIZER_RUN,),
    ).fetchone()["c"]
    assert n_journal2 == 1
    digest2 = db_conn.execute(
        "SELECT digest_text FROM weekly_digests WHERE soul_id = 's1'"
    ).fetchone()["digest_text"]
    assert digest2 == digest["digest_text"]


def test_summarizer_prunes_summarized_rows_past_7d(db_conn):
    now = time.time()
    ancient = now - 8 * 86400
    memory.log_episode(
        "s1", "reflex", {"summary": "old"}, salience=0.2, ts=ancient
    )
    memory.run_summarizer(now=now, min_age_s=3600)
    n = db_conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
    assert n == 0


# ---------------------------------------------------- restart recall


def test_restart_preserves_consistent_recall(db_conn):
    now = time.time()
    base = memory._week_start(now) - 2 * 86400 + 3600
    sid = "restart-soul"
    memory.log_episode(
        sid,
        "feed",
        {"summary": "was fed by soul-7 (+10e)", "role": "recipient"},
        salience=0.7,
        ts=base,
    )
    memory.log_episode(
        sid,
        "reflex",
        {"summary": "reflex fired: move_to", "actions": ["move_to"]},
        salience=0.6,
        ts=base + 10,
    )
    memory.log_episode(
        sid,
        "deliberation",
        {
            "summary": "RATIONALE-ZETA-9: the blue gate leads north "
            "past the old willow",
            "rationale": "RATIONALE-ZETA-9: the blue gate leads north "
            "past the old willow",
            "intents": ["move_to"],
        },
        salience=0.85,
        ts=base + 20,
    )
    memory.log_episode(
        sid,
        "dormancy",
        {"summary": "woke from dormancy (essence 0 -> 42)"},
        salience=0.7,
        ts=base + 30,
    )
    memory.run_summarizer(now=now, min_age_s=3600)

    ctx_before = memory.wake_context(sid)
    block_before = memory.wake_block(sid)
    assert "RATIONALE-ZETA-9" in block_before
    assert "soul-7" in block_before
    assert ctx_before["digest"] is not None
    assert len(ctx_before["episodes"]) == 3  # 0.7+ salience rows

    # ---- simulate a cold restart: drop ALL process-volatile state,
    # same database. reset_volatile() is the honest equivalent of a
    # fresh import: working deques, wake-delivered set, maintenance
    # guard are process-only by design.
    memory.reset_volatile()
    sensations.clear()
    deliberation.clear_identity_cache()

    ctx_after = memory.wake_context(sid)
    block_after = memory.wake_block(sid)
    assert ctx_after == ctx_before
    assert block_after == block_before
    assert "RATIONALE-ZETA-9" in block_after

    # the first post-restart deliberation restores it into the prompt
    _insert_soul(db_conn, sid)
    _store_key(f"owner_{sid}")
    captured = {}

    def fake_call(provider, model, prompt, timeout_s, key):
        captured["prompt"] = prompt
        return json.dumps(
            {
                "intents": [{"action": "wait", "params": {}}],
                "rationale": "Checking the gate route.",
            }
        )

    p = pool.AgentPool(think_scheduler=scheduler.ThinkScheduler(seed=1))
    d = deliberation.Deliberator(
        p, tracker=deliberation.EscalationTracker(), call_provider=fake_call
    )
    result = asyncio.run(
        p.deliberate(sid, _vision(), reflex.NullProvider(), 7, now, "idle",
                     deliberator=d)
    )
    assert result["status"] == "deliberated"
    assert "LONG-TERM MEMORY" in captured["prompt"]
    assert "RATIONALE-ZETA-9" in captured["prompt"]
    assert "restored after restart" in captured["prompt"]

    # ...exactly once per process: the next deliberation has no wake
    captured.clear()
    result2 = asyncio.run(
        p.deliberate(sid, _vision(), reflex.NullProvider(), 8, now + 1,
                     "idle", deliberator=d)
    )
    assert result2["status"] == "deliberated"
    assert "restored after restart" not in captured["prompt"]


# ------------------------------------------------------- retrieval


def _seed_eval_corpus(now):
    sid = "eval-soul"
    day = 86400
    golds = [
        ("the blue gate leads north past the old willow", "deliberation"),
        ("traded three berries for a smooth stone with Rowan", "trade"),
        ("the well water tastes of iron after the rain", "observation"),
        ("Rowan promised to return at dusk with rope", "social"),
        ("the cellar stays cool even at noon", "observation"),
        ("ate two mushrooms near the fallen oak", "reflex"),
    ]
    queries = [
        ("where is the blue gate", 0),
        ("what did I trade with Rowan", 1),
        ("how does the well water taste", 2),
        ("what did Rowan promise", 3),
        ("where is it cool at noon", 4),
        ("what did I eat near the oak", 5),
        ("blue gate north willow", 0),
        ("berries stone trade Rowan", 1),
        ("iron taste water well rain", 2),
        ("dusk rope Rowan return promise", 3),
        ("cool cellar noon", 4),
        ("mushrooms oak ate", 5),
    ]
    distractors = [
        "blue gate blue gate blue gate painted blue",
        "gate the blue gate gate blue",
        "trade trade berries berries stone stone Rowan Rowan",
        "Rowan berries trade stone trade",
        "water water well well iron iron taste taste rain",
        "well water iron rain taste",
        "Rowan Rowan dusk dusk rope rope promise promise return",
        "Rowan promise rope dusk return",
        "cellar cellar cool cool noon noon stays stays",
        "cellar cool noon stays",
        "mushrooms mushrooms oak oak ate ate fallen fallen",
        "oak mushrooms ate fallen",
    ]
    fillers = [
        "the sky was clear at dawn",
        "a crow circled the tower twice",
        "moss grows thick on the north stones",
        "the path forks near the dead pine",
        "rain drums on the canvas awning",
        "someone left footprints by the creek",
        "the wind smells of pine and smoke",
        "a bell rang three times at midnight",
    ]
    with database.get_db() as conn:
        n = 0

        def add(text, salience, age_days, kind):
            nonlocal n
            memory._upsert_semantic(
                conn,
                memory_id=f"eval{n}",
                soul_id=sid,
                episode_id=None,
                text=text,
                embedding=memory.embed_text(text),
                salience=salience,
                kind=kind,
                created_at=now - age_days * day,
                vocab_version=vocab.VOCAB_VERSION,
            )
            n += 1

        for text, kind in golds:
            add(text, 0.8, 1.0, kind)
        for i, text in enumerate(distractors):
            # even: stale (outside the 30d metadata window);
            # odd: fresh but trivially low salience.
            if i % 2 == 0:
                add(text, 0.9, 60.0, "distractor")
            else:
                add(text, 0.1, 1.0, "distractor")
        for text in fillers:
            add(text, 0.5, 2.0, "filler")
        conn.commit()
    return sid, [(q, golds[gi][0]) for q, gi in queries]


def _mrr(sid, queries, retrieve):
    total = 0.0
    for query, gold in queries:
        ranked = retrieve(sid, query, k=5)
        rank = next(
            (i + 1 for i, text in enumerate(ranked) if text == gold), None
        )
        total += 1.0 / rank if rank else 0.0
    return total / len(queries)


def test_metadata_first_beats_embedding_only_baseline():
    now = time.time()
    sid, queries = _seed_eval_corpus(now)
    mrr_meta = _mrr(sid, queries, memory.retrieve_semantic)
    mrr_base = _mrr(sid, queries, memory.baseline_retrieve)
    print(f"\nMRR metadata-first: {mrr_meta:.3f} | baseline: {mrr_base:.3f}")
    assert mrr_meta > mrr_base, (
        f"metadata-first ({mrr_meta:.3f}) did not beat baseline "
        f"({mrr_base:.3f})"
    )
    assert mrr_meta >= 0.9


def test_retrieve_semantic_delegates_from_deliberation_stub():
    memory.log_episode(
        "s9", "deliberation", {"summary": "the heron fishes at dawn"},
        salience=0.8,
    )
    got = deliberation.retrieve_semantic("s9", "heron fishes")
    assert got == memory.retrieve_semantic("s9", "heron fishes")
    assert got and "heron" in got[0]


def test_retrieve_perf_over_10k_memories():
    sid = "perf-soul"
    now = time.time()
    with database.get_db() as conn:
        for i in range(10000):
            age_days = i % 60
            sal = 0.2 + (i % 8) * 0.1
            text = (
                f"memory number {i} about "
                f"{'gate' if i % 7 == 0 else 'stone'} and other things"
            )
            memory._upsert_semantic(
                conn,
                memory_id=f"p{i}",
                soul_id=sid,
                episode_id=None,
                text=text,
                embedding=memory.embed_text(text),
                salience=sal,
                kind="observation",
                created_at=now - age_days * 86400,
                vocab_version=vocab.VOCAB_VERSION,
            )
        conn.commit()
    start = time.perf_counter()
    results = memory.retrieve_semantic(sid, "where is the gate", k=5)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    print(f"\nretrieve_semantic over 10k memories: {elapsed_ms:.1f} ms")
    assert len(results) == 5
    assert elapsed_ms < 1000.0, f"too slow for the think path: {elapsed_ms:.1f} ms"


# ------------------------------------------------- think-path hooks


def test_think_logs_reflex_episode_and_working_memory(db_conn):
    _insert_soul(db_conn, "s1", satiety=10.0)
    p = pool.AgentPool(think_scheduler=scheduler.ThinkScheduler(seed=9))
    result = asyncio.run(
        p.think(
            "s1", _vision(), reflex.StubProvider(food=[(500.0, 500.0)]),
            0, time.time(),
        )
    )
    assert result["status"] == "thought"
    rows = db_conn.execute(
        "SELECT kind, salience, vocab_version, content FROM episodes "
        "WHERE soul_id = 's1'"
    ).fetchall()
    assert len(rows) >= 1
    assert rows[0]["kind"] == "reflex"
    assert rows[0]["vocab_version"] == vocab.VOCAB_VERSION
    ctx = memory.working_context("s1")
    kinds = {e["kind"] for e in ctx}
    assert "observation" in kinds
    assert "intent" in kinds


def test_quiet_think_writes_no_episode(db_conn):
    _insert_soul(db_conn, "s1")
    p = pool.AgentPool(think_scheduler=scheduler.ThinkScheduler(seed=9))
    result = asyncio.run(
        p.think("s1", _vision(), reflex.NullProvider(), 0, time.time())
    )
    assert result["status"] == "thought"
    n = db_conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
    assert n == 0
    # ...but the observation still joins working memory
    assert any(
        e["kind"] == "observation" for e in memory.working_context("s1")
    )


def test_deliberate_logs_episode_with_rationale(db_conn):
    sid = "d1"
    _insert_soul(db_conn, sid)
    _store_key(f"owner_{sid}")

    def fake_call(provider, model, prompt, timeout_s, key):
        return json.dumps(
            {
                "intents": [{"action": "wait", "params": {}}],
                "rationale": "RATIONALE-DELTA-3: resting conserves essence.",
            }
        )

    p = pool.AgentPool(think_scheduler=scheduler.ThinkScheduler(seed=3))
    d = deliberation.Deliberator(
        p, tracker=deliberation.EscalationTracker(), call_provider=fake_call
    )
    result = asyncio.run(
        p.deliberate(sid, _vision(), reflex.NullProvider(), 7, time.time(),
                     "idle", deliberator=d)
    )
    assert result["status"] == "deliberated"
    row = db_conn.execute(
        "SELECT salience, content, vocab_version FROM episodes "
        "WHERE soul_id = ? AND kind = 'deliberation'",
        (sid,),
    ).fetchone()
    assert row is not None
    assert row["salience"] == pytest.approx(0.8)
    assert row["vocab_version"] == vocab.VOCAB_VERSION
    assert "RATIONALE-DELTA-3" in row["content"]
    notes = memory.working_notes(sid)
    assert any("RATIONALE-DELTA-3" in note for note in notes)
    # salience 0.8 >= 0.7: retrievable immediately, no nightly run
    assert "RATIONALE-DELTA-3" in memory.retrieve_semantic(
        sid, "resting conserves essence"
    )[0]


def test_feed_soul_logs_episodes_for_both_souls(db_conn):
    _insert_soul(db_conn, "feeder", essence=100.0)
    _insert_soul(db_conn, "recip", essence=20.0, state="collapsed", hp=0.0)
    biology.enqueue_feed_soul(
        "sess",
        "nonceM",
        None,
        "feeder",
        "feed_soul",
        {"feeder_soul_id": "feeder", "recipient_soul_id": "recip"},
    )
    from .. import world_tick as wt

    wt.WorldTick().pump_intents()
    feeder = db_conn.execute(
        "SELECT kind, salience, content FROM episodes "
        "WHERE soul_id = 'feeder' AND kind = 'feed'"
    ).fetchone()
    recip = db_conn.execute(
        "SELECT kind, salience, content FROM episodes "
        "WHERE soul_id = 'recip' AND kind = 'feed'"
    ).fetchone()
    assert feeder is not None and "fed recip" in feeder["content"]
    assert feeder["salience"] == pytest.approx(0.6)
    assert recip is not None and "was fed by feeder" in recip["content"]
    assert recip["salience"] == pytest.approx(0.7)


def test_dormancy_transition_logs_episode(db_conn):
    _insert_soul(db_conn, "s1", essence=50.0)
    with database.get_db() as conn:
        conn.execute("UPDATE souls SET essence = 0.0 WHERE soul_id = 's1'")
        dormant, flipped = dormancy.note_essence_change(conn, "s1", 50.0, tick_id=1)
        conn.commit()
    assert flipped and dormant
    row = db_conn.execute(
        "SELECT kind, salience, content FROM episodes WHERE soul_id = 's1'"
    ).fetchone()
    assert row["kind"] == "dormancy"
    assert row["salience"] == pytest.approx(0.7)
    assert "froze into dormancy" in row["content"]


def test_intent_rejection_logs_outcome_episode(db_conn):
    _insert_soul(db_conn, "s1")
    from .. import world_tick as wt

    tick = wt.WorldTick()
    intent = {
        "intent_id": "i1",
        "kind": "move_to",
        "soul_id": "s1",
        "custodian_id": "owner_s1",
        "payload": {},
    }
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO intents (intent_id, session_id, nonce, soul_id, "
            "kind, payload, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("i1", "sess", "n1", "s1", "move_to", "{}", "pending", time.time()),
        )
        conn.commit()
        tick._reject(conn, intent, "custody")
    row = db_conn.execute(
        "SELECT kind, salience, content FROM episodes WHERE soul_id = 's1'"
    ).fetchone()
    assert row["kind"] == "intent_outcome"
    assert "custody" in row["content"]


# ------------------------------------------------- nightly trigger


def test_tick_maintenance_runs_nightly_and_cheap_guards(db_conn):
    r1 = memory.tick_maintenance(now=1000000.0)
    assert r1["ran"] is True
    r2 = memory.tick_maintenance(now=1000000.0 + 10.0)
    assert r2["ran"] is False  # in-memory cheap guard: no second run
    memory.reset_volatile()
    with database.get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO globals (key, value) VALUES (?, ?)",
            ("memory_last_summarize_at", 1000000.0 - 90000.0),
        )
        conn.commit()
    r3 = memory.tick_maintenance(now=1000000.0)
    assert r3["ran"] is True  # >24h since last: boot catch-up path


def test_assemble_prompt_memory_section_and_cap(db_conn):
    row = {
        "name": "S",
        "species": "wisp",
        "nature": "Hardy",
        "essence": 100.0,
        "satiety": 100.0,
        "hydration": 100.0,
        "hp": 100.0,
    }
    prompt, report = deliberation.assemble_prompt(
        "s1", row, memory_block="the blue gate leads north"
    )
    assert "LONG-TERM MEMORY:" in prompt
    assert "the blue gate leads north" in prompt
    assert report["memory_block"] is True
    assert report["memory_dropped"] is False
    assert deliberation.estimate_tokens(prompt) <= deliberation.PROMPT_TOKEN_CAP
    # no block: byte-identical contract to before #26
    prompt2, _ = deliberation.assemble_prompt("s1", row)
    assert "LONG-TERM MEMORY" not in prompt2
