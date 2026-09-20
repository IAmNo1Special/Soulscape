"""Pet interaction grammar (issue #31): chirp / pet / carry."""

import itertools
import json
import random
import time

import pytest

from .. import affection
from .. import database
from .. import intents
from .. import persistence
from ..world_tick import WorldTick
from ..agents import drives
from ..agents import scheduler as think_scheduler
from ..agents import sensations

_nonce_seq = itertools.count()


def _make_soul(
    soul_id,
    custodian="tam1",
    x=100.0,
    y=100.0,
    nature="docile",
    essence=100.0,
    state="normal",
):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO souls (soul_id, owner_id, custodian_id, position, "
            "essence, nature, state, satiety, hydration, hp, max_hp) "
            "VALUES (?, 'o1', ?, ?, ?, ?, ?, 100.0, 100.0, 100.0, 100.0)",
            (
                soul_id,
                custodian,
                json.dumps([x, y]),
                essence,
                nature,
                state,
            ),
        )
        conn.commit()


def _enqueue(kind, soul_id, custodian="tam1", nonce=None, **payload):
    return intents.enqueue_intent(
        "sess31", nonce or f"n-{soul_id}-{kind}-{next(_nonce_seq)}", custodian,
        soul_id, kind, payload,
    )


def _status(intent_id):
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT status, result FROM intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
    return row["status"], json.loads(row["result"] or "{}")


def _pump():
    WorldTick().pump_intents()


# ------------------------------------------------------------------
# petting


def test_pet_writes_loyalty_memory_and_nudges_scalar():
    _make_soul("pet1")
    record = _enqueue("affection_pet", "pet1", nonce="pet-n1")
    _pump()
    status, result = _status(record["intent_id"])
    assert status == "adjudicated"
    assert result["petted"] is True
    assert result["loyalty"] == pytest.approx(0.52)
    with database.get_db() as conn:
        loyalty = conn.execute(
            "SELECT loyalty FROM souls WHERE soul_id = 'pet1'"
        ).fetchone()["loyalty"]
        assert loyalty == pytest.approx(0.52)
        rows = conn.execute(
            "SELECT salience, content FROM episodes WHERE soul_id = 'pet1' "
            "AND kind = 'affection'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["salience"] == pytest.approx(0.85)
    assert "petted" in rows[0]["content"]
    with database.get_db() as conn:
        sem = conn.execute(
            "SELECT COUNT(*) AS c FROM semantic_memories "
            "WHERE soul_id = 'pet1' AND kind = 'affection'"
        ).fetchone()["c"]
    assert sem == 1


def test_pet_cooldown_blocks_second_petting_server_side():
    _make_soul("pet2")
    first = _enqueue("affection_pet", "pet2", nonce="pet2-n1")
    second = _enqueue("affection_pet", "pet2", nonce="pet2-n2")
    _pump()
    assert _status(first["intent_id"])[0] == "adjudicated"
    status, result = _status(second["intent_id"])
    assert status == "rejected"
    assert result["reason"] == "pet_cooldown"
    with database.get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM episodes WHERE soul_id = 'pet2' "
            "AND kind = 'affection'"
        ).fetchone()["c"]
        loyalty = conn.execute(
            "SELECT loyalty FROM souls WHERE soul_id = 'pet2'"
        ).fetchone()["loyalty"]
    assert count == 1
    assert loyalty == pytest.approx(0.52)


def test_pet_cooldown_is_per_soul_and_tamer():
    _make_soul("pet3a")
    _make_soul("pet3b")
    first = _enqueue("affection_pet", "pet3a", nonce="pet3a-n")
    _pump()
    assert _status(first["intent_id"])[0] == "adjudicated"
    # Same tamer, same soul: blocked.
    second = _enqueue("affection_pet", "pet3a", nonce="pet3a-n2")
    _pump()
    assert _status(second["intent_id"])[1]["reason"] == "pet_cooldown"
    # Same tamer, different soul: own budget.
    other_soul = _enqueue("affection_pet", "pet3b", nonce="pet3b-n")
    _pump()
    assert _status(other_soul["intent_id"])[0] == "adjudicated"


def test_pet_custody_rejected():
    _make_soul("pet4", custodian="tam1")
    record = _enqueue("affection_pet", "pet4", custodian="stranger", nonce="pet4-n")
    _pump()
    status, result = _status(record["intent_id"])
    assert status == "rejected"
    assert result["reason"] == "custody"


