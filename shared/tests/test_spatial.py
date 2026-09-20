import math
import random

import pytest

from shared.spatial import DEFAULT_CELL_SIZE, SpatialHashGrid


def _brute_force(points, x, y, radius):
    return {
        eid
        for eid, (ex, ey) in points.items()
        if math.hypot(ex - x, ey - y) <= radius
    }


def test_default_cell_size():
    assert SpatialHashGrid().cell_size == DEFAULT_CELL_SIZE == 32.0


def test_bad_cell_size():
    with pytest.raises(ValueError):
        SpatialHashGrid(cell_size=0)
    with pytest.raises(ValueError):
        SpatialHashGrid(cell_size=-4)


def test_insert_remove_len_contains():
    grid = SpatialHashGrid()
    assert len(grid) == 0
    grid.insert("a", 10.0, 20.0, payload="pa")
    grid.insert("b", 100.0, 200.0)
    assert len(grid) == 2
    assert "a" in grid
    assert "zzz" not in grid
    grid.remove("a")
    assert len(grid) == 1
    assert "a" not in grid
    grid.remove("missing")
    assert len(grid) == 1


def test_insert_replaces_existing():
    grid = SpatialHashGrid()
    grid.insert("a", 10.0, 10.0, payload="old")
    grid.insert("a", 500.0, 500.0, payload="new")
    assert len(grid) == 1
    hits = grid.query_radius(500.0, 500.0, 1.0)
    assert [(h[0], h[3]) for h in hits] == [("a", "new")]
    assert grid.query_radius(10.0, 10.0, 1.0) == []


def test_move_keeps_payload_and_updates_cell():
    grid = SpatialHashGrid(cell_size=32.0)
    grid.insert("a", 1.0, 1.0, payload="pa")
    grid.move("a", 2.0, 2.0)
    assert len(grid) == 1
    assert grid.query_radius(2.0, 2.0, 0.5)[0][3] == "pa"
    grid.move("a", 500.0, 500.0)
    assert grid.query_radius(2.0, 2.0, 1.0) == []
    assert grid.query_radius(500.0, 500.0, 1.0)[0][0] == "a"
    with pytest.raises(KeyError):
        grid.move("missing", 0.0, 0.0)


def test_clear():
    grid = SpatialHashGrid()
    grid.insert("a", 1.0, 1.0)
    grid.insert("b", 2.0, 2.0)
    grid.clear()
    assert len(grid) == 0
    assert grid.query_radius(1.5, 1.5, 100.0) == []


def test_query_radius_negative():
    grid = SpatialHashGrid()
    with pytest.raises(ValueError):
        grid.query_radius(0.0, 0.0, -1.0)


def test_query_radius_zero_hits_exact_point():
    grid = SpatialHashGrid()
    grid.insert("a", 5.0, 5.0)
    grid.insert("b", 5.0, 5.0001)
    hits = grid.query_radius(5.0, 5.0, 0.0)
    assert [h[0] for h in hits] == ["a"]


def test_query_rect():
    grid = SpatialHashGrid()
    grid.insert("a", 10.0, 10.0)
    grid.insert("b", 90.0, 90.0)
    grid.insert("c", 200.0, 200.0)
    hits = grid.query_rect(0.0, 0.0, 100.0, 100.0)
    assert {h[0] for h in hits} == {"a", "b"}
    flipped = grid.query_rect(100.0, 100.0, 0.0, 0.0)
    assert {h[0] for h in flipped} == {"a", "b"}


def test_negative_coordinates():
    grid = SpatialHashGrid()
    grid.insert("neg", -100.0, -200.0, payload=1)
    hits = grid.query_radius(-100.0, -200.0, 5.0)
    assert [h[0] for h in hits] == ["neg"]


