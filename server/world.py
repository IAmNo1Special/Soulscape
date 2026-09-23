"""Hub world spatial index and vision rings (issue #20).

Maintains a uniform spatial hash grid over Soul positions, rebuilt every
tick (20 Hz) from the tick's read-through position view (issue #16: dirty
set overlaid on SQLite). Plot-grid blocking (issue #19) governs movement
only; vision here is purely spatial and never consults plot access.

Detail ring: entities within DETAIL_RADIUS world units, full observation
schema, computed on demand every tick via ``detail_observations``.

Coarse ring: radius ``R = 40 + 10 * floor(V_eff / 25)`` capped at 80 world
units, diffed every 5th tick (1 Hz) by ``diff_coarse``. Events carry
``{id, kind, bearing}`` only. ``bearing`` is radians from ``atan2(dy, dx)``
(east = 0, counterclockwise).

V_eff is the Soul's effective vision: ``stat_vis_base + stat_vis_iv +
stat_vis_ev // 4`` read from the souls table, floored at DEFAULT_VISION
(25, giving R = 50) when no vision stat is recorded. There is currently
no separate vision column; the stat triple is the real stat.

Hysteresis: an entity enters the coarse set when it crosses inside R and
only exits once it leaves R + COARSE_HYSTERESIS. Entities in the band
(R, R + COARSE_HYSTERESIS] keep their previous membership, so a soul
hovering on the boundary does not flap enter/exit events.

First-diff baseline: the first coarse diff for a soul (boot, or a newly
appearing soul) initializes its baseline silently and reports no events,
so boot never produces an enter storm. A soul that vanishes from the
world reports an exit with ``bearing`` None.

``kind`` is currently always ``"soul"``: souls are the only entities in
the world grid. The field exists so future entity types slot into the
same schema without changing consumers.
"""

import json
import math

from shared.spatial import SpatialHashGrid

from . import database
from . import persistence

DETAIL_RADIUS = 40.0
COARSE_BASE_RADIUS = 40.0
COARSE_RADIUS_STEP = 10.0
COARSE_RADIUS_VISION_STEP = 25
COARSE_RADIUS_MAX = 80.0
COARSE_HYSTERESIS = 4.0
COARSE_DIFF_EVERY_TICKS = 20  # 1 Hz at the 20 Hz world tick
DEFAULT_VISION = 25


def effective_vision(
    stat_base: int | None, stat_iv: int | None, stat_ev: int | None
) -> int:
    return max(
        (stat_base or 0) + (stat_iv or 0) + (stat_ev or 0) // 4,
        DEFAULT_VISION,
    )


