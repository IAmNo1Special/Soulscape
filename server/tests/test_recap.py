"""Tests for the ambient recap (issue #33).

Covers every acceptance criterion with real evidence:

- Scripted-day integration: a full synthetic day (petting loyalty
  delta, mailbag Q&A, metering debit, soul_woke, an unanswered
  mailbag expiry) -> recap cites the real journaled events by id/text
  and the mailbag count is right. Note: journal ``created_at`` is
  wall-clock in the mailbag writers (they take ``now`` for domain
  timestamps but append_event stamps time.time()), so the script
  re-times journal rows onto the synthetic day after driving the real
  code paths -- the rows and payloads themselves are real.
- Once per unlock cycle: fake unlock -> bubble; second unlock (no new
  recap) -> no bubble; new-day recap -> bubble again. Simulated clock
  throughout (``now`` is injected into on_unlock).
- Retention compaction: 40 synthetic days -> bounded rows, salient
  entries survive verbatim, low-salience merged into per-day rollups,
  snapshot recovery tails stay gapless.
- Determinism: same journal twice -> identical lines.
- REST: GET /souls/{soul_id}/recaps (browse, newest first),
  404 for unknown souls, 403 cross-custody.
- Wiring: presence adjudication on an unlock event calls on_unlock.
"""

import json
import time

from fastapi.testclient import TestClient

from .. import database
from .. import intents
from .. import mailbag
from .. import persistence
from .. import presence as presence_module
from .. import recap
from .. import viewport


T0 = 1_750_000_000.0
DAY = 86400.0


def _insert_soul(db_conn, soul_id, custodian, name="TestSoul", loyalty=0.5):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, custodian_id, name, essence, "
        "satiety, hydration, hp, max_hp, loyalty, state) "
        "VALUES (?, ?, ?, ?, 100.0, 100.0, 100.0, 100.0, 100.0, ?, 'normal')",
        (soul_id, custodian, custodian, name, loyalty),
    )
    db_conn.commit()


def _retime_journal(db_conn, base, step=3600.0):
    """Lay journal rows onto the synthetic day, one ``step`` apart."""
    rows = db_conn.execute("SELECT seq FROM journal ORDER BY seq ASC").fetchall()
    for i, row in enumerate(rows):
        db_conn.execute(
            "UPDATE journal SET created_at = ? WHERE seq = ?",
            (base + i * step, row["seq"]),
        )
    db_conn.commit()
    return [r["seq"] for r in rows]


def _script_day(db_conn, soul_id, base):
    """Drive the real code paths for one synthetic day. Returns context."""
    q1 = mailbag.ask_question(soul_id, "What do you dream about?", base + 3600.0)
    assert q1["status"] == "asked"
    ans = mailbag.answer_question(
        q1["question_id"], soul_id, "Electric sheep.", base + 7200.0
    )
    assert ans["status"] == "answered"
    q2 = mailbag.ask_question(soul_id, "Will you remember this?", base + 3 * 3600.0)
    assert q2["status"] == "asked"
    # Expire q2 inside the synthetic day: pull its TTL into the day,
    # then run the real expiry sweep.
    db_conn.execute(
        "UPDATE mailbag SET expires_at = ? WHERE question_id = ?",
        (base + 4 * 3600.0, q2["question_id"]),
    )
    db_conn.commit()
    expired = mailbag.sweep_expired(base + 5 * 3600.0, tick_id=7)
    assert [e["question_id"] for e in expired] == [q2["question_id"]]
    # Petting: real loyalty-delta path.
    with database.get_db() as conn:
        mailbag._apply_loyalty_delta(
            conn, soul_id, 0.03, "petting", base + 6 * 3600.0, 7
        )
        conn.commit()
    # Metering debit + wake: real event types / payload shapes, written
    # directly (their writers need heavy tick setup).
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        persistence.append_event(
            conn,
            7,
            "metering_debit_settled",
            {
                "soul_id": soul_id,
                "batch_id": "b_day",
                "debited_essence": 1.25,
                "essence_after": 98.75,
            },
        )
        persistence.append_event(
            conn,
            7,
            "soul_woke",
            {"soul_id": soul_id, "essence_before": 0.0, "essence_after": 5.0},
        )
        # Quips have no journal writer yet; the scripted quip row is a
        # real journal row so the recap can cite it.
        persistence.append_event(
            conn, 7, "quip", {"soul_id": soul_id, "text": "I am the storm."}
        )
        conn.commit()
    _retime_journal(db_conn, base)
    return {"q1": q1, "q2": q2}


