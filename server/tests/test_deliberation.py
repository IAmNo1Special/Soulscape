import asyncio
import json
import math

import pytest

from .. import dormancy
from .. import key_vault
from ..agents import deliberation, pool, reflex, scheduler, sensations, vocab


class StubVision:
    def __init__(self, observations=None):
        self._obs = observations or {}

    def detail_observations(self, soul_id):
        return list(self._obs.get(soul_id, []))


@pytest.fixture(autouse=True)
def clear_deliberation_state():
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
        "name": "TestSoul",
        "species": "wisp",
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


def _store_key(tamer_id, provider):
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


def _journal_payloads(db_conn, event_type):
    rows = db_conn.execute(
        "SELECT payload FROM journal WHERE type = ?", (event_type,)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _usage_rows(db_conn, soul_id=None):
    if soul_id is None:
        rows = db_conn.execute("SELECT * FROM llm_usage").fetchall()
    else:
        rows = db_conn.execute(
            "SELECT * FROM llm_usage WHERE soul_id = ?", (soul_id,)
        ).fetchall()
    return [dict(r) for r in rows]


GOOD_JSON = json.dumps(
    {
        "intents": [
            {
                "action": "move_to",
                "params": {"x": 120.0, "y": 100.0},
            }
        ],
        "rationale": "Exploring east for resources.",
    }
)


def _fake_ok(provider, model, prompt, timeout_s, key):
    assert isinstance(key, bytearray)
    return GOOD_JSON


def _soul_row(db_conn, soul_id):
    row = db_conn.execute(
        "SELECT * FROM souls WHERE soul_id = ?", (soul_id,)
    ).fetchone()
    return dict(row)


# ---------------------------------------------------------------- triggers


def test_reflex_rate_escalation():
    tr = deliberation.EscalationTracker()
    now = 1000.0
    assert tr.should_escalate("s", now) is None
    reflex.note_firing("s", now - 50.0)
    reflex.note_firing("s", now - 30.0)
    assert tr.should_escalate("s", now) is None
    reflex.note_firing("s", now - 10.0)
    assert tr.should_escalate("s", now) == "reflex_rate"
    # One-shot for flags; rate persists while firing continues.
    assert tr.should_escalate("s", now) == "reflex_rate"


def test_reflex_rate_window_expiry():
    tr = deliberation.EscalationTracker()
    now = 1000.0
    for dt in (200.0, 150.0, 120.0):
        reflex.note_firing("s", now - dt)
    assert tr.should_escalate("s", now) is None


def test_wallet_delta_escalation():
    tr = deliberation.EscalationTracker()
    assert tr.note_wallet("s", 100.0, 1000.0) == 0.0
    assert tr.note_wallet("s", 95.0, 1001.0) == pytest.approx(-0.05)
    assert tr.should_escalate("s", 1001.0) is None
    tr.note_wallet("s", 80.0, 1002.0)
    assert tr.should_escalate("s", 1002.0) == "wallet_delta"
    assert tr.last_wallet_delta("s") == pytest.approx(0.1579, rel=1e-3)


def test_converse_order_tamer_return_flags():
    tr = deliberation.EscalationTracker()
    tr.note_converse_turn("s", 1000.0)
    assert tr.should_escalate("s", 1000.0) == "converse_turn"
    tr.issue_order("s", "go east", 1001.0)
    assert tr.pop_order("s") == "go east"
    assert tr.should_escalate("s", 1001.0) == "tamer_order"
    tr.note_tamer_return("s", 1002.0)
    assert tr.should_escalate("s", 1002.0) == "tamer_return"


def test_detail_enter_detection():
    tr = deliberation.EscalationTracker()
    obs = [{"id": "e1", "kind": "soul", "distance": 10.0}]
    assert tr.check_detail_enters("s", obs, 1000.0) == ["e1"]
    # Same entity seen again: no new flag.
    assert tr.check_detail_enters("s", obs, 1001.0) == []
    assert tr.should_escalate("s", 1001.0) == "detail_enter"
    obs2 = obs + [{"id": "e2", "kind": "soul", "distance": 5.0}]
    assert tr.check_detail_enters("s", obs2, 1002.0) == ["e2"]
    assert tr.should_escalate("s", 1002.0) == "detail_enter"


def test_flag_priority_order():
    tr = deliberation.EscalationTracker()
    tr.note_converse_turn("s", 1000.0)
    tr.issue_order("s", "go east", 1000.0)
    assert tr.should_escalate("s", 1000.0) == "tamer_order"


def test_idle_floor_eligibility():
    tr = deliberation.EscalationTracker()
    tr.mark_deliberated("s", 1000.0)
    assert not tr.idle_eligible("s", 1299.0)
    assert tr.idle_eligible("s", 1300.0)


def test_idle_thinks_rarely_vs_escalated():
    tr = deliberation.EscalationTracker()
    idle_thinks = 0
    escalated_thinks = 0
    for step in range(20):
        now = 1000.0 + step * 60.0
        if tr.idle_eligible("idle", now):
            idle_thinks += 1
            tr.mark_deliberated("idle", now)
        for _ in range(3):
            reflex.note_firing("busy", now - 5.0)
        if tr.should_escalate("busy", now) is not None:
            escalated_thinks += 1
    assert idle_thinks == 4  # every 5 min over 20 min
    assert escalated_thinks == 20
    assert idle_thinks * 4 < escalated_thinks


# ---------------------------------------------------------------- token cap


def test_estimate_tokens_measured():
    assert deliberation.estimate_tokens("") == 0
    assert deliberation.estimate_tokens("hello world") == math.ceil(11 / 4)
    assert deliberation.estimate_tokens("abcd") == 1


def test_prompt_within_token_cap_maximal_inputs(db_conn):
    _insert_soul(
        db_conn,
        "s1",
        name="A" * 60,
        species="B" * 60,
        nature="C" * 60,
    )
    row = _soul_row(db_conn, "s1")
    for i in range(32):
        sensations.record("s1", "x" * 280, f"cause-{i}", tick_id=0)
    observations = [
        {"id": f"e{i}", "kind": "soul", "distance": float(i)} for i in range(40)
    ]
    prompt, report = deliberation.assemble_prompt(
        "s1",
        row,
        sensations_list=sensations.recent("s1", limit=32),
        observations=observations,
        drive_vec={d: 0.5 for d in ("survival", "wealth", "social")},
        tamer_order="Do a thing. " * 20,
    )
    assert report["prompt_tokens"] <= deliberation.PROMPT_TOKEN_CAP
    # Identity is never truncated: byte-present.
    assert deliberation.identity_block("s1", row) in prompt
    # Action menu survives truncation.
    assert "claim_plot" in prompt
    assert "post (20e)" in prompt


def test_truncation_priority_identity_never_cut(db_conn, monkeypatch):
    _insert_soul(db_conn, "s1", name="KeepMe")
    row = _soul_row(db_conn, "s1")
    for i in range(32):
        sensations.record("s1", "y" * 280, f"cause-{i}", tick_id=0)
    observations = [
        {"id": f"e{i}", "kind": "soul", "distance": float(i)} for i in range(40)
    ]
    # Force the truncation loop to engage.
    monkeypatch.setattr(deliberation, "PROMPT_SOFT_BUDGET", 120)
    prompt, report = deliberation.assemble_prompt(
        "s1",
        row,
        sensations_list=sensations.recent("s1", limit=32),
        observations=observations,
    )
    assert report["truncated"]["observations"] > 0
    assert report["prompt_tokens"] <= deliberation.PROMPT_TOKEN_CAP
    assert deliberation.identity_block("s1", row) in prompt
    assert "claim_plot" in prompt


def test_identity_cache_invalidation(db_conn):
    _insert_soul(db_conn, "s1", name="Alpha")
    row = _soul_row(db_conn, "s1")
    first = deliberation.identity_block("s1", row)
    assert deliberation.identity_block("s1", row) == first  # cached
    assert "Alpha" in first
    row["name"] = "Beta"
    second = deliberation.identity_block("s1", row)
    assert second != first
    assert "Beta" in second


def test_semantic_stub_documented():
    assert deliberation.retrieve_semantic("s1", "anything") == []


# ---------------------------------------------------------------- parsing


def test_parse_output_valid():
    intents, rationale = deliberation.parse_output(GOOD_JSON)
    assert intents == [("move_to", {"x": 120.0, "y": 100.0})]
    assert rationale == "Exploring east for resources."


def test_parse_output_fenced():
    raw = "```json\n" + GOOD_JSON + "\n```"
    intents, _ = deliberation.parse_output(raw)
    assert intents[0][0] == "move_to"


def test_parse_output_rejects():
    with pytest.raises(deliberation.DeliberationFailure):
        deliberation.parse_output("not json at all {{{")
    with pytest.raises(deliberation.DeliberationFailure):
        deliberation.parse_output(json.dumps({"intents": []}))
    four = {
        "intents": [{"action": "wait", "params": {}}] * 4,
        "rationale": "too many",
    }
    with pytest.raises(deliberation.DeliberationFailure):
        deliberation.parse_output(json.dumps(four))
    with pytest.raises(deliberation.DeliberationFailure):
        deliberation.parse_output(json.dumps({"intents": [{"params": {}}]}))


# ---------------------------------------------------------------- routing


def _deliberator(p, **kw):
    kw.setdefault("tracker", deliberation.EscalationTracker())
    kw.setdefault("call_provider", _fake_ok)
    return deliberation.Deliberator(p, **kw)


def test_flash_default_routing(db_conn):
    _insert_soul(db_conn, "s1")
    _store_key("owner_s1", "google")
    p = pool.AgentPool()
    d = _deliberator(p)
    row = _soul_row(db_conn, "s1")
    result = d.deliberate(
        "s1", row, StubVision(), reflex.NullProvider(), 1000.0, "reflex_rate", tick_id=1
    )
    assert result["status"] == "deliberated"
    assert result["tier"] == "flash"
    assert result["model"] == "gemini-2.5-flash"
    assert not result["fallback_used"]
    rows = _usage_rows(db_conn, "s1")
    assert len(rows) == 1
    usage = rows[0]
    assert usage["tier"] == "flash"
    assert usage["provider"] == "google"
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    expected = deliberation.estimate_cost(
        "gemini-2.5-flash", usage["prompt_tokens"], usage["completion_tokens"]
    )
    assert usage["estimated_cost_usd"] == pytest.approx(expected)
    assert usage["fallback_used"] == 0
    events = _journal_payloads(db_conn, deliberation.EVENT_LLM_DELIBERATION)
    assert len(events) == 1
    assert events[0]["rationale"] == "Exploring east for resources."
    assert events[0]["intents"] == ["move_to"]


def test_high_stakes_wallet_routes_pro(db_conn):
    _insert_soul(db_conn, "s1")
    _store_key("owner_s1", "google")
    p = pool.AgentPool()
    tr = deliberation.EscalationTracker()
    d = deliberation.Deliberator(p, tracker=tr, call_provider=_fake_ok)
    tr.note_wallet("s1", 100.0, 900.0)
    tr.note_wallet("s1", 40.0, 1000.0)  # 60% drop
    reason = tr.should_escalate("s1", 1000.0)
    assert reason == "wallet_delta"
    row = _soul_row(db_conn, "s1")
    result = d.deliberate(
        "s1", row, StubVision(), reflex.NullProvider(), 1000.0, reason, tick_id=1
    )
    assert result["tier"] == "pro"
    assert result["model"] == "gemini-2.5-pro"


def test_tamer_order_routes_pro_and_rides_prompt(db_conn):
    _insert_soul(db_conn, "s1")
    _store_key("owner_s1", "openai")
    p = pool.AgentPool()
    tr = deliberation.EscalationTracker()
    seen_prompts = []

    def fake(provider, model, prompt, timeout_s, key):
        seen_prompts.append(prompt)
        return GOOD_JSON

    d = deliberation.Deliberator(p, tracker=tr, call_provider=fake)
    tr.issue_order("s1", "guard the gate", 1000.0)
    row = _soul_row(db_conn, "s1")
    result = d.deliberate(
        "s1", row, StubVision(), reflex.NullProvider(), 1000.0, "tamer_order", tick_id=1
    )
    assert result["tier"] == "pro"
    assert result["model"] == "gpt-5"
    assert "TAMER ORDER: guard the gate" in seen_prompts[0]


def test_flash_failures_promote_to_pro(db_conn):
    _insert_soul(db_conn, "s1")
    _store_key("owner_s1", "google")
    p = pool.AgentPool()
    tr = deliberation.EscalationTracker()

    def fake(provider, model, prompt, timeout_s, key):
        if "pro" in model:
            return GOOD_JSON
        return "garbage{{{ not json"

    d = deliberation.Deliberator(p, tracker=tr, call_provider=fake)
    row = _soul_row(db_conn, "s1")
    vision = StubVision()
    fw = reflex.NullProvider()
    r1 = d.deliberate("s1", row, vision, fw, 1000.0, "reflex_rate", 1)
    assert r1["status"] == "heuristic"  # flash garbage -> chain exhausted
    r2 = d.deliberate("s1", row, vision, fw, 1001.0, "reflex_rate", 1)
    assert r2["status"] == "heuristic"
    r3 = d.deliberate("s1", row, vision, fw, 1002.0, "reflex_rate", 1)
    assert r3["status"] == "deliberated"
    assert r3["tier"] == "pro"
    assert r3["model"] == "gemini-2.5-pro"
    tiers = [r["tier"] for r in _usage_rows(db_conn, "s1")]
    assert tiers == ["heuristic", "heuristic", "pro"]


def test_illegal_action_rejected_and_counted(db_conn):
    _insert_soul(db_conn, "s1")
    _store_key("owner_s1", "google")
    p = pool.AgentPool()
    tr = deliberation.EscalationTracker()
    bad = json.dumps(
        {
            "intents": [{"action": "attack", "params": {}}],
            "rationale": "violence",
        }
    )
    d = deliberation.Deliberator(p, tracker=tr, call_provider=lambda *a: bad)
    row = _soul_row(db_conn, "s1")
    result = d.deliberate(
        "s1", row, StubVision(), reflex.NullProvider(), 1000.0, "reflex_rate", 1
    )
    assert result["status"] == "heuristic"
    assert d._flash_failures.get("s1") == 1
    events = _journal_payloads(db_conn, deliberation.EVENT_LLM_DEGRADED)
    assert len(events) == 1
    assert "illegal_action" in events[0]["reason"]


# ---------------------------------------------------------------- fallback


def test_provider_fallback_chain_walks_providers(db_conn):
    _insert_soul(db_conn, "s1")
    _store_key("owner_s1", "google")
    _store_key("owner_s1", "openai")
    p = pool.AgentPool()
    calls = []

    def fake(provider, model, prompt, timeout_s, key):
        calls.append(provider)
        if provider == "google":
            raise TimeoutError("simulated timeout")
        return GOOD_JSON

    d = _deliberator(p, call_provider=fake)
    row = _soul_row(db_conn, "s1")
    result = d.deliberate(
        "s1", row, StubVision(), reflex.NullProvider(), 1000.0, "reflex_rate", 1
    )
    assert result["status"] == "deliberated"
    assert result["provider"] == "openai"
    assert result["fallback_used"] is True
    assert calls == ["google", "openai"]
    usage = _usage_rows(db_conn, "s1")[0]
    assert usage["provider"] == "openai"
    assert usage["fallback_used"] == 1


def test_tamer_preference_jumps_queue(db_conn):
    _insert_soul(db_conn, "s1")
    _store_key("owner_s1", "google")
    _store_key("owner_s1", "anthropic")
    p = pool.AgentPool()
    calls = []

    def fake(provider, model, prompt, timeout_s, key):
        calls.append(provider)
        return GOOD_JSON

    d = _deliberator(p, call_provider=fake)
    d.set_provider_preference("owner_s1", "anthropic")
    row = _soul_row(db_conn, "s1")
    result = d.deliberate(
        "s1", row, StubVision(), reflex.NullProvider(), 1000.0, "reflex_rate", 1
    )
    assert result["provider"] == "anthropic"
    assert calls == ["anthropic"]
    with pytest.raises(ValueError):
        d.set_provider_preference("owner_s1", "nope")


def test_total_outage_falls_back_to_reflex(db_conn):
    _insert_soul(db_conn, "s1", satiety=5.0)
    _store_key("owner_s1", "google")

    def fake(provider, model, prompt, timeout_s, key):
        raise ConnectionError("simulated 500")

    p = pool.AgentPool()
    d = _deliberator(p, call_provider=fake)
    row = _soul_row(db_conn, "s1")
    result = d.deliberate(
        "s1", row, StubVision(), reflex.NullProvider(), 1000.0, "reflex_rate", 1
    )
    assert result["status"] == "heuristic"
    assert result["fallback_used"] is True
    # Pool level: the soul still acts via the reflex think, no raise.
    fw = reflex.StubProvider(food=[(108.0, 100.0)])
    summary = asyncio.run(
        p.deliberate("s1", StubVision(), fw, 1, 1000.0, "reflex_rate", d)
    )
    assert summary["deliberation"] == "heuristic_fallback"
    assert summary["status"] == "thought"
    assert summary["enqueued"], "starving soul should still seek food"
    degraded = _journal_payloads(db_conn, deliberation.EVENT_LLM_DEGRADED)
    assert degraded, "degradation must be journaled"
    assert degraded[0]["fallback"] == "heuristic_reflex"
    usage = _usage_rows(db_conn, "s1")
    assert usage and usage[0]["tier"] == "heuristic"
    assert usage[0]["fallback_used"] == 1


def test_no_keys_degrades_cleanly(db_conn):
    _insert_soul(db_conn, "s1")
    p = pool.AgentPool()
    d = _deliberator(p)
    row = _soul_row(db_conn, "s1")
    result = d.deliberate(
        "s1", row, StubVision(), reflex.NullProvider(), 1000.0, "idle", 1
    )
    assert result["status"] == "heuristic"
    assert result["degraded_reason"] == "no_provider_keys"


def test_pool_deliberate_never_raises(db_conn):
    _insert_soul(db_conn, "s1")
    _store_key("owner_s1", "google")

    def boom(provider, model, prompt, timeout_s, key):
        raise RuntimeError("unexpected")

    p = pool.AgentPool()
    d = _deliberator(p, call_provider=boom)
    summary = asyncio.run(
        p.deliberate(
            "s1",
            StubVision(),
            reflex.NullProvider(),
            1,
            1000.0,
            "reflex_rate",
            d,
        )
    )
    assert summary["status"] in ("thought", "heuristic_fallback", "dormant")


def test_dormant_soul_never_deliberates(db_conn):
    _insert_soul(db_conn, "s1", essence=0.0)
    assert dormancy.is_dormant(0.0)
    p = pool.AgentPool()
    d = _deliberator(p)
    summary = asyncio.run(
        p.deliberate("s1", StubVision(), reflex.NullProvider(), 1, 1000.0, "idle", d)
    )
    assert summary["status"] == "dormant"
    assert _usage_rows(db_conn, "s1") == []


def test_default_http_boundary_is_the_only_network_path(db_conn, monkeypatch):
    _insert_soul(db_conn, "s1")
    _store_key("owner_s1", "google")

    def raising(provider, model, prompt, timeout_s, key):
        raise AssertionError("network path must not run in tests")

    monkeypatch.setattr(deliberation, "default_provider_call", raising)
    p = pool.AgentPool()
    # Constructed AFTER the patch: binds the module-level boundary.
    d = deliberation.Deliberator(p, tracker=deliberation.EscalationTracker())
    row = _soul_row(db_conn, "s1")
    result = d.deliberate(
        "s1", row, StubVision(), reflex.NullProvider(), 1000.0, "idle", 1
    )
    # Boundary raised -> walked past -> heuristic fallback, no crash.
    assert result["status"] == "heuristic"


# ---------------------------------------------------------------- pool path


def test_deliberate_enqueues_through_pool_validation(db_conn):
    _insert_soul(db_conn, "s1")
    _store_key("owner_s1", "google")
    p = pool.AgentPool()
    d = _deliberator(p)
    fw = reflex.StubProvider(food=[(120.0, 100.0)])
    summary = asyncio.run(
        p.deliberate("s1", StubVision(), fw, 7, 1000.0, "reflex_rate", d)
    )
    assert summary["status"] == "deliberated"
    assert summary["tier"] == "flash"
    assert len(summary["enqueued"]) == 1
    rows = db_conn.execute(
        "SELECT kind FROM intents WHERE intent_id = ?",
        (summary["enqueued"][0],),
    ).fetchall()
    assert [r["kind"] for r in rows] == ["move_to"]


def test_run_thinks_routes_deliberation(db_conn):
    _insert_soul(db_conn, "s1")
    _insert_soul(db_conn, "s2")
    _store_key("owner_s1", "google")
    p = pool.AgentPool()
    d = _deliberator(p)
    results = p.run_thinks(
        ["s1", "s2"],
        StubVision(),
        reflex.NullProvider(),
        1,
        1000.0,
        deliberate_ids={"s1"},
        escalations={"s1": "reflex_rate"},
        deliberator=d,
    )
    by_id = {r["soul_id"]: r for r in results}
    assert by_id["s1"]["status"] == "deliberated"
    assert by_id["s2"]["status"] == "thought"
    assert _usage_rows(db_conn, "s1")
    assert not _usage_rows(db_conn, "s2")


def test_scheduler_pull_forward_on_escalation():
    sched = scheduler.ThinkScheduler(seed=1)
    tr = deliberation.EscalationTracker()
    tr.bind_scheduler(sched)
    now = 1000.0
    sched._next["s"] = now + 500.0
    sched._last["s"] = now - 120.0
    tr.note_converse_turn("s", now)
    assert sched.next_think("s") < now + 500.0


def test_tracker_seed_stagger_spreads_idle():
    tr = deliberation.EscalationTracker(seed=7)
    souls = ["a", "b", "c", "d"]
    tr.seed_stagger(souls, 1000.0)
    lasts = [tr._last_deliberated[s] for s in souls]
    # Spread over the 5-minute window, not a thundering herd.
    assert max(lasts) - min(lasts) > 60.0
    # None eligible immediately at boot...
    assert not any(tr.idle_eligible(s, 1000.0) for s in souls)
    # ...all eligible one full floor later.
    assert all(tr.idle_eligible(s, 1300.0) for s in souls)


def test_action_menu_prices():
    menu = dict(deliberation.action_menu())
    assert menu["post"] == "20e"
    assert menu["reply"] == "8e"
    assert menu["claim_plot"].startswith("50e")
    assert menu["wait"] == "free"
    assert set(menu) == set(vocab.LEGAL_ACTIONS)