def coarse_radius(v_eff: int) -> float:
    return min(
        COARSE_RADIUS_MAX,
        COARSE_BASE_RADIUS
        + COARSE_RADIUS_STEP * (max(v_eff, 0) // COARSE_RADIUS_VISION_STEP),
    )


class WorldVision:
    def __init__(self, cell_size: float = 32.0) -> None:
        self.cell_size = cell_size
        self._view: tuple[
            SpatialHashGrid, dict[str, tuple[float, float]], dict[str, dict]
        ] = (SpatialHashGrid(cell_size=cell_size), {}, {})
        self._coarse_in: dict[str, set[str]] = {}
        self._last_coarse_events: dict[str, dict] = {}

    @property
    def grid(self) -> SpatialHashGrid:
        return self._view[0]

    @property
    def positions(self) -> dict[str, tuple[float, float]]:
        return self._view[1]

    @property
    def meta(self) -> dict[str, dict]:
        return self._view[2]

    def rebuild(self) -> None:
        with database.get_db() as conn:
            rows = conn.execute(
                "SELECT soul_id, position, velocity, stat_vis_base, "
                "stat_vis_iv, stat_vis_ev FROM souls"
            ).fetchall()
        dirty = persistence.dirty.snapshot()
        grid = SpatialHashGrid(cell_size=self.cell_size)
        positions: dict[str, tuple[float, float]] = {}
        resolved: dict[str, dict] = {}
        for row in rows:
            soul_id = row["soul_id"]
            try:
                x, y = _parse_pair(row["position"])
            except (ValueError, TypeError, IndexError):
                continue
            unflushed = dirty.get(soul_id)
            if unflushed is not None:
                if unflushed.get("position") is not None:
                    px, py = unflushed["position"]
                    x, y = float(px), float(py)
            try:
                velocity = _parse_pair(row["velocity"])
            except (ValueError, TypeError, IndexError):
                velocity = (0.0, 0.0)
            if unflushed is not None and unflushed.get("velocity") is not None:
                vx, vy = unflushed["velocity"]
                velocity = (float(vx), float(vy))
            entry = {
                "kind": "soul",
                "velocity": velocity,
                "v_eff": effective_vision(
                    row["stat_vis_base"], row["stat_vis_iv"], row["stat_vis_ev"]
                ),
            }
            grid.insert(soul_id, x, y, entry)
            positions[soul_id] = (x, y)
            resolved[soul_id] = entry
        self._view = (grid, positions, resolved)

    def detail_observations(self, soul_id: str) -> list[dict]:
        me = self.positions.get(soul_id)
        if me is None:
            return []
        x, y = me
        observations = []
        for eid, ex, ey, payload in self.grid.query_radius(x, y, DETAIL_RADIUS):
            if eid == soul_id:
                continue
            dx, dy = ex - x, ey - y
            vx, vy = payload.get("velocity", (0.0, 0.0))
            observations.append(
                {
                    "id": eid,
                    "kind": payload.get("kind", "soul"),
                    "position": [ex, ey],
                    "velocity": [vx, vy],
                    "bearing": math.atan2(dy, dx),
                    "distance": math.hypot(dx, dy),
                }
            )
        return observations

    def coarse_events(self, soul_id: str) -> dict:
        return self._last_coarse_events.get(
            soul_id, {"entered": [], "exited": []}
        )

    def diff_coarse(self) -> dict[str, dict]:
        for soul_id in [
            known for known in self._coarse_in if known not in self.positions
        ]:
            del self._coarse_in[soul_id]
            self._last_coarse_events.pop(soul_id, None)
        events: dict[str, dict] = {}
        for soul_id, (x, y) in self.positions.items():
            events[soul_id] = self._diff_one(soul_id, x, y)
            self._last_coarse_events[soul_id] = events[soul_id]
        return events

    def _coarse_view(self, viewer_id: str, entity_id: str) -> dict:
        vx, vy = self.positions[viewer_id]
        seen = self.positions.get(entity_id)
        bearing = None
        if seen is not None:
            bearing = math.atan2(seen[1] - vy, seen[0] - vx)
        return {
            "id": entity_id,
            "kind": self.meta.get(entity_id, {}).get("kind", "soul"),
            "bearing": bearing,
        }

    def _diff_one(self, soul_id: str, x: float, y: float) -> dict:
        v_eff = self.meta.get(soul_id, {}).get("v_eff", DEFAULT_VISION)
        radius = coarse_radius(v_eff)
        inner, band = self.grid.query_radius_sets(
            x, y, radius, radius + COARSE_HYSTERESIS
        )
        inner.discard(soul_id)
        band.discard(soul_id)
        prev = self._coarse_in.get(soul_id)
        if prev is None:
            self._coarse_in[soul_id] = inner
            return {"entered": [], "exited": []}
        current = inner | (prev & band)
        entered = sorted(current - prev)
        exited = sorted(prev - current)
        self._coarse_in[soul_id] = current
        return {
            "entered": [self._coarse_view(soul_id, eid) for eid in entered],
            "exited": [self._coarse_view(soul_id, eid) for eid in exited],
        }


def _parse_pair(raw) -> tuple[float, float]:
    if raw is None:
        return (0.0, 0.0)
    if isinstance(raw, str):
        raw = json.loads(raw)
    x, y = float(raw[0]), float(raw[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("non-finite pair")
    return (x, y)
