import json
import math

import pytest

from .. import persistence
from ..world import (
    COARSE_DIFF_EVERY_TICKS,
    COARSE_HYSTERESIS,
    DEFAULT_VISION,
    DETAIL_RADIUS,
    WorldVision,
    coarse_radius,
    effective_vision,
)
from ..world_tick import WorldTick


def _insert_soul(
    db_conn,
    soul_id,
    x=0.0,
    y=0.0,
    vx=0.0,
    vy=0.0,
    vis_base=None,
    vis_iv=None,
    vis_ev=None,
):
    db_conn.execute(
        "INSERT INTO souls (soul_id, owner_id, position, velocity, "
        "stat_vis_base, stat_vis_iv, stat_vis_ev) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            soul_id,
            f"owner_{soul_id}",
            json.dumps([x, y]),
            json.dumps([vx, vy]),
            vis_base,
            vis_iv,
            vis_ev,
        ),
    )
    db_conn.commit()


def _move(soul_id, x, y):
    persistence.dirty.mark(soul_id, position=[x, y])


@pytest.fixture
def vision(db_conn):
    _insert_soul(db_conn, "s1", x=0.0, y=0.0, vx=3.0, vy=4.0)
    v = WorldVision()
    v.rebuild()
    return v


def test_effective_vision_defaults():
    assert effective_vision(None, None, None) == DEFAULT_VISION == 25
    assert effective_vision(0, 0, 0) == 25
    assert effective_vision(100, 0, 0) == 100
    assert effective_vision(60, 20, 40) == 60 + 20 + 10


def test_coarse_radius_formula():
    assert coarse_radius(25) == 50.0
    assert coarse_radius(0) == 40.0
    assert coarse_radius(49) == 50.0
    assert coarse_radius(50) == 60.0
    assert coarse_radius(100) == 80.0
    assert coarse_radius(1000) == 80.0


def test_rebuild_reads_through_dirty(db_conn, vision):
    assert vision.positions["s1"] == (0.0, 0.0)
    _move("s1", 111.0, 222.0)
    vision.rebuild()
    assert vision.positions["s1"] == (111.0, 222.0)
    assert len(vision.grid) == 1
    hits = vision.grid.query_radius(111.0, 222.0, 1.0)
    assert [h[0] for h in hits] == ["s1"]


def test_rebuild_picks_up_dirty_velocity(db_conn, vision):
    persistence.dirty.mark("s1", velocity=[9.0, -9.0])
    vision.rebuild()
    obs = vision.detail_observations("s1")
    assert obs == []
    _insert_soul(db_conn, "s2", x=10.0, y=0.0)
    vision.rebuild()
    obs = vision.detail_observations("s2")
    assert obs[0]["id"] == "s1"
    assert obs[0]["velocity"] == [9.0, -9.0]


def test_detail_observations_schema_and_radius(db_conn, vision):
    _insert_soul(db_conn, "near", x=30.0, y=0.0, vx=1.0, vy=2.0)
    _insert_soul(db_conn, "far", x=DETAIL_RADIUS + 1.0, y=0.0)
    vision.rebuild()
    obs = vision.detail_observations("s1")
    assert len(obs) == 1
    entry = obs[0]
    assert set(entry) == {
        "id",
        "kind",
        "position",
        "velocity",
        "bearing",
        "distance",
    }
    assert entry["id"] == "near"
    assert entry["kind"] == "soul"
    assert entry["position"] == [30.0, 0.0]
    assert entry["velocity"] == [1.0, 2.0]
    assert entry["distance"] == pytest.approx(30.0)
    assert entry["bearing"] == pytest.approx(0.0)


def test_detail_observations_bearing(db_conn, vision):
    _insert_soul(db_conn, "north", x=0.0, y=10.0)
    vision.rebuild()
    obs = vision.detail_observations("s1")
    assert obs[0]["bearing"] == pytest.approx(math.pi / 2)


def test_detail_observations_unknown_soul(vision):
    assert vision.detail_observations("ghost") == []


def test_coarse_first_diff_is_silent(db_conn):
    _insert_soul(db_conn, "s1", x=0.0, y=0.0)
    _insert_soul(db_conn, "s2", x=10.0, y=0.0)
    v = WorldVision()
    v.rebuild()
    events = v.diff_coarse()
    assert events["s1"] == {"entered": [], "exited": []}
    assert events["s2"] == {"entered": [], "exited": []}
    assert v.coarse_events("s1") == {"entered": [], "exited": []}