def test_pet_loyalty_clamps_at_one():
    _make_soul("pet5")
    with database.get_db() as conn:
        conn.execute("UPDATE souls SET loyalty = 0.995 WHERE soul_id = 'pet5'")
        conn.commit()
    record = _enqueue("affection_pet", "pet5", nonce="pet5-n")
    _pump()
    status, result = _status(record["intent_id"])
    assert status == "adjudicated"
    assert result["loyalty"] == pytest.approx(1.0)


def test_pet_rejected_for_dormant_and_collapsed():
    _make_soul("pet6", essence=0.0)
    record = _enqueue("affection_pet", "pet6", nonce="pet6-n")
    _pump()
    assert _status(record["intent_id"])[1]["reason"] == "soul_dormant"
    _make_soul("pet7", state="collapsed")
    record = _enqueue("affection_pet", "pet7", nonce="pet7-n")
    _pump()
    assert _status(record["intent_id"])[1]["reason"] == "collapsed"


# ------------------------------------------------------------------
# chirp


def test_chirp_records_sensation_and_pulls_think_forward():
    _make_soul("chirp1")
    sched = think_scheduler.default()
    now = time.time()
    before = sched.schedule_next("chirp1", now)
    record = _enqueue("chirp", "chirp1", nonce="chirp-n1")
    _pump()
    status, result = _status(record["intent_id"])
    assert status == "adjudicated"
    assert result["chirped"] is True
    heard = [s for s in sensations.recent("chirp1")
             if s["text"] == affection.CHIRP_SENSATION]
    assert len(heard) == 1
    assert heard[0]["cause"] == "chirp"
    assert sched.next_think("chirp1") < before


def test_chirp_custody_rejected():
    _make_soul("chirp2", custodian="tam1")
    record = _enqueue("chirp", "chirp2", custodian="stranger", nonce="chirp2-n")
    _pump()
    assert _status(record["intent_id"])[1]["reason"] == "custody"


def test_chirp_rejected_for_collapsed():
    _make_soul("chirp3", state="collapsed")
    record = _enqueue("chirp", "chirp3", nonce="chirp3-n")
    _pump()
    assert _status(record["intent_id"])[1]["reason"] == "collapsed"


# ------------------------------------------------------------------
# carry


@pytest.fixture
def no_escape(monkeypatch):
    monkeypatch.setattr(affection, "roll_escape", lambda *a, **k: False)


def _grab(soul_id, nonce, custodian="tam1"):
    return _enqueue(
        "carry_move", soul_id, custodian, nonce, x=100.0, y=100.0, phase="grab"
    )


def test_carry_grab_move_release_happy_path(no_escape):
    _make_soul("car1")
    grab = _grab("car1", "car1-g")
    _pump()
    status, result = _status(grab["intent_id"])
    assert status == "adjudicated"
    assert result["phase"] == "grab"
    assert result["plot"] == "0:0"
    assert "car1" in affection.carried_souls()
    move = _enqueue(
        "carry_move", "car1", "tam1", "car1-m1", x=110.0, y=105.0, phase="move"
    )
    _pump()
    status, result = _status(move["intent_id"])
    assert status == "adjudicated"
    assert result["escaped"] is False
    assert (result["x"], result["y"]) == pytest.approx((110.0, 105.0))
    # Read-through coordinates are authoritative: the carry wrote the
    # dirty overlay; the viewport stream sees it before the flush.
    dirty = persistence.dirty_get("car1")
    assert dirty is not None
    assert dirty["position"] == pytest.approx([110.0, 105.0])
    # The periodic flush makes it durable.
    with database.get_db() as conn:
        persistence.flush_dirty(conn, 0)
        pos = json.loads(
            conn.execute(
                "SELECT position FROM souls WHERE soul_id = 'car1'"
            ).fetchone()["position"]
        )
    assert pos == pytest.approx([110.0, 105.0])
    release = _enqueue(
        "carry_move", "car1", "tam1", "car1-r", x=0.0, y=0.0, phase="release"
    )
    _pump()
    status, result = _status(release["intent_id"])
    assert status == "adjudicated"
    assert result["was_carried"] is True
    assert "car1" not in affection.carried_souls()


def test_carry_cannot_cross_plot_border(no_escape):
    _make_soul("car2")
    _pump_grab_move("car2")
    bad = _enqueue(
        "carry_move", "car2", "tam1", "car2-bad", x=150.0, y=100.0, phase="move"
    )
    _pump()
    status, result = _status(bad["intent_id"])
    assert status == "rejected"
    assert result["reason"] == "plot_border"
    with database.get_db() as conn:
        pos = json.loads(
            conn.execute(
                "SELECT position FROM souls WHERE soul_id = 'car2'"
            ).fetchone()["position"]
        )
    assert pos == pytest.approx([100.0, 100.0])


