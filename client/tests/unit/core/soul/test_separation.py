import math
import random

import pytest

from client.core.soul import physics as soul_physics
from client.core.soul.soul import Soul

DT = 1.0 / 60.0


def _make_souls(n, seed):
    rng = random.Random(seed)
    souls = []
    for i in range(n):
        soul = Soul(
            orb_color_rgb=(1.0, 0.0, 0.0),
            aura_color_rgb=(0.0, 1.0, 0.0),
            name=f"sep-{i}",
            owner_id="test_owner",
            local_instance_id="test_owner",
        )
        soul.x = rng.uniform(0.0, 400.0)
        soul.y = rng.uniform(0.0, 400.0)
        soul.physics.x = soul.x
        soul.physics.y = soul.y
        souls.append(soul)
    for soul in souls:
        soul.soul_registry = souls
    return souls


@pytest.fixture
def souls():
    soul_physics.begin_separation_frame()
    return _make_souls(12, seed=20260917)


def _reference_separation(physics, dt):
    separation_radius = physics.width * 0.3
    separation_force = 100.0
    my_center_x = physics.x + physics.width / 2
    my_center_y = physics.y + physics.height / 2
    push_x = 0.0
    push_y = 0.0
    count = 0
    if hasattr(physics.soul, "soul_registry") and physics.soul.soul_registry:
        for other in physics.soul.soul_registry:
            if other is physics.soul:
                continue
            if abs(other.x - physics.x) > separation_radius:
                continue
            if abs(other.y - physics.y) > separation_radius:
                continue
            other_center_x = other.x + other.width / 2
            other_center_y = other.y + other.height / 2
            dx = my_center_x - other_center_x
            dy = my_center_y - other_center_y
            dist_sq = dx * dx + dy * dy
            min_dist_sq = separation_radius * separation_radius
            if 0 < dist_sq < min_dist_sq:
                dist = math.sqrt(dist_sq)
                force = (separation_radius - dist) / separation_radius
                push_x += (dx / dist) * force
                push_y += (dy / dist) * force
                count += 1
            elif dist_sq == 0:
                angle = random.random() * 2 * math.pi
                push_x += math.cos(angle)
                push_y += math.sin(angle)
                count += 1
    if count > 0:
        physics.x += push_x * separation_force * dt
        physics.y += push_y * separation_force * dt
        physics.soul.x = physics.x
        physics.soul.y = physics.y
    return (physics.x, physics.y)


def test_neighbor_set_matches_brute_force(souls):
    from shared.spatial import SpatialHashGrid

    grid = SpatialHashGrid(cell_size=soul_physics.SEPARATION_CELL_SIZE)
    for soul in souls:
        grid.insert(
            soul.biology.soul_id,
            soul.x + soul.width / 2,
            soul.y + soul.height / 2,
            soul,
        )
    for soul in souls:
        radius = soul.physics.width * 0.3
        cx = soul.x + soul.width / 2
        cy = soul.y + soul.height / 2
        grid_ids = {
            hit[3].biology.soul_id
            for hit in grid.query_radius(cx, cy, radius)
            if hit[3] is not soul
        }
        brute_ids = set()
        for other in souls:
            if other is soul:
                continue
            ocx = other.x + other.width / 2
            ocy = other.y + other.height / 2
            if math.hypot(cx - ocx, cy - ocy) <= radius:
                brute_ids.add(other.biology.soul_id)
        assert grid_ids == brute_ids


def _reset(souls, snapshot):
    for soul, (x, y) in zip(souls, snapshot):
        soul.physics.x = soul.x = x
        soul.physics.y = soul.y = y


def test_displacement_matches_reference_implementation():
    for seed in (1, 2, 3):
        souls = _make_souls(10, seed=seed)
        snapshot = [(s.x, s.y) for s in souls]
        for soul in souls:
            _reset(souls, snapshot)
            soul_physics.begin_separation_frame()
            random.seed(999)
            soul.physics._apply_separation(DT)
            grid_pos = (soul.physics.x, soul.physics.y)
            _reset(souls, snapshot)
            random.seed(999)
            ref_pos = _reference_separation(soul.physics, DT)
            assert grid_pos == pytest.approx(ref_pos)


def test_grid_built_once_per_frame(souls, monkeypatch):
    builds = []
    original = soul_physics.SpatialHashGrid

    def counting(*args, **kwargs):
        builds.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(soul_physics, "SpatialHashGrid", counting)
    soul_physics.begin_separation_frame()
    for soul in souls:
        soul.physics._apply_separation(DT)
    assert len(builds) == 1
    soul_physics.begin_separation_frame()
    for soul in souls:
        soul.physics._apply_separation(DT)
    assert len(builds) == 2


def test_full_update_moves_souls_apart():
    souls = _make_souls(2, seed=42)
    souls[0].x = souls[0].physics.x = 100.0
    souls[0].y = souls[0].physics.y = 100.0
    souls[1].x = souls[1].physics.x = 105.0
    souls[1].y = souls[1].physics.y = 100.0
    soul_physics.begin_separation_frame()
    before = math.hypot(
        souls[0].x - souls[1].x, souls[0].y - souls[1].y
    )
    for soul in souls:
        soul.physics._apply_separation(DT)
    after = math.hypot(souls[0].x - souls[1].x, souls[0].y - souls[1].y)
    assert after > before
