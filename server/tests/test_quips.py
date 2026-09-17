"""Tests for issue #30: quip budget endpoint, biology viewport stream,
bubble ops."""

import json

import pytest

from .. import quips
from .. import viewport as vp


def _insert_soul(db_conn, soul_id, essence=100.0, **kw):
    cols = {
        "soul_id": soul_id,
        "owner_id": f"owner_{soul_id}",
        "position": json.dumps([100.0, 200.0]),
        "velocity": json.dumps([0.0, 0.0]),
        "essence": essence,
        "satiety": 100.0,
        "hydration": 100.0,
        "hp": 100.0,
        "max_hp": 100.0,
        "state": "normal",
        "name": "TestSoul",
    }
    cols.update(kw)
    names = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    db_conn.execute(
        f"INSERT INTO souls ({names}) VALUES ({placeholders})",
        tuple(cols.values()),
    )
    db_conn.commit()


def _essence(db_conn, soul_id):
    row = db_conn.execute(
        "SELECT COALESCE(essence, 0.0) AS e FROM souls WHERE soul_id = ?",
        (soul_id,),
    ).fetchone()
    return float(row["e"])


class TestQuipBudgetEndpoint:
    def test_three_quips_succeed_fourth_rejected(self, client, db_conn):
        _insert_soul(db_conn, "quip1")
        for i in range(3):
            resp = client.post("/souls/quip1/quip", json={})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "success"
            assert body["quip"]
            assert body["essence_debited"] == quips.QUIP_PRICE_ESSENCE
            assert body["quips_used_today"] == i + 1
            assert body["quips_remaining_today"] == 2 - i
            assert body["fallback_used"] is True
        assert _essence(db_conn, "quip1") == pytest.approx(
            100.0 - 3 * quips.QUIP_PRICE_ESSENCE
        )
        resp = client.post("/souls/quip1/quip", json={})
        assert resp.status_code == 429
        detail = resp.json()["detail"]
        assert detail["reason"] == "budget_exhausted"
        assert detail["quips_remaining_today"] == 0
        assert "resets_at" in detail
        assert _essence(db_conn, "quip1") == pytest.approx(
            100.0 - 3 * quips.QUIP_PRICE_ESSENCE
        )

    def test_insufficient_essence_rejected_402(self, client, db_conn):
        _insert_soul(db_conn, "quip2", essence=1.0)
        resp = client.post("/souls/quip2/quip", json={})
        assert resp.status_code == 402
        detail = resp.json()["detail"]
        assert detail["reason"] == "insufficient_essence"
        assert detail["price"] == quips.QUIP_PRICE_ESSENCE
        assert _essence(db_conn, "quip2") == pytest.approx(1.0)

    def test_day_boundary_resets_budget(self, client, db_conn, monkeypatch):
        _insert_soul(db_conn, "quip3")
        for _ in range(3):
            assert client.post("/souls/quip3/quip", json={}).status_code == 200
        assert client.post("/souls/quip3/quip", json={}).status_code == 429
        monkeypatch.setattr(quips, "_today_utc", lambda: "2026-09-18")
        resp = client.post("/souls/quip3/quip", json={})
        assert resp.status_code == 200
        assert resp.json()["quips_remaining_today"] == 2

    def test_unknown_soul_404(self, client, db_conn):
        resp = client.post("/souls/nope/quip", json={})
        assert resp.status_code == 404

    def test_ledger_and_usage_rows_written(self, client, db_conn):
        _insert_soul(db_conn, "quip4")
        assert client.post("/souls/quip4/quip", json={}).status_code == 200
        ledger = db_conn.execute(
            "SELECT entry_type, amount FROM ledger WHERE soul_id = ?",
            ("quip4",),
        ).fetchall()
        debits = [r for r in ledger if r["entry_type"] == "quip_debit"]
        assert len(debits) == 1
        assert debits[0]["amount"] == pytest.approx(-quips.QUIP_PRICE_ESSENCE)
        usage = db_conn.execute(
            "SELECT tier, fallback_used FROM llm_usage WHERE soul_id = ?",
            ("quip4",),
        ).fetchall()
        assert len(usage) == 1
        assert usage[0]["tier"] == "flash"
        assert usage[0]["fallback_used"] == 1

    def test_quip_with_prompt_accepted(self, client, db_conn):
        _insert_soul(db_conn, "quip5")
        resp = client.post(
            "/souls/quip5/quip", json={"prompt": "brag about your aura"}
        )
        assert resp.status_code == 200
        assert resp.json()["quip"]