def test_coarse_enter_exit_crossing(db_conn):
    _insert_soul(db_conn, "s1", x=0.0, y=0.0)
    _insert_soul(db_conn, "s2", x=100.0, y=0.0)
    v = WorldVision()
    v.rebuild()
    v.diff_coarse()
    _move("s2", 45.0, 0.0)
    v.rebuild()
    events = v.diff_coarse()["s1"]
    assert [e["id"] for e in events["entered"]] == ["s2"]
    assert events["exited"] == []
    view = events["entered"][0]
    assert set(view) == {"id", "kind", "bearing"}
    assert view["kind"] == "soul"
    assert view["bearing"] == pytest.approx(0.0)
    _move("s2", 60.0, 0.0)
    v.rebuild()
    events = v.diff_coarse()["s1"]
    assert [e["id"] for e in events["exited"]] == ["s2"]
    assert events["entered"] == []


def test_coarse_hysteresis_no_flap(db_conn):
    _insert_soul(db_conn, "s1", x=0.0, y=0.0)
    _insert_soul(db_conn, "s2", x=10.0, y=0.0)
    v = WorldVision()
    v.rebuild()
    v.diff_coarse()
    radius = coarse_radius(DEFAULT_VISION)
    assert radius == 50.0
    for x in (radius - 1.0, radius + 1.0, radius - 0.5, radius + 2.0):
        _move("s2", x, 0.0)
        v.rebuild()
        events = v.diff_coarse()["s1"]
        assert events == {"entered": [], "exited": []}
    _move("s2", radius + COARSE_HYSTERESIS + 1.0, 0.0)
    v.rebuild()
    events = v.diff_coarse()["s1"]
    assert [e["id"] for e in events["exited"]] == ["s2"]
    _move("s2", radius + COARSE_HYSTERESIS - 1.0, 0.0)
    v.rebuild()
    events = v.diff_coarse()["s1"]
    assert events == {"entered": [], "exited": []}
    _move("s2", radius - 1.0, 0.0)
    v.rebuild()
    events = v.diff_coarse()["s1"]
    assert [e["id"] for e in events["entered"]] == ["s2"]


def test_coarse_teleport_in_and_out(db_conn):
    _insert_soul(db_conn, "s1", x=0.0, y=0.0)
    _insert_soul(db_conn, "s2", x=500.0, y=500.0)
    v = WorldVision()
    v.rebuild()
    v.diff_coarse()
    _move("s2", 5.0, 0.0)
    v.rebuild()
    events = v.diff_coarse()["s1"]
    assert [e["id"] for e in events["entered"]] == ["s2"]
    _move("s2", 500.0, 500.0)
    v.rebuild()
    events = v.diff_coarse()["s1"]
    assert [e["id"] for e in events["exited"]] == ["s2"]


def test_coarse_uses_real_vision_stat(db_conn):
    _insert_soul(db_conn, "s1", x=0.0, y=0.0, vis_base=100)
    _insert_soul(db_conn, "s2", x=70.0, y=0.0)
    v = WorldVision()
    v.rebuild()
    assert v.meta["s1"]["v_eff"] == 100
    assert coarse_radius(100) == 80.0
    v.diff_coarse()
    _move("s2", 75.0, 0.0)
    v.rebuild()
    events = v.diff_coarse()["s1"]
    assert events == {"entered": [], "exited": []}
    _move("s2", 85.0, 0.0)
    v.rebuild()
    events = v.diff_coarse()["s1"]
    assert [e["id"] for e in events["exited"]] == ["s2"]


def test_coarse_prunes_deleted_souls(db_conn):
    _insert_soul(db_conn, "s1", x=0.0, y=0.0)
    _insert_soul(db_conn, "s2", x=10.0, y=0.0)
    v = WorldVision()
    v.rebuild()
    v.diff_coarse()
    db_conn.execute("DELETE FROM souls WHERE soul_id = 's2'")
    db_conn.commit()
    v.rebuild()
    events = v.diff_coarse()
    assert "s2" not in events
    assert [e["id"] for e in events["s1"]["exited"]] == ["s2"]
    assert "s2" not in v._coarse_in
    v.rebuild()
    events = v.diff_coarse()
    assert events["s1"] == {"entered": [], "exited": []}


def test_tick_wires_rebuild_and_fifth_tick_diff(db_conn):
    _insert_soul(db_conn, "s1", x=100.0, y=200.0, vx=10.0, vy=0.0)
    _insert_soul(db_conn, "s2", x=120.0, y=200.0)
    tick = WorldTick()
    for _ in range(COARSE_DIFF_EVERY_TICKS):
        tick.step()
    assert tick.tick_id == COARSE_DIFF_EVERY_TICKS
    assert len(tick.vision.grid) == 2
    assert tick.vision.positions["s1"][0] == pytest.approx(100.0 + 10.0 * 0.2 * 5)
    assert "s2" in tick.vision._coarse_in["s1"]
    obs = tick.vision.detail_observations("s1")
    assert {o["id"] for o in obs} == {"s2"}
    snap = tick.snapshot()
    assert snap["vision_entities"] == 2