class TestScriptedDayIntegration:
    def test_recap_cites_real_journaled_events(self, db_conn):
        _insert_soul(db_conn, "s1", "tamer1")
        _script_day(db_conn, "s1", T0)
        rec = recap.maybe_generate_recap("s1", now=T0 + 25 * 3600.0)
        assert rec is not None
        assert len(rec["lines"]) <= recap.RECAP_MAX_LINES
        assert len(rec["lines"]) > 0
        # Every cite resolves to a real row.
        journal_seqs = {
            r["seq"]
            for r in db_conn.execute("SELECT seq, type FROM journal").fetchall()
        }
        source_ids = {
            r["source_id"]
            for r in db_conn.execute("SELECT source_id FROM recap_sources").fetchall()
        }
        cited_events = set()
        for line in rec["lines"]:
            assert line["text"]
            for cite in line["cites"]:
                if cite["type"] == "journal":
                    assert cite["seq"] in journal_seqs
                    row = db_conn.execute(
                        "SELECT type FROM journal WHERE seq = ?", (cite["seq"],)
                    ).fetchone()
                    assert row["type"] == cite["event"]
                    cited_events.add(cite["event"])
                else:
                    assert cite["source_id"] in source_ids
        # The day's salient events are actually cited.
        assert "loyalty_delta" in cited_events
        assert "mailbag_answered" in cited_events
        # The unanswered question is cited via its recap_sources row.
        unanswered_cites = [
            cite
            for line in rec["lines"]
            for cite in line["cites"]
            if cite.get("kind") == "mailbag_unanswered"
        ]
        assert len(unanswered_cites) == 1
        src = db_conn.execute(
            "SELECT summary FROM recap_sources WHERE source_id = ?",
            (unanswered_cites[0]["source_id"],),
        ).fetchone()
        assert "Will you remember this?" in src["summary"]

    def test_recap_generation_is_journaled_with_mailbag_count(self, db_conn):
        _insert_soul(db_conn, "s1", "tamer1")
        _script_day(db_conn, "s1", T0)
        recap.maybe_generate_recap("s1", now=T0 + 25 * 3600.0)
        rows = db_conn.execute(
            "SELECT payload FROM journal WHERE type = ?",
            (recap.EVENT_RECAP_GENERATED,),
        ).fetchall()
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"])
        assert payload["soul_id"] == "s1"
        assert payload["n_lines"] > 0
        assert payload["unanswered_mailbag"] == 1
        assert any("Will you remember this?" in t for t in payload["unanswered_texts"])
        assert len(payload["cited"]) >= payload["n_lines"]

    def test_morning_note_lines_and_mailbag_count(self, db_conn, monkeypatch):
        _insert_soul(db_conn, "s1", "tamer1", name="Zed")
        _script_day(db_conn, "s1", T0)
        rec = recap.maybe_generate_recap("s1", now=T0 + 25 * 3600.0)
        calls = []
        monkeypatch.setattr(
            viewport.viewport,
            "notify_bubble",
            lambda o, s, t, kind="speech", solicited=False, payload=None: (
                calls.append((o, s, t, kind, solicited)),
                1,
            )[1],
        )
        note = recap.maybe_morning_note("s1", now=T0 + 25 * 3600.0 + 10)
        assert note is not None
        assert note["recap_id"] == rec["recap_id"]
        assert len(note["lines"]) <= 3
        assert len(calls) == 1
        owner_id, soul_id, text, kind, solicited = calls[0]
        assert soul_id == "s1"
        assert kind == recap.BUBBLE_KIND_MORNING_NOTE
        assert solicited is True
        assert "mailbag: 1 unanswered question" in text
        # shown_at is set: the recap is no longer fresh.
        row = db_conn.execute(
            "SELECT shown_at FROM recaps WHERE recap_id = ?", (rec["recap_id"],)
        ).fetchone()
        assert row["shown_at"] is not None

    def test_quiet_night_still_gets_an_honest_line(self, db_conn, monkeypatch):
        _insert_soul(db_conn, "s1", "tamer1", name="Zed")
        rec = recap.maybe_generate_recap("s1", now=T0)
        assert rec is not None
        assert rec["lines"] == []
        calls = []
        monkeypatch.setattr(
            viewport.viewport,
            "notify_bubble",
            lambda o, s, t, kind="speech", solicited=False, payload=None: (
                calls.append(t),
                0,
            )[1],
        )
        note = recap.maybe_morning_note("s1", now=T0 + 10)
        assert note is not None
        assert note["lines"] == ["quiet night Zed — nothing new"]

    def test_generation_is_deterministic(self, db_conn):
        _insert_soul(db_conn, "s1", "tamer1")
        _script_day(db_conn, "s1", T0)
        first = recap.maybe_generate_recap("s1", now=T0 + 25 * 3600.0)
        lines_a = [line["text"] for line in first["lines"]]
        db_conn.execute("DELETE FROM recaps")
        db_conn.commit()
        second = recap.maybe_generate_recap("s1", now=T0 + 25 * 3600.0)
        lines_b = [line["text"] for line in second["lines"]]
        assert lines_a == lines_b

    def test_no_regeneration_within_22h(self, db_conn):
        _insert_soul(db_conn, "s1", "tamer1")
        first = recap.maybe_generate_recap("s1", now=T0)
        assert first is not None
        assert recap.maybe_generate_recap("s1", now=T0 + 21 * 3600.0) is None
        second = recap.maybe_generate_recap("s1", now=T0 + 23 * 3600.0)
        assert second is not None
        assert second["recap_id"] != first["recap_id"]