class TestQuipBudgetPure:
    def test_count_and_remaining(self, db_conn):
        cursor = db_conn
        assert quips.get_quip_count(cursor, "s9") == 0
        assert quips.quips_remaining(cursor, "s9") == 3
        quips.record_quip(cursor, "s9")
        quips.record_quip(cursor, "s9")
        db_conn.commit()
        assert quips.get_quip_count(cursor, "s9") == 2
        assert quips.quips_remaining(cursor, "s9") == 1

    def test_release_quip_frees_slot(self, db_conn):
        cursor = db_conn
        day = quips.quip_day()
        quips.record_quip(cursor, "s9", day)
        quips.record_quip(cursor, "s9", day)
        db_conn.commit()
        assert quips.release_quip(cursor, "s9", day) == 1
        db_conn.commit()
        assert quips.quips_remaining(cursor, "s9") == 2
        assert quips.release_quip(cursor, "s9", day) == 0
        db_conn.commit()
        assert quips.quips_remaining(cursor, "s9") == 3
        # Releasing a nonexistent reservation is a no-op.
        assert quips.release_quip(cursor, "s9", day) == 0

    def test_generate_quip_fallback_without_keys(self):
        gen = quips.generate_quip(
            {"soul_id": "s", "name": "Zed", "owner_id": "o",
             "custodian_id": None, "species": None},
            rng=__import__("random").Random(0),
        )
        assert gen["fallback_used"] is True
        assert "Zed" in gen["text"]
        assert len(gen["text"]) <= quips.QUIP_MAX_CHARS

    def test_generate_quip_uses_provider_chain(self, monkeypatch):
        from .. import key_vault

        calls = []

        def fake_call(provider, model, prompt, timeout_s, key):
            calls.append((provider, model))
            return '  "A quip!"  '

        nonce, ciphertext = key_vault.encrypt_key("fake-google-key-value")
        key_vault.store_key(
            "tamer_q", key_vault.new_key_id(), "google", "test", "1234",
            nonce, ciphertext,
        )
        gen = quips.generate_quip(
            {"soul_id": "s", "name": "Zed", "owner_id": "tamer_q",
             "custodian_id": None, "species": None},
            provider_call=fake_call,
        )
        assert gen["fallback_used"] is False
        assert gen["text"] == "A quip!"
        assert gen["model"] == "gemini-2.5-flash"
        assert calls


class TestBiologyViewportStream:
    def test_read_biology(self, db_conn):
        _insert_soul(
            db_conn, "bio1", satiety=30.0, hydration=40.0, hp=50.0, max_hp=120.0
        )
        bio = vp.read_biology()
        assert bio["bio1"] == (30.0, 40.0, 50.0, 120.0)

    def test_diff_biology_emits_on_change(self):
        ops = vp.diff_biology({"s1": (30.0, 40.0, 50.0, 100.0)}, {})
        assert len(ops) == 1
        op, domain = ops[0]
        assert domain == "priority"
        assert op["domain"] == "biology"
        assert op["state"]["satiety"] == 30.0
        assert op["state"]["hp"] == 50.0

    def test_diff_biology_ignores_float_noise(self):
        ops = vp.diff_biology(
            {"s1": (30.04, 40.0, 50.0, 100.0)},
            {"s1": (30.0, 40.0, 50.0, 100.0)},
        )
        assert ops == []

    def test_snapshot_carries_biology(self, db_conn):
        _insert_soul(
            db_conn, "bio2", satiety=11.0, hydration=22.0, hp=33.0, max_hp=44.0
        )
        manager = vp.ViewportManager()
        session = manager.create("owner_bio2")
        from shared import protocol

        frame = vp.build_snapshot(session, protocol.SnapReason.JOIN, 1)
        entry = next(s for s in frame["souls"] if s["soul_id"] == "bio2")
        assert entry["satiety"] == 11.0
        assert entry["hydration"] == 22.0
        assert entry["hp"] == 33.0
        assert entry["max_hp"] == 44.0
        assert session.committed_biology["bio2"] == (11.0, 22.0, 33.0, 44.0)


class TestBubbleOps:
    def test_notify_bubble_enqueued_as_priority(self):
        manager = vp.ViewportManager()
        session = manager.create("owner_b")
        assert manager.notify_bubble("owner_b", "s1", "hello", kind="quip",
                                     solicited=True) == 1
        assert len(session.pending_priority) == 1
        op = session.pending_priority[0]
        assert op["op"] == "bubble"
        assert op["soul_id"] == "s1"
        assert op["text"] == "hello"
        assert op["kind"] == "quip"
        assert op["solicited"] is True

    def test_notify_bubble_no_sessions(self):
        manager = vp.ViewportManager()
        assert manager.notify_bubble("ghost", "s1", "hi") == 0

    @pytest.mark.anyio
    async def test_bubble_op_flushed_but_not_replayed(self):
        manager = vp.ViewportManager()
        session = manager.create("owner_c")
        manager.notify_bubble("owner_c", "s1", "hi there")
        sent = []

        async def send(frame):
            sent.append(frame)

        result = await vp.flush(session, {}, 1, send)
        assert result == "ok"
        ops = sent[0]["ops"]
        assert any(o.get("op") == "bubble" for o in ops)
        assert session.committed_biology == {}
