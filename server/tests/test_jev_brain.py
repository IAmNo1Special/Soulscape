import json
import math

import pytest

from .. import database
from ..agents import drives, jev, jev_brain, pool as pool_mod, reflex, sensations
from ..agents.jev_brain import Cooldowns, WanderManager


@pytest.fixture(autouse=True)
def clean_rings():
    sensations.clear()
    yield
    sensations.clear()


class FakeVision:
    def __init__(self, obs):
        self._obs = obs

    def detail_observations(self, soul_id):
        return self._obs


def _insert_soul(db_conn, soul_id, x=960.0, y=540.0, **kw):
    cols = {
        "soul_id": soul_id,
        "owner_id": f"owner_{soul_id}",
        "custodian_id": f"owner_{soul_id}",
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


def _snap(db_conn, soul_id="s1", obs=None, kinds=("restlessness",), now=1000.0):
    _insert_soul(db_conn, soul_id)
    agent_pool = pool_mod.AgentPool()
    provider = reflex.StubProvider()
    return jev_brain.build_snapshot(
        agent_pool, soul_id, FakeVision(obs or []), provider, kinds, now
    )


def test_snapshot_shape(db_conn):
    snap = _snap(db_conn)
    assert snap["soul_id"] == "s1"
    assert snap["position"] == [960.0, 540.0]
    assert snap["event_kinds"] == ["restlessness"]
    assert set(snap["drives"]) == set(drives.DRIVES) | {"fear"}
    assert snap["need_bands"] == {"satiety": "high", "hydration": "high", "hp": "high"}
    assert snap["sensations"] == []
    assert snap["nearby_souls"] == 0
    assert snap["nearest_soul"] is None
    assert snap["tamer_present"] is False
    assert snap["region"] in ("commons", "road", "plot", "edge")
    assert snap["seconds_since_last_event"] is None


def test_snapshot_need_bands(db_conn):
    _insert_soul(db_conn, "s1", satiety=20.0, hydration=50.0, hp=90.0)
    agent_pool = pool_mod.AgentPool()
    snap = jev_brain.build_snapshot(
        agent_pool, "s1", FakeVision([]), reflex.StubProvider(), ("x",), 1000.0
    )
    assert snap["need_bands"] == {"satiety": "low", "hydration": "mid", "hp": "high"}
    assert snap["satiety"] == 20.0


def test_snapshot_social_context(db_conn):
    obs = [
        {"id": "a", "kind": "soul", "distance": 90.0, "position": [1000.0, 540.0]},
        {"id": "b", "kind": "soul", "distance": 500.0, "position": [1400.0, 540.0]},
    ]
    snap = _snap(db_conn, obs=obs)
    assert snap["nearby_souls"] == 2
    assert snap["nearest_soul"]["kind"] == "soul"
    assert snap["nearest_soul"]["distance_band"] == "near"
    assert snap["nearest_soul"]["position"] == [1000.0, 540.0]


def test_snapshot_sensations_newest_last(db_conn):
    _insert_soul(db_conn, "s1")
    for i in range(6):
        sensations.record("s1", f"sensation {i}", f"cause{i}", tick_id=i)
    agent_pool = pool_mod.AgentPool()
    snap = jev_brain.build_snapshot(
        agent_pool, "s1", FakeVision([]), reflex.StubProvider(), ("x",), 1000.0
    )
    assert snap["sensations"] == [f"sensation {i}" for i in range(2, 6)]


def test_snapshot_missing_soul_returns_none(db_conn):
    agent_pool = pool_mod.AgentPool()
    assert (
        jev_brain.build_snapshot(
            agent_pool, "ghost", FakeVision([]), reflex.StubProvider(), ("x",), 1.0
        )
        is None
    )


def test_snapshot_is_json_safe(db_conn):
    snap = _snap(db_conn)
    assert json.loads(json.dumps(snap))["soul_id"] == "s1"


def test_survival_tripped():
    assert jev_brain.survival_tripped({"satiety": 10.0, "hydration": 90.0, "fear": 0.1})
    assert jev_brain.survival_tripped({"satiety": 90.0, "hydration": 5.0, "fear": 0.1})
    assert jev_brain.survival_tripped({"satiety": 90.0, "hydration": 90.0, "fear": 0.9})
    assert not jev_brain.survival_tripped(
        {"satiety": 90.0, "hydration": 90.0, "fear": 0.1}
    )


def _judgments(**over):
    base = {
        "threatened": False,
        "needs_pressing": False,
        "user_engaged": False,
        "should_rest": False,
    }
    base.update(over)
    return base


def _snapshot(**over):
    base = {"nearest_soul": None}
    base.update(over)
    return base


def test_arbitrate_threshold_boundary():
    cd = Cooldowns()
    below = jev_brain.arbitrate(
        "s1", _judgments(threatened=0.49), _snapshot(), cd, 1000.0, ["restlessness"]
    )
    assert below == "explore"
    at = jev_brain.arbitrate(
        "s1", _judgments(threatened=0.5), _snapshot(), cd, 1000.0, ["restlessness"]
    )
    assert at == "flee_threat"


def test_arbitrate_priority_order():
    cd = Cooldowns()
    goal = jev_brain.arbitrate(
        "s1",
        _judgments(
            threatened=True, needs_pressing=True, user_engaged=True, should_rest=True
        ),
        _snapshot(nearest_soul={"kind": "soul"}),
        cd,
        1000.0,
        ["restlessness"],
    )
    assert goal == "flee_threat"


def test_arbitrate_deterministic():
    cd = Cooldowns()
    args = (
        "s1",
        _judgments(needs_pressing=True),
        _snapshot(nearest_soul={"kind": "soul"}),
        cd,
        1000.0,
        ["restlessness"],
    )
    assert jev_brain.arbitrate(*args) == jev_brain.arbitrate(*args) == "sate_needs"


def test_arbitrate_explore_gated_on_restlessness():
    cd = Cooldowns()
    assert (
        jev_brain.arbitrate(
            "s1", _judgments(), _snapshot(), cd, 1000.0, ["restlessness"]
        )
        == "explore"
    )
    assert (
        jev_brain.arbitrate(
            "s1", _judgments(), _snapshot(), cd, 1000.0, ["vision_enter"]
        )
        is None
    )
    assert (
        jev_brain.arbitrate(
            "s1", _judgments(), _snapshot(), cd, 1000.0, ["wander_continue"]
        )
        == "explore"
    )


def test_arbitrate_rest_beats_explore():
    cd = Cooldowns()
    goal = jev_brain.arbitrate(
        "s1", _judgments(should_rest=True), _snapshot(), cd, 1000.0, ["restlessness"]
    )
    assert goal == "rest"


def test_arbitrate_cooldown_skips_to_next():
    cd = Cooldowns()
    cd.mark("s1", "engage_user", 1000.0)
    goal = jev_brain.arbitrate(
        "s1", _judgments(user_engaged=True), _snapshot(), cd, 1030.0, ["tamer_poke"]
    )
    assert goal is None
    goal = jev_brain.arbitrate(
        "s1", _judgments(user_engaged=True), _snapshot(), cd, 1070.0, ["tamer_poke"]
    )
    assert goal == "engage_user"


def test_arbitrate_socialize_gated_on_nearby_soul():
    cd = Cooldowns()
    assert (
        jev_brain.arbitrate("s1", _judgments(), _snapshot(), cd, 1.0, ["vision_enter"])
        is None
    )
    assert (
        jev_brain.arbitrate(
            "s1",
            _judgments(),
            _snapshot(nearest_soul={"kind": "soul"}),
            cd,
            1.0,
            ["vision_enter"],
        )
        == "socialize"
    )


def test_plan_sate_needs_eat_directly():
    snap = {
        "has_food": True,
        "has_water": False,
        "node_nearby": False,
        "node_exists": False,
    }
    assert jev_brain.plan_for_goal("sate_needs", snap) == ["eat"]


def test_plan_sate_needs_gather_then_eat():
    snap = {
        "has_food": False,
        "has_water": False,
        "node_nearby": True,
        "node_exists": True,
    }
    assert jev_brain.plan_for_goal("sate_needs", snap) == ["gather", "eat"]


def test_plan_sate_needs_move_gather_eat():
    snap = {
        "has_food": False,
        "has_water": False,
        "node_nearby": False,
        "node_exists": True,
    }
    assert jev_brain.plan_for_goal("sate_needs", snap) == [
        "move_to",
        "gather",
        "eat",
    ]


def test_plan_sate_needs_no_food_no_node():
    snap = {
        "has_food": False,
        "has_water": False,
        "node_nearby": False,
        "node_exists": False,
    }
    assert jev_brain.plan_for_goal("sate_needs", snap) == []


def test_plan_engage_is_look():
    assert jev_brain.plan_for_goal("engage_user", {}) == ["look"]


def test_plan_flee_and_explore_single_move():
    assert jev_brain.plan_for_goal("flee_threat", {}) == ["move_to"]
    assert jev_brain.plan_for_goal("explore", {}) == ["move_to"]
    assert jev_brain.plan_for_goal("socialize", {}) == ["move_to"]


def test_payload_flee_moves_away_from_threat():
    snap = {
        "position": [500.0, 500.0],
        "nearest_threat": {"position": [600.0, 500.0], "distance": 100.0},
    }
    payload = jev_brain.payload_for("flee_threat", "move_to", snap)
    assert payload["x"] < 500.0
    assert payload["y"] == pytest.approx(500.0)


def test_payload_sate_gather_uses_node_id():
    snap = {"nearest_node": {"node_id": "n1", "position": [10.0, 10.0]}}
    assert jev_brain.payload_for("sate_needs", "gather", snap) == {"node_id": "n1"}
    assert jev_brain.payload_for("sate_needs", "eat", snap) == {}


def test_payload_sate_move_targets_node():
    snap = {
        "position": [0.0, 0.0],
        "nearest_node": {"node_id": "n1", "position": [300.0, 400.0]},
    }
    payload = jev_brain.payload_for("sate_needs", "move_to", snap)
    assert payload["x"] == 300.0
    assert payload["y"] == 400.0
    assert "speed" not in payload


def test_payload_socialize_approaches_but_keeps_distance():
    snap = {
        "position": [0.0, 0.0],
        "nearest_soul": {"position": [300.0, 0.0], "distance": 300.0},
    }
    payload = jev_brain.payload_for("socialize", "move_to", snap)
    dist = math.hypot(payload["x"] - 0.0, payload["y"] - 0.0)
    assert dist == pytest.approx(220.0)
    assert payload["pace"] == "amble"
    assert "speed" not in payload


def test_wander_deterministic_per_episode():
    a = WanderManager(seed=7)
    b = WanderManager(seed=7)
    assert a.start("s1", 960.0, 540.0) == b.start("s1", 960.0, 540.0)


def test_wander_episodes_differ_by_sequence():
    m = WanderManager(seed=7)
    first = m.start("s1", 960.0, 540.0)
    second = m.start("s1", 960.0, 540.0)
    assert first != second


def test_wander_leg_count_and_spacing():
    m = WanderManager(seed=7)
    legs = m.start("s1", 960.0, 540.0)
    assert 2 <= len(legs) <= 4
    px, py = 960.0, 540.0
    for x, y in legs:
        d = math.hypot(x - px, y - py)
        assert 80.0 <= d <= 150.0
        px, py = x, y


def test_wander_waypoints_edge_clamped():
    m = WanderManager(seed=7)
    w, h = database.SCREEN_BOUNDS
    for _ in range(20):
        legs = m.start("edge", 5.0, 5.0)
        for x, y in legs:
            assert 10.0 <= x <= w - 10.0
            assert 10.0 <= y <= h - 10.0


def test_wander_next_leg_pops_in_order():
    m = WanderManager(seed=7)
    legs = m.start("s1", 960.0, 540.0)
    assert m.legs_remaining("s1") == len(legs)
    assert m.next_leg("s1") == legs[0]
    assert m.legs_remaining("s1") == len(legs) - 1
    assert m.active("s1") is True
    m.cancel("s1")
    assert m.active("s1") is False
    assert m.next_leg("s1") is None


def test_wander_dwell_range_and_fixed_amble_speed():
    m = WanderManager(seed=7)
    m.start("s1", 960.0, 540.0)
    assert 2.0 <= m.dwell_for("s1") <= 5.0
    assert jev.WANDER_SPEED_MIN <= jev.JEV_AMBLE_SPEED <= jev.WANDER_SPEED_MAX
