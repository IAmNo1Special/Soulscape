"""Tests for issue #32: mailbag (soul questions answered by tamer).

Covers every acceptance criterion:
- Cap/TTL enforced; expiry lands in the recap queue as unanswered
- Answer latency pulls next think forward (same mechanism as #31's chirp)
- Loyalty deltas journaled on answer (+0.02) and ignore (-0.01)
- Tray badge count reflects pending questions
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from .. import mailbag
from .. import main
from ..agents import pool as agent_pool
from ..agents import reflex
from ..agents import scheduler as think_scheduler
from ..agents import sensations


NOW = 1_700_000_000.0


def _make_soul(db_conn, soul_id, loyalty=0.5, custodian=None):
    owner = f"owner_{soul_id}"
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, custodian_id, name, essence, "
        "satiety, hydration, hp, max_hp, loyalty, state) "
        "VALUES (?, ?, ?, ?, 100.0, 100.0, 100.0, 100.0, 100.0, ?, 'normal')",
        (soul_id, owner, custodian, f"Soul {soul_id}", loyalty),
    )
    db_conn.commit()


def _journal_types(db_conn):
    rows = db_conn.execute("SELECT type FROM journal").fetchall()
    return [r["type"] for r in rows]


class _StubVision:
    def detail_observations(self, soul_id):
        return []


# ------------------------------------------------------------------ cap/TTL


def test_ask_records_pending_with_48h_ttl(db_conn):
    _make_soul(db_conn, "s1")
    result = mailbag.ask_question("s1", "Do you dream?", NOW, tick_id=1)
    assert result["status"] == "asked"
    row = db_conn.execute(
        "SELECT * FROM mailbag WHERE question_id = ?", (result["question_id"],)
    ).fetchone()
    assert row["soul_id"] == "s1"
    assert row["question"] == "Do you dream?"
    assert row["status"] == "pending"
    assert row["created_at"] == pytest.approx(NOW)
    assert row["expires_at"] == pytest.approx(NOW + 48 * 3600.0)
    assert row["answer"] is None
    types = _journal_types(db_conn)
    assert mailbag.EVENT_MAILBAG_ASKED in types


def test_empty_question_refused(db_conn):
    _make_soul(db_conn, "s1")
    result = mailbag.ask_question("s1", "   ", NOW)
    assert result == {"status": "refused", "reason": "empty_question"}
    assert mailbag.pending_count(db_conn, "s1") == 0


def test_cap_drops_fourth_question_with_journal(db_conn):
    _make_soul(db_conn, "s1")
    for i in range(3):
        r = mailbag.ask_question("s1", f"question {i}", NOW + i, tick_id=1)
        assert r["status"] == "asked"
    fourth = mailbag.ask_question("s1", "question 3", NOW + 3, tick_id=1)
    assert fourth["status"] == "dropped"
    assert fourth["reason"] == "cap"
    assert fourth["cap"] == mailbag.MAILBAG_MAX_PENDING
    assert mailbag.pending_count(db_conn, "s1") == 3
    dropped = db_conn.execute(
        "SELECT payload FROM journal WHERE type = ?",
        (mailbag.EVENT_MAILBAG_CAP_DROPPED,),
    ).fetchall()
    assert len(dropped) == 1
    payload = json.loads(dropped[0]["payload"])
    assert payload["soul_id"] == "s1"
    assert payload["reason"] == "cap"
    assert payload["question"] == "question 3"


def test_cap_is_per_soul(db_conn):
    _make_soul(db_conn, "s1")
    _make_soul(db_conn, "s2")
    for i in range(3):
        mailbag.ask_question("s1", f"q{i}", NOW)
    r = mailbag.ask_question("s2", "other soul question", NOW)
    assert r["status"] == "asked"


def test_sweep_expires_after_48h_archives_and_queues_recap(db_conn):
    _make_soul(db_conn, "s1", loyalty=0.5)
    asked = mailbag.ask_question("s1", "Will you remember me?", NOW, tick_id=1)
    qid = asked["question_id"]
    # Before the TTL: nothing expires.
    assert mailbag.sweep_expired(NOW + 47 * 3600.0, tick_id=2) == []
    assert mailbag.pending_count(db_conn, "s1") == 1
    # Past the TTL: expired, archived (kept), recap row queued.
    expired = mailbag.sweep_expired(NOW + 48 * 3600.0 + 1.0, tick_id=3)
    assert len(expired) == 1
    assert expired[0]["question_id"] == qid
    row = db_conn.execute(
        "SELECT status FROM mailbag WHERE question_id = ?", (qid,)
    ).fetchone()
    assert row["status"] == "expired"
    recap = db_conn.execute(
        "SELECT * FROM recap_sources WHERE kind = ?",
        (mailbag.RECAP_KIND_UNANSWERED,),
    ).fetchall()
    assert len(recap) == 1
    assert recap[0]["soul_id"] == "s1"
    assert recap[0]["ref_id"] == qid
    assert "Will you remember me?" in recap[0]["summary"]
    assert mailbag.pending_count(db_conn, "s1") == 0
    types = _journal_types(db_conn)
    assert mailbag.EVENT_MAILBAG_EXPIRED in types


def test_expiry_decays_loyalty_with_journal_and_memory(db_conn):
    _make_soul(db_conn, "s1", loyalty=0.5)
    mailbag.ask_question("s1", "ignored question", NOW, tick_id=1)
    mailbag.sweep_expired(NOW + 49 * 3600.0, tick_id=2)
    loyalty = db_conn.execute(
        "SELECT loyalty FROM souls WHERE soul_id = 's1'"
    ).fetchone()["loyalty"]
    assert loyalty == pytest.approx(0.49)
    deltas = db_conn.execute(
        "SELECT payload FROM journal WHERE type = ?",
        (mailbag.EVENT_LOYALTY_DELTA,),
    ).fetchall()
    assert len(deltas) == 1
    payload = json.loads(deltas[0]["payload"])
    assert payload["soul_id"] == "s1"
    assert payload["source"] == "mailbag_ignored"
    assert payload["delta"] == pytest.approx(-0.01)
    assert payload["before"] == pytest.approx(0.5)
    assert payload["after"] == pytest.approx(0.49)
    eps = db_conn.execute(
        "SELECT kind, content FROM episodes WHERE soul_id = 's1'"
    ).fetchall()
    assert any(e["kind"] == "intent_outcome" for e in eps)


def test_loyalty_decay_clamped_at_zero(db_conn):
    _make_soul(db_conn, "s1", loyalty=0.005)
    mailbag.ask_question("s1", "ignored", NOW)
    mailbag.sweep_expired(NOW + 49 * 3600.0)
    loyalty = db_conn.execute(
        "SELECT loyalty FROM souls WHERE soul_id = 's1'"
    ).fetchone()["loyalty"]
    assert loyalty == pytest.approx(0.0)


# ------------------------------------------------- answer: latency/forward


def test_answer_records_latency_nudges_loyalty_and_journals(db_conn):
    _make_soul(db_conn, "s1", loyalty=0.5)
    asked = mailbag.ask_question("s1", "What is your favorite color?", NOW)
    result = mailbag.answer_question(
        asked["question_id"], "s1", "Blue, like the deep water.", NOW + 3600.0
    )
    assert result["status"] == "answered"
    assert result["latency_ms"] == pytest.approx(3_600_000.0)
    assert result["loyalty"] == pytest.approx(0.52)
    row = db_conn.execute(
        "SELECT * FROM mailbag WHERE question_id = ?", (asked["question_id"],)
    ).fetchone()
    assert row["status"] == "answered"
    assert row["answer"] == "Blue, like the deep water."
    assert row["answered_at"] == pytest.approx(NOW + 3600.0)
    assert row["answer_latency_ms"] == pytest.approx(3_600_000.0)
    assert mailbag.pending_count(db_conn, "s1") == 0
    loyalty = db_conn.execute(
        "SELECT loyalty FROM souls WHERE soul_id = 's1'"
    ).fetchone()["loyalty"]
    assert loyalty == pytest.approx(0.52)
    deltas = db_conn.execute(
        "SELECT payload FROM journal WHERE type = ?",
        (mailbag.EVENT_LOYALTY_DELTA,),
    ).fetchall()
    assert len(deltas) == 1
    payload = json.loads(deltas[0]["payload"])
    assert payload["source"] == "mailbag_answered"
    assert payload["delta"] == pytest.approx(0.02)
    answered = db_conn.execute(
        "SELECT payload FROM journal WHERE type = ?",
        (mailbag.EVENT_MAILBAG_ANSWERED,),
    ).fetchall()
    assert len(answered) == 1
    assert json.loads(answered[0]["payload"])["latency_ms"] == pytest.approx(
        3_600_000.0
    )
    eps = db_conn.execute(
        "SELECT kind, content FROM episodes WHERE soul_id = 's1'"
    ).fetchall()
    assert any(e["kind"] == "affection" for e in eps)


def test_loyalty_nudge_clamped_at_one(db_conn):
    _make_soul(db_conn, "s1", loyalty=0.995)
    asked = mailbag.ask_question("s1", "q", NOW)
    mailbag.answer_question(asked["question_id"], "s1", "a", NOW + 1.0)
    loyalty = db_conn.execute(
        "SELECT loyalty FROM souls WHERE soul_id = 's1'"
    ).fetchone()["loyalty"]
    assert loyalty == pytest.approx(1.0)


def test_answer_pulls_think_forward_like_chirp(db_conn):
    _make_soul(db_conn, "s1")
    asked = mailbag.ask_question("s1", "Are you awake?", NOW)
    sched = think_scheduler.default()
    sched.forget("s1")
    # Soul thought long ago: next think is far out (jittered ~600 s).
    sched.schedule_next("s1", NOW)
    before = sched.next_think("s1")
    assert before > NOW + 400.0
    mailbag.answer_question(
        asked["question_id"], "s1", "Yes, wide awake.", NOW + 1.0
    )
    after = sched.next_think("s1")
    # Same mechanism as #31's chirp: now + PULL_FORWARD_DELAY (5 s),
    # never violating the 60 s minimum gap.
    assert after == pytest.approx(NOW + 1.0 + think_scheduler.PULL_FORWARD_DELAY)
    assert after < before
    sched.forget("s1")


def test_answer_delivers_priority_observation(db_conn):
    _make_soul(db_conn, "s1")
    asked = mailbag.ask_question("s1", "What do you see?", NOW)
    mailbag.answer_question(asked["question_id"], "s1", "Stars.", NOW + 5.0)
    recent = sensations.recent("s1")
    matches = [
        s for s in recent if s.get("cause") == mailbag.CAUSE_MAILBAG_ANSWER
    ]
    assert matches, "answer observation missing from the sensation ring"
    assert "What do you see?" in matches[-1]["text"]
    assert "Stars." in matches[-1]["text"]


def test_answer_refused_when_not_pending(db_conn):
    _make_soul(db_conn, "s1")
    asked = mailbag.ask_question("s1", "q", NOW)
    ok = mailbag.answer_question(asked["question_id"], "s1", "a", NOW + 1.0)
    assert ok["status"] == "answered"
    again = mailbag.answer_question(asked["question_id"], "s1", "b", NOW + 2.0)
    assert again == {"status": "refused", "reason": "already_answered"}


def test_answer_rejects_soul_mismatch(db_conn):
    _make_soul(db_conn, "s1")
    _make_soul(db_conn, "s2")
    asked = mailbag.ask_question("s1", "q", NOW)
    result = mailbag.answer_question(asked["question_id"], "s2", "a", NOW + 1.0)
    assert result == {"status": "refused", "reason": "soul_mismatch"}


def test_ask_fans_unsolicited_question_bubble(monkeypatch, db_conn):
    _make_soul(db_conn, "s1")
    calls = []
    from .. import viewport

    def _fake(owner_id, soul_id, text, kind="speech", solicited=False, payload=None):
        calls.append(
            {
                "owner_id": owner_id,
                "soul_id": soul_id,
                "text": text,
                "kind": kind,
                "solicited": solicited,
                "payload": payload,
            }
        )
        return 0

    monkeypatch.setattr(viewport.viewport, "notify_bubble", _fake)
    mailbag.ask_question("s1", "Do you get lonely?", NOW)
    assert len(calls) == 1
    bubble = calls[0]
    assert bubble["owner_id"] == "owner_s1"
    assert bubble["kind"] == mailbag.BUBBLE_KIND_MAILBAG
    # Unsolicited: subject to the client's noise caps (documented).
    assert bubble["solicited"] is False
    assert "lonely" in bubble["text"]
    # The question_id rides the payload so the client tap handler can
    # open the right question in the answer surface.
    assert bubble["payload"] == {
        "question_id": mailbag.pending_for_owner(db_conn, "owner_s1")[0][
            "question_id"
        ]
    }


# ------------------------------------------------- deliberation emission


def _pool_soul_row(db_conn, soul_id):
    return dict(
        db_conn.execute(
            "SELECT soul_id, position, velocity, nature, state, "
            "COALESCE(essence, 0.0) AS essence, COALESCE(satiety, 100.0) AS satiety, "
            "COALESCE(hydration, 100.0) AS hydration, COALESCE(hp, 100.0) AS hp, "
            "COALESCE(max_hp, 100.0) AS max_hp, COALESCE(loyalty, 0.5) AS loyalty, "
            "custodian_id, owner_id FROM souls WHERE soul_id = ?",
            (soul_id,),
        ).fetchone()
    )


class _QuestionDeliberator:
    """Fake deliberator: one successful deliberation carrying a question."""

    def __init__(self, question):
        self.question = question

    def deliberate(self, soul_id, row, vision, provider, now, escalation, tick_id):
        return {
            "status": "deliberated",
            "tier": "flash",
            "intents": [("wait", {})],
            "rationale": "resting",
            "question": self.question,
            "fallback_used": False,
            "trace_id": None,
        }


def test_deliberated_question_reaches_mailbag(db_conn):
    _make_soul(db_conn, "s1")
    p = agent_pool.AgentPool()
    summary = asyncio.run(
        p.deliberate(
            "s1",
            _StubVision(),
            reflex.NullProvider(),
            7,
            NOW,
            "idle",
            deliberator=_QuestionDeliberator("What is the sky?"),
        )
    )
    assert summary["status"] == "deliberated"
    assert summary["mailbag"]["status"] == "asked"
    assert mailbag.pending_count(db_conn, "s1") == 1


def test_deliberated_question_respects_cap(db_conn):
    _make_soul(db_conn, "s1")
    for i in range(3):
        mailbag.ask_question("s1", f"existing {i}", NOW)
    p = agent_pool.AgentPool()
    summary = asyncio.run(
        p.deliberate(
            "s1",
            _StubVision(),
            reflex.NullProvider(),
            7,
            NOW,
            "idle",
            deliberator=_QuestionDeliberator("one too many"),
        )
    )
    assert summary["mailbag"]["status"] == "dropped"
    assert mailbag.pending_count(db_conn, "s1") == 3


def test_blank_question_emits_nothing(db_conn):
    _make_soul(db_conn, "s1")
    p = agent_pool.AgentPool()
    summary = asyncio.run(
        p.deliberate(
            "s1",
            _StubVision(),
            reflex.NullProvider(),
            7,
            NOW,
            "idle",
            deliberator=_QuestionDeliberator("   "),
        )
    )
    assert "mailbag" not in summary
    assert mailbag.pending_count(db_conn, "s1") == 0


def test_heuristic_fallback_never_asks(db_conn):
    """The deterministic fallback policy never asks questions (#32
    decision): even a 'question' on a heuristic result stays silent."""
    _make_soul(db_conn, "s1")
    p = agent_pool.AgentPool()

    class _Heuristic:
        def deliberate(self, soul_id, row, vision, provider, now, escalation, tick_id):
            return {
                "status": "heuristic",
                "intents": [],
                "fallback_used": True,
                "question": "a degraded brain should not ask this",
            }

    summary = asyncio.run(
        p.deliberate(
            "s1",
            _StubVision(),
            reflex.NullProvider(),
            7,
            NOW,
            "idle",
            deliberator=_Heuristic(),
        )
    )
    assert summary["deliberation"] == "heuristic_fallback"
    assert mailbag.pending_count(db_conn, "s1") == 0


def test_real_heuristic_path_has_no_question_key(db_conn):
    """The real #25 heuristic fallback returns no question at all."""
    from ..agents import deliberation

    _make_soul(db_conn, "s1")
    d = deliberation.Deliberator(
        agent_pool.AgentPool(),
        tracker=deliberation.EscalationTracker(),
        call_provider=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = d.deliberate(
        "s1",
        _pool_soul_row(db_conn, "s1"),
        _StubVision(),
        reflex.NullProvider(),
        NOW,
        "idle",
        tick_id=1,
    )
    assert result["status"] == "heuristic"
    assert result.get("question") in (None, "")


# ------------------------------------------------- REST endpoints


def _register_tamer():
    c = TestClient(main.app)
    r = c.post(
        "/tamers/register",
        json={"username": "mb_tamer", "password": "s3cur3pass"},
    )
    assert r.status_code == 201, r.text
    tamer_id = r.json()["tamer_id"]
    r = c.post(
        "/tamers/login", json={"username": "mb_tamer", "password": "s3cur3pass"}
    )
    assert r.status_code == 200, r.text
    return tamer_id, r.json()["token"]


def test_endpoints_count_pending_answer(client, db_conn, register_soul):
    register_soul("s1", essence=100.0)
    asked = mailbag.ask_question("s1", "REST question?", NOW)
    count = client.get("/mailbag/count")
    assert count.status_code == 200
    assert count.json() == {"pending": 1}
    pending = client.get("/mailbag/pending")
    assert pending.status_code == 200
    questions = pending.json()["questions"]
    assert len(questions) == 1
    assert questions[0]["question_id"] == asked["question_id"]
    assert questions[0]["soul_name"] == "Test Soul"
    resp = client.post(
        f"/mailbag/{asked['question_id']}/answer", json={"answer": "REST answer"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "answered"
    assert "latency_ms" in resp.json()
    count = client.get("/mailbag/count")
    assert count.json() == {"pending": 0}


def test_answer_endpoint_custody_allowed(client, db_conn, register_soul):
    register_soul("s1", essence=100.0)
    tamer_id, token = _register_tamer()
    # Give the tamer custody of the soul (as a transfer would).
    db_conn.execute(
        "UPDATE souls SET custodian_id = ? WHERE soul_id = 's1'", (tamer_id,)
    )
    db_conn.commit()
    asked = mailbag.ask_question("s1", "custody question?", NOW)
    tamer_client = TestClient(main.app, headers={"X-Hub-Secret": token})
    ok = tamer_client.post(
        f"/mailbag/{asked['question_id']}/answer", json={"answer": "mine to answer"}
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "answered"


def test_answer_endpoint_stranger_denied(client, db_conn, register_soul):
    register_soul("s1", essence=100.0)
    tamer_id, token = _register_tamer()
    db_conn.execute(
        "UPDATE souls SET custodian_id = ? WHERE soul_id = 's1'", (tamer_id,)
    )
    db_conn.commit()
    c = TestClient(main.app)
    r = c.post(
        "/tamers/register",
        json={"username": "mb_stranger", "password": "s3cur3pass"},
    )
    assert r.status_code == 201
    r = c.post(
        "/tamers/login",
        json={"username": "mb_stranger", "password": "s3cur3pass"},
    )
    stranger_token = r.json()["token"]
    asked = mailbag.ask_question("s1", "private question?", NOW)
    stranger_client = TestClient(
        main.app, headers={"X-Hub-Secret": stranger_token}
    )
    denied = stranger_client.post(
        f"/mailbag/{asked['question_id']}/answer", json={"answer": "not mine"}
    )
    assert denied.status_code == 403
    # The question is still pending.
    assert mailbag.pending_count(db_conn, "s1") == 1


def test_count_scoped_to_custody(db_conn, register_soul):
    register_soul("s1", essence=100.0)
    tamer_id, token = _register_tamer()
    db_conn.execute(
        "UPDATE souls SET custodian_id = ? WHERE soul_id = 's1'", (tamer_id,)
    )
    db_conn.commit()
    mailbag.ask_question("s1", "scoped?", NOW)
    tamer_client = TestClient(main.app, headers={"X-Hub-Secret": token})
    assert tamer_client.get("/mailbag/count").json() == {"pending": 1}


def test_soul_user_sees_only_own_questions(client, db_conn, register_soul):
    register_soul("s1", secret="soulsecret_one_1234")
    register_soul("s2", secret="soulsecret_two_1234")
    mailbag.ask_question("s1", "question from s1?", NOW)
    mailbag.ask_question("s2", "question from s2?", NOW)
    soul_client = TestClient(
        main.app, headers={"X-Hub-Secret": "soulsecret_one_1234"}
    )
    resp = soul_client.get("/mailbag/pending")
    assert resp.status_code == 200
    assert [q["soul_id"] for q in resp.json()["questions"]] == ["s1"]
    assert soul_client.get("/mailbag/count").json()["pending"] == 1


def test_answer_missing_question_404(client):
    resp = client.post(
        "/mailbag/q_nope/answer", json={"answer": "hello"}
    )
    assert resp.status_code == 404


# ------------------------------------------------------- world-tick sweep
def test_world_tick_sweeps_mailbag_on_cadence(db_conn, register_soul, monkeypatch):
    register_soul("s1", essence=100.0)
    calls = []
    monkeypatch.setattr(
        mailbag,
        "sweep_expired",
        lambda now, tick_id=0: calls.append(tick_id),
    )
    from ..world_tick import WorldTick

    period = mailbag.MAILBAG_SWEEP_EVERY_TICKS
    tick = WorldTick()
    tick.tick_id = period - 1
    tick.step()
    assert calls == []
    tick.step()
    assert calls == [period]


def test_world_tick_sweep_failure_does_not_kill_tick(
    db_conn, register_soul, monkeypatch
):
    register_soul("s1", essence=100.0)

    def _boom(now, tick_id=0):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(mailbag, "sweep_expired", _boom)
    from ..world_tick import WorldTick

    tick = WorldTick()
    tick.tick_id = 300
    tick.step()  # tick survives; the sweep error is logged, not raised
    assert tick.tick_id == 301