def _pump_grab_move(soul_id):
    _pump_result(_grab(soul_id, f"{soul_id}-g"))


def _pump_result(record):
    _pump()
    return _status(record["intent_id"])


def test_carry_true_coordinate_validation_rejects_teleport(no_escape):
    _make_soul("car3")
    _pump_grab_move("car3")
    far = _enqueue(
        "carry_move", "car3", "tam1", "car3-far", x=5000.0, y=100.0, phase="move"
    )
    _pump()
    status, result = _status(far["intent_id"])
    assert status == "rejected"
    assert result["reason"] == "beyond_leash"


def test_carry_move_without_grab_rejected():
    _make_soul("car4")
    move = _enqueue(
        "carry_move", "car4", "tam1", "car4-m", x=105.0, y=105.0, phase="move"
    )
    _pump()
    assert _status(move["intent_id"])[1]["reason"] == "no_carry_session"


def test_carry_suspends_move_to(no_escape):
    _make_soul("car5")
    _pump_grab_move("car5")
    move = intents.enqueue_intent(
        "sess31", "car5-mt", "tam1", "car5", "move_to", {"x": 300.0, "y": 300.0}
    )
    _pump()
    assert _status(move["intent_id"])[1]["reason"] == "carried"


def test_carry_escape_drops_at_true_position_and_cooldown(monkeypatch):
    monkeypatch.setattr(affection, "roll_escape", lambda *a, **k: True)
    _make_soul("car6")
    _pump_grab_move("car6")
    move = _enqueue(
        "carry_move", "car6", "tam1", "car6-m", x=110.0, y=105.0, phase="move"
    )
    _pump()
    status, result = _status(move["intent_id"])
    assert status == "adjudicated"
    assert result["escaped"] is True
    assert (result["x"], result["y"]) == pytest.approx((100.0, 100.0))
    assert "car6" not in affection.carried_souls()
    with database.get_db() as conn:
        eps = conn.execute(
            "SELECT salience, content FROM episodes WHERE soul_id = 'car6' "
            "AND kind = 'intent_outcome'"
        ).fetchall()
    assert len(eps) == 1
    assert "escaped" in eps[0]["content"]
    regrab = _grab("car6", "car6-g2")
    _pump()
    assert _status(regrab["intent_id"])[1]["reason"] == "escape_cooldown"
    affection._escape_cooldowns.clear()
    regrab2 = _grab("car6", "car6-g3")
    _pump()
    assert _status(regrab2["intent_id"])[0] == "adjudicated"


def test_carry_rejected_for_dormant_and_collapsed():
    _make_soul("car7", essence=0.0)
    assert _pump_result(_grab("car7", "car7-g"))[1]["reason"] == "soul_dormant"
    _make_soul("car8", state="collapsed")
    assert _pump_result(_grab("car8", "car8-g"))[1]["reason"] == "collapsed"


def test_carry_grab_by_other_tamer_rejected(no_escape):
    _make_soul("car9", custodian="tam1")
    _pump_result(_grab("car9", "car9-g", custodian="tam1"))
    other = _enqueue(
        "carry_move", "car9", "tamX", "car9-gx", x=100.0, y=100.0, phase="grab"
    )
    _pump()
    assert _status(other["intent_id"])[1]["reason"] in ("custody", "already_carried")


# ------------------------------------------------------------------
# escape odds: statistical match to the nature table


@pytest.mark.parametrize("nature,rate", sorted(affection.NATURE_ESCAPE_RATE.items()))
def test_escape_odds_match_table(nature, rate):
    rng = random.Random(20260917)
    n = 20000
    escapes = sum(affection.roll_escape(nature, 1.0, rng) for _ in range(n))
    assert (escapes / n) == pytest.approx(rate, rel=0.3)


def test_escape_odds_default_for_unknown_nature():
    for nature in (None, "", "fierce"):
        rng = random.Random(77)
        n = 20000
        escapes = sum(affection.roll_escape(nature, 1.0, rng) for _ in range(n))
        assert (escapes / n) == pytest.approx(affection.DEFAULT_ESCAPE_RATE, rel=0.3)


def test_escape_probability_scales_with_dt():
    rng = random.Random(5)
    n = 40000
    one_s = sum(affection.roll_escape("timid", 1.0, rng) for _ in range(n)) / n
    rng = random.Random(5)
    two_s = sum(affection.roll_escape("timid", 2.0, rng) for _ in range(n)) / n
    expected_two = 1.0 - (1.0 - 0.25) ** 2
    assert two_s == pytest.approx(expected_two, rel=0.15)
    assert two_s > one_s


