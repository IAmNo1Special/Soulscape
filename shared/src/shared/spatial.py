"""Uniform spatial hash grid for radius and rectangle neighbor queries.

Replaces O(n squared) all-pairs scans on both sides of the wire: the
client uses it for separation steering and the Hub uses it for vision
rings. Pure data structure with no dependencies.

Entities live in square cells of ``cell_size`` world units keyed by
integer cell coordinates, so a radius query only visits the cells the
query disc overlaps instead of every entity. Query results are sorted by
``str(entity_id)`` for deterministic iteration order.
"""

from __future__ import annotations

import math
from typing import Any, Hashable

DEFAULT_CELL_SIZE = 32.0

_Stored = tuple[Hashable, float, float, Any]


class SpatialHashGrid:
    def __init__(self, cell_size: float = DEFAULT_CELL_SIZE) -> None:
        if cell_size <= 0:
            raise ValueError("cell_size must be positive")
        self.cell_size = float(cell_size)
        self._cells: dict[tuple[int, int], dict[Hashable, _Stored]] = {}
        self._index: dict[Hashable, tuple[int, int]] = {}

    def _cell_of(self, x: float, y: float) -> tuple[int, int]:
        return (
            math.floor(x / self.cell_size),
            math.floor(y / self.cell_size),
        )

    def insert(
        self, entity_id: Hashable, x: float, y: float, payload: Any = None
    ) -> None:
        self.remove(entity_id)
        cell = self._cell_of(x, y)
        bucket = self._cells.get(cell)
        if bucket is None:
            bucket = {}
            self._cells[cell] = bucket
        bucket[entity_id] = (entity_id, float(x), float(y), payload)
        self._index[entity_id] = cell

    def remove(self, entity_id: Hashable) -> None:
        cell = self._index.pop(entity_id, None)
        if cell is None:
            return
        bucket = self._cells.get(cell)
        if bucket is None:
            return
        bucket.pop(entity_id, None)
        if not bucket:
            del self._cells[cell]

    def move(self, entity_id: Hashable, x: float, y: float) -> None:
        cell = self._index.get(entity_id)
        if cell is None:
            raise KeyError(entity_id)
        payload = self._cells[cell][entity_id][3]
        if self._cell_of(x, y) == cell:
            self._cells[cell][entity_id] = (entity_id, float(x), float(y), payload)
            return
        self.remove(entity_id)
        self.insert(entity_id, x, y, payload)

    def query_radius(
        self, x: float, y: float, radius: float, sort: bool = True
    ) -> list[tuple[Hashable, float, float, Any]]:
        if radius < 0:
            raise ValueError("radius must be non-negative")
        x0 = math.floor((x - radius) / self.cell_size)
        x1 = math.floor((x + radius) / self.cell_size)
        y0 = math.floor((y - radius) / self.cell_size)
        y1 = math.floor((y + radius) / self.cell_size)
        radius_sq = radius * radius
        hits: list[tuple[Hashable, float, float, Any]] = []
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                bucket = self._cells.get((cx, cy))
                if not bucket:
                    continue
                cell_x = cx * self.cell_size
                cell_y = cy * self.cell_size
                far_x = max(
                    abs(x - cell_x), abs(x - cell_x - self.cell_size)
                )
                far_y = max(
                    abs(y - cell_y), abs(y - cell_y - self.cell_size)
                )
                if far_x * far_x + far_y * far_y <= radius_sq:
                    hits.extend(bucket.values())
                else:
                    for entity_id, ex, ey, payload in bucket.values():
                        dx = ex - x
                        dy = ey - y
                        if dx * dx + dy * dy <= radius_sq:
                            hits.append((entity_id, ex, ey, payload))
        if sort:
            hits.sort(key=lambda hit: str(hit[0]))
        return hits

    def query_radius_sets(
        self, x: float, y: float, inner_radius: float, outer_radius: float
    ) -> tuple[set[Hashable], set[Hashable]]:
        if inner_radius < 0 or outer_radius < 0:
            raise ValueError("radii must be non-negative")
        if inner_radius > outer_radius:
            raise ValueError("inner_radius must not exceed outer_radius")
        x0 = math.floor((x - outer_radius) / self.cell_size)
        x1 = math.floor((x + outer_radius) / self.cell_size)
        y0 = math.floor((y - outer_radius) / self.cell_size)
        y1 = math.floor((y + outer_radius) / self.cell_size)
        inner_sq = inner_radius * inner_radius
        outer_sq = outer_radius * outer_radius
        inner: set[Hashable] = set()
        outer: set[Hashable] = set()
        for cx in range(x0, x1 + 1):
            for cy in range(y0, y1 + 1):
                bucket = self._cells.get((cx, cy))
                if not bucket:
                    continue
                cell_x = cx * self.cell_size
                cell_y = cy * self.cell_size
                far_x = max(
                    abs(x - cell_x), abs(x - cell_x - self.cell_size)
                )
                far_y = max(
                    abs(y - cell_y), abs(y - cell_y - self.cell_size)
                )
                far_sq = far_x * far_x + far_y * far_y
                if far_sq <= inner_sq:
                    inner.update(bucket)
                    outer.update(bucket)
                elif far_sq <= outer_sq:
                    for entity_id, ex, ey, _payload in bucket.values():
                        dx = ex - x
                        dy = ey - y
                        if dx * dx + dy * dy <= inner_sq:
                            inner.add(entity_id)
                        outer.add(entity_id)
                else:
                    for entity_id, ex, ey, _payload in bucket.values():
                        dx = ex - x
                        dy = ey - y
                        dist_sq = dx * dx + dy * dy
                        if dist_sq <= inner_sq:
                            inner.add(entity_id)
                            outer.add(entity_id)
                        elif dist_sq <= outer_sq:
                            outer.add(entity_id)
        return inner, outer

    def query_rect(
        self, x0: float, y0: float, x1: float, y1: float
    ) -> list[tuple[Hashable, float, float, Any]]:
        lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
        lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)
        cx0 = math.floor(lo_x / self.cell_size)
        cx1 = math.floor(hi_x / self.cell_size)
        cy0 = math.floor(lo_y / self.cell_size)
        cy1 = math.floor(hi_y / self.cell_size)
        hits: list[tuple[Hashable, float, float, Any]] = []
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                bucket = self._cells.get((cx, cy))
                if not bucket:
                    continue
                for entity_id, ex, ey, payload in bucket.values():
                    if lo_x <= ex <= hi_x and lo_y <= ey <= hi_y:
                        hits.append((entity_id, ex, ey, payload))
        hits.sort(key=lambda hit: str(hit[0]))
        return hits

    def clear(self) -> None:
        self._cells.clear()
        self._index.clear()

    def __len__(self) -> int:
        return len(self._index)

    def __contains__(self, entity_id: Hashable) -> bool:
        return entity_id in self._index