class TestOncePerUnlockCycle:
    def _unlock_setup(self, db_conn, monkeypatch):
        _insert_soul(db_conn, "s1", "tamer1", name="Zed")
        presence_module.record_presence(
            "tamer1",
            {"presence": "locked", "idle_bucket": "30+"},
            now=T0 - 3600.0,
        )
        calls = []
        monkeypatch.setattr(
            viewport.viewport,
            "notify_bubble",
            lambda o, s, t, kind="speech", solicited=False, payload=None: (
                calls.append((s, t, kind)),
                1,
            )[1],
        )
        return calls

    def test_unlock_bubble_then_no_repeat_then_new_day(self, db_conn, monkeypatch):
        calls = self._unlock_setup(db_conn, monkeypatch)
        emitted = recap.on_unlock("tamer1", now=T0)
        assert len(emitted) == 1
        assert emitted[0]["soul_id"] == "s1"
        assert len(calls) == 1
        # Second unlock, no new recap: no repeat.
        emitted = recap.on_unlock("tamer1", now=T0 + 60.0)
        assert emitted == []
        assert len(calls) == 1
        # New day: fresh recap -> bubble again.
        emitted = recap.on_unlock("tamer1", now=T0 + 23 * 3600.0)
        assert len(emitted) == 1
        assert len(calls) == 2

    def test_sweep_skips_quiet_souls_silently(self, db_conn):
        # The tick backstop must not journal for souls with nothing to
        # say -- an idle tick stays journal-silent (persistence tests
        # rely on this).
        _insert_soul(db_conn, "s1", "tamer1")
        made = recap.sweep_recaps(now=T0)
        assert made == 0
        n = db_conn.execute("SELECT COUNT(*) AS n FROM recaps").fetchone()["n"]
        assert n == 0
        assert persistence.journal_head(db_conn) == 0

    def test_unlock_path_still_notes_a_quiet_night(self, db_conn, monkeypatch):
        self._unlock_setup(db_conn, monkeypatch)
        emitted = recap.on_unlock("tamer1", now=T0)
        assert len(emitted) == 1
        assert emitted[0]["lines"] == ["quiet night Zed — nothing new"]
        emitted = recap.on_unlock("tamer1", now=T0 + 120.0)
        assert emitted == []

    def test_presence_adjudication_wires_unlock_to_on_unlock(
        self, db_conn, monkeypatch
    ):
        _insert_soul(db_conn, "s1", "tamer1")
        seen = []
        monkeypatch.setattr(
            recap, "on_unlock", lambda tamer_id, now=None: seen.append(tamer_id)
        )
        intents.enqueue_intent(
            "sess1",
            "nonce1",
            "tamer1",
            "s1",
            presence_module.KIND_TAMER_PRESENCE,
            {"presence": "active", "idle_bucket": "0-5", "event": "unlock"},
        )
        intent = intents.get_intent_by_nonce("sess1", "nonce1")
        assert intent is not None
        presence_module.adjudicate_presence_intent(None, intent)
        assert seen == ["tamer1"]
        # A non-unlock report does not trigger the hook.
        seen.clear()
        intents.enqueue_intent(
            "sess1",
            "nonce2",
            "tamer1",
            "s1",
            presence_module.KIND_TAMER_PRESENCE,
            {"presence": "active", "idle_bucket": "0-5"},
        )
        intent = intents.get_intent_by_nonce("sess1", "nonce2")
        assert intent is not None
        presence_module.adjudicate_presence_intent(None, intent)
        assert seen == []