# ------------------------------------------------------------------
# grammar boundary: zero direct-command verbs


def test_command_verbs_rejected_from_intent_grammar():
    for verb in [
        "move", "stay", "fetch", "attack", "come", "go", "wait_order",
        *sorted(affection.FORBIDDEN_COMMAND_VERBS),
    ]:
        payload, error = intents.validate_payload(verb, {})
        assert error == "UNKNOWN_INTENT_KIND", verb
        assert payload is None
    assert affection.FORBIDDEN_COMMAND_VERBS.isdisjoint(affection.AFFECTION_KINDS)


def test_affection_kinds_validate():
    for kind, msg in [
        ("chirp", {}),
        ("affection_pet", {}),
        ("carry_move", {"x": 1.0, "y": 2.0, "phase": "grab"}),
    ]:
        payload, error = intents.validate_payload(kind, msg)
        assert error is None, (kind, error)
    payload, error = intents.validate_payload(
        "carry_move", {"x": 1.0, "y": 2.0, "phase": "yeet"}
    )
    assert error == "BAD_PAYLOAD"


# ------------------------------------------------------------------
# loyalty drive wiring


def test_loyalty_drive_reads_stored_scalar():
    vec = drives.compute_drives(
        "Hardy", 100.0, 100.0, 100.0, 100.0, [], loyalty=0.8
    )
    assert vec["loyalty"] == pytest.approx(0.8)
    vec = drives.compute_drives("Hardy", 100.0, 100.0, 100.0, 100.0, [])
    assert vec["loyalty"] == pytest.approx(
        drives.nature_baseline("Hardy", "loyalty")
    )
    vec = drives.compute_drives(
        "Hardy", 100.0, 100.0, 100.0, 100.0, [], loyalty=1.7
    )
    assert vec["loyalty"] == pytest.approx(1.0)


# ------------------------------------------------------------------
# identity delta domain (viewport stream)


def test_diff_identities_emits_on_change():
    from .. import viewport as vp

    ops = vp.diff_identities({"s1": ("Pip", "Wisp", 4, "resting")}, {})
    assert len(ops) == 1
    op, domain = ops[0]
    assert domain == "priority"
    assert op["domain"] == "identity"
    assert op["state"]["name"] == "Pip"
    assert op["state"]["activity"] == "resting"


def test_diff_identities_noop_when_unchanged():
    from .. import viewport as vp

    current = {"s1": ("Pip", "Wisp", 4, "resting")}
    assert vp.diff_identities(current, dict(current)) == []


def test_diff_identities_emits_only_changed_soul():
    from .. import viewport as vp

    committed = {
        "s1": ("Pip", "Wisp", 4, "resting"),
        "s2": ("Zed", "Wisp", 2, "idle"),
    }
    current = {
        "s1": ("Pip", "Wisp", 4, "resting"),
        "s2": ("Zed", "Wisp", 2, "playing"),
    }
    ops = vp.diff_identities(current, committed)
    assert [op["soul_id"] for op, _ in ops] == ["s2"]
    assert ops[0][0]["state"]["activity"] == "playing"


def test_carry_session_cleared_on_ownership_transfer():
    # tam1 grabs; ownership transfers to tam2 mid-carry; tam1's next move
    # is a custody refusal AND clears the stale session, so tam2 can grab.
    _make_soul("car10", custodian="tam1")
    _pump_result(_grab("car10", "car10-g", custodian="tam1"))
    assert "car10" in affection.carried_souls()
    with database.get_db() as conn:
        conn.execute(
            "UPDATE souls SET custodian_id = 'tam2' WHERE soul_id = 'car10'"
        )
        conn.commit()
    stale = _enqueue(
        "carry_move", "car10", "tam1", "car10-m", x=110.0, y=105.0, phase="move"
    )
    _pump()
    assert _status(stale["intent_id"])[1]["reason"] == "custody"
    assert "car10" not in affection.carried_souls()
    fresh = _grab("car10", "car10-g2", custodian="tam2")
    _pump()
    assert _status(fresh["intent_id"])[0] == "adjudicated"


def test_stranger_chirp_does_not_break_live_carry():
    # tamX (not custodian) chirping mid-carry is refused but the
    # rightful tamer's session survives.
    _make_soul("car11", custodian="tam1")
    _pump_result(_grab("car11", "car11-g", custodian="tam1"))
    poke = _enqueue("chirp", "car11", "tamX", "car11-cx")
    _pump()
    assert _status(poke["intent_id"])[1]["reason"] == "custody"
    assert "car11" in affection.carried_souls()