def test_parity_with_brute_force_seeded():
    rng = random.Random(20260917)
    for trial in range(5):
        cell_size = rng.choice([8.0, 16.0, 32.0, 64.0])
        grid = SpatialHashGrid(cell_size=cell_size)
        points = {}
        for i in range(300):
            if trial == 0:
                x = rng.choice(
                    [k * cell_size for k in range(-4, 5)]
                ) + rng.uniform(-0.001, 0.001)
                y = rng.choice(
                    [k * cell_size for k in range(-4, 5)]
                ) + rng.uniform(-0.001, 0.001)
            else:
                x = rng.uniform(-500.0, 500.0)
                y = rng.uniform(-500.0, 500.0)
            points[f"e{i}"] = (x, y)
            grid.insert(f"e{i}", x, y, payload=i)
        assert len(grid) == 300
        for _ in range(40):
            qx = rng.uniform(-520.0, 520.0)
            qy = rng.uniform(-520.0, 520.0)
            radius = rng.choice(
                [0.0, 0.5, 1.0, cell_size - 0.01, cell_size, cell_size * 1.5, 120.0]
            )
            grid_ids = {h[0] for h in grid.query_radius(qx, qy, radius)}
            assert grid_ids == _brute_force(points, qx, qy, radius)


def test_cell_boundary_edge_cases():
    grid = SpatialHashGrid(cell_size=32.0)
    grid.insert("edge", 32.0, 64.0)
    grid.insert("just_out", 32.0 + 40.0001, 64.0)
    hits = {h[0] for h in grid.query_radius(32.0, 64.0, 40.0)}
    assert hits == {"edge"}
    hits = {h[0] for h in grid.query_radius(0.0, 64.0, 32.0)}
    assert hits == {"edge"}


def test_query_results_deterministic_order():
    rng = random.Random(7)
    grid = SpatialHashGrid()
    for i in range(50):
        grid.insert(f"s{i:03d}", rng.uniform(0, 100), rng.uniform(0, 100))
    first = [h[0] for h in grid.query_radius(50, 50, 200)]
    second = [h[0] for h in grid.query_radius(50, 50, 200)]
    assert first == second == sorted(first, key=str)


def test_query_radius_sets_parity_with_brute_force():
    rng = random.Random(20260920)
    for trial in range(5):
        cell_size = rng.choice([8.0, 16.0, 32.0, 64.0])
        grid = SpatialHashGrid(cell_size=cell_size)
        points = {}
        for i in range(200):
            x = rng.uniform(-300.0, 300.0)
            y = rng.uniform(-300.0, 300.0)
            points[f"e{i}"] = (x, y)
            grid.insert(f"e{i}", x, y)
        for _ in range(30):
            qx = rng.uniform(-320.0, 320.0)
            qy = rng.uniform(-320.0, 320.0)
            inner_r = rng.choice([0.0, 5.0, cell_size, 90.0])
            outer_r = inner_r + rng.choice([0.0, 4.0, 40.0])
            inner, outer = grid.query_radius_sets(qx, qy, inner_r, outer_r)
            assert inner == _brute_force(points, qx, qy, inner_r)
            assert outer == _brute_force(points, qx, qy, outer_r)
            assert inner <= outer


def test_query_radius_sets_bad_radii():
    grid = SpatialHashGrid()
    with pytest.raises(ValueError):
        grid.query_radius_sets(0.0, 0.0, -1.0, 5.0)
    with pytest.raises(ValueError):
        grid.query_radius_sets(0.0, 0.0, 10.0, 5.0)


def test_query_radius_unsorted_matches_sorted():
    rng = random.Random(11)
    grid = SpatialHashGrid()
    for i in range(100):
        grid.insert(f"e{i}", rng.uniform(0, 200), rng.uniform(0, 200))
    ordered = grid.query_radius(100, 100, 80)
    unordered = grid.query_radius(100, 100, 80, sort=False)
    assert {h[0] for h in ordered} == {h[0] for h in unordered}
    assert [h[0] for h in ordered] == sorted([h[0] for h in ordered], key=str)