class TestRetentionCompaction:
    def _seed_40_days(self, db_conn, now):
        import json as _json

        seqs_high = []
        for day in range(40):
            base = now - (40 - day) * DAY
            for i in range(10):
                db_conn.execute(
                    "INSERT INTO journal (tick_id, type, payload, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        day,
                        "metering_debit_settled",
                        _json.dumps({"soul_id": "s1", "debited_essence": 0.1}),
                        base + i,
                    ),
                )
            for i in range(2):
                cur = db_conn.execute(
                    "INSERT INTO journal (tick_id, type, payload, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        day,
                        "loyalty_delta",
                        _json.dumps(
                            {
                                "soul_id": "s1",
                                "source": "petting",
                                "delta": 0.03,
                                "before": 0.5,
                                "after": 0.53,
                            }
                        ),
                        base + 100 + i,
                    ),
                )
                seqs_high.append(cur.lastrowid)
            for i in range(2):
                db_conn.execute(
                    "INSERT INTO recap_sources (kind, soul_id, ref_id, summary, "
                    "created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        "mailbag_unanswered",
                        "s1",
                        f"q_{day}_{i}",
                        f"Unanswered after 48h: question {i}",
                        base + 200 + i,
                    ),
                )
        db_conn.commit()
        return seqs_high

    def test_compaction_bounds_storage_and_preserves_salient(self, db_conn):
        now = time.time()
        seqs_high = self._seed_40_days(db_conn, now)
        before_journal = db_conn.execute("SELECT COUNT(*) c FROM journal").fetchone()[
            "c"
        ]
        assert before_journal == 40 * 12
        report = recap.compact_retention(now)
        # Days 0-9 are older than 30 d: 10 days x 12 rows -> 10 rollups + 20 high.
        # Days 10-39 untouched: 30 x 12 = 360 rows.
        after = db_conn.execute("SELECT COUNT(*) c FROM journal").fetchone()["c"]
        assert after == 10 + 20 + 360
        assert report["journal"]["rollups"] == 10
        assert report["journal"]["deleted"] == 100
        rollups = db_conn.execute(
            "SELECT payload FROM journal WHERE type = ?",
            (recap.EVENT_JOURNAL_ROLLUP,),
        ).fetchall()
        assert len(rollups) == 10
        for row in rollups:
            payload = json.loads(row["payload"])
            assert payload["n"] == 10
            assert payload["types"] == {"metering_debit_settled": 10}
            assert len(payload["rolled_seqs"]) == 10
            assert len(payload["samples"]) == 3
        # All 20 compacted high-salience rows survive verbatim.
        for seq in seqs_high[:20]:
            row = db_conn.execute(
                "SELECT type, payload FROM journal WHERE seq = ?", (seq,)
            ).fetchone()
            assert row is not None and row["type"] == "loyalty_delta"
            assert json.loads(row["payload"])["source"] == "petting"
        # recap_sources: days 0-9 -> 10 rollups; days 10-39 kept (60 rows).
        src_after = db_conn.execute("SELECT COUNT(*) c FROM recap_sources").fetchone()[
            "c"
        ]
        assert src_after == 10 + 60
        assert report["recap_sources"]["rollups"] == 10
        assert report["recap_sources"]["deleted"] == 20

    def test_compaction_never_breaks_snapshot_recovery_tail(self, db_conn):
        now = time.time()
        self._seed_40_days(db_conn, now)
        head = persistence.journal_head(db_conn)
        snap_seq = head - 50
        db_conn.execute(
            "INSERT INTO snapshots (tick_id, journal_seq, blob, created_at) "
            "VALUES (?, ?, ?, ?)",
            (1, snap_seq, b"blob", now),
        )
        db_conn.commit()
        recap.compact_retention(now)
        assert persistence.journal_continuous(
            db_conn, snap_seq, persistence.journal_head(db_conn)
        )

    def test_compaction_drops_old_recaps(self, db_conn):
        now = time.time()
        db_conn.execute(
            "INSERT INTO recaps (soul_id, day, lines, generated_at, shown_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s1", "2026-01-01", "[]", now - 31 * DAY, now - 31 * DAY),
        )
        db_conn.execute(
            "INSERT INTO recaps (soul_id, day, lines, generated_at, shown_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s1", "2026-09-16", "[]", now - 1 * DAY, None),
        )
        db_conn.commit()
        report = recap.compact_retention(now)
        assert report["recaps_deleted"] == 1
        rows = db_conn.execute("SELECT day FROM recaps").fetchall()
        assert [r["day"] for r in rows] == ["2026-09-16"]

    def test_explicit_payload_salience_overrides_default(self, db_conn):
        now = time.time()
        old = now - 40 * DAY
        db_conn.execute(
            "INSERT INTO journal (tick_id, type, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                0,
                "custom_milestone",
                json.dumps({"soul_id": "s1", "salience": 0.95, "note": "x"}),
                old,
            ),
        )
        db_conn.execute(
            "INSERT INTO journal (tick_id, type, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                0,
                "custom_noise",
                json.dumps({"soul_id": "s1", "salience": 0.05, "note": "y"}),
                old,
            ),
        )
        db_conn.commit()
        recap.compact_retention(now)
        assert (
            db_conn.execute(
                "SELECT COUNT(*) c FROM journal WHERE type = 'custom_milestone'"
            ).fetchone()["c"]
            == 1
        )
        assert (
            db_conn.execute(
                "SELECT COUNT(*) c FROM journal WHERE type = 'custom_noise'"
            ).fetchone()["c"]
            == 0
        )
        rollup = db_conn.execute(
            "SELECT payload FROM journal WHERE type = ?",
            (recap.EVENT_JOURNAL_ROLLUP,),
        ).fetchone()
        assert json.loads(rollup["payload"])["types"] == {"custom_noise": 1}


class TestRecapRest:
    def test_list_recaps_endpoint(self, db_conn, client: TestClient):
        _insert_soul(db_conn, "s1", "tamer1")
        recap.maybe_generate_recap("s1", now=T0)
        recap.maybe_generate_recap("s1", now=T0 + 23 * 3600.0)
        # The operator client in the fixture reads across custody.
        r = client.get("/souls/s1/recaps")
        assert r.status_code == 200, r.text
        recaps = r.json()["recaps"]
        assert len(recaps) == 2
        assert recaps[0]["generated_at"] > recaps[1]["generated_at"]
        assert isinstance(recaps[0]["lines"], list)

    def test_recaps_404_unknown_soul(self, client: TestClient):
        r = client.get("/souls/nope/recaps")
        assert r.status_code == 404

    def test_recaps_403_cross_custody(self, db_conn, client: TestClient):
        from .. import main as main_module

        _insert_soul(db_conn, "s1", "tamer1")
        reg = client.post(
            "/tamers/register", json={"username": "rcap_a", "password": "s3cur3pass!"}
        )
        assert reg.status_code == 201
        login = client.post(
            "/tamers/login", json={"username": "rcap_a", "password": "s3cur3pass!"}
        )
        token = login.json()["token"]
        tc = TestClient(main_module.app, headers={"X-Hub-Secret": token})
        r = tc.get("/souls/s1/recaps")
        assert r.status_code == 403
