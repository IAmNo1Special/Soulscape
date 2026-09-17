"""Free reflex layer (issue #24).

Evaluated on the per-soul think schedule (not literally every tick --
the scheduler owns cadence; the reflex itself is free, i.e. no LLM).
Thresholds on the drive vector produce intents:

- eat:   satiety < STARVING_SATIETY (20) -> seek nearest known food.
- drink: hydration < THIRST (20)    -> seek nearest known water.
- flee:  fear > FLEE_FEAR (0.7)     -> move away from the threat.
- emote: every EMOTE_MIN_S..EMOTE_MAX_S jittered, pick from drive
  state. Sets soul state only; #30 renders bubbles later.

Food/water come from #34's resource nodes: the production provider is
resources.NodeProvider. The reflex forages autonomously: a starving soul
with food in its inventory eats from the pack; otherwise it seeks the
nearest ready node -- moving toward it, or gathering when in reach.
Gather yields inventory (server-adjudicated); eat/drink consume
inventory. A needy soul with no known nodes records the "no food/water
in sight" sensation and emits nothing (no crash, no intent).

Firing timestamps per soul are kept in-memory so #25's escalation
hook can read the reflex-firing rate (>=3/min triggers deliberation).

Emote storage: in-memory current-emote per soul. The viewport stream
(#12) does not carry it yet -- that wiring is documented as a future
hook in pool.py.
"""

import math
import time
from collections import deque
from typing import Protocol

from .. import biology
from . import drives

#: Reflex thresholds (tunables, documented for Malcom).
SATIETY_STARVE = biology.STARVING_SATIETY
HYDRATION_THIRST = 20.0
FLEE_FEAR = 0.7

#: Reach for consume intents: at/inside this distance the soul eats or
#: drinks instead of moving closer.
CONSUME_REACH_WU = 8.0

#: Flee destination: this far from the soul, away from the threat.
FLEE_DISTANCE_WU = 60.0

#: Emote cadence: uniform jitter in [EMOTE_MIN_S, EMOTE_MAX_S].
EMOTE_MIN_S = 120.0
EMOTE_MAX_S = 300.0

#: Closed emote set. Physical-expression only; no speech bubbles here.
EMOTES = ("content", "hungry", "thirsty", "resting", "alert")

#: Sensation recorded when a need fires but the provider knows nothing.
NO_FOOD_SENSATION = "no food in sight"
NO_WATER_SENSATION = "no water in sight"

#: #25 escalation trigger: this many reflex firings inside the window.
ESCALATION_FIRINGS_PER_MIN = 3


class FoodWaterProvider(Protocol):
    """Where food and water are, as far as the reflex layer knows."""

    def find_food(self, x: float, y: float) -> list[tuple[float, float]]: ...
    def find_water(self, x: float, y: float) -> list[tuple[float, float]]: ...
    def is_food_at(self, x: float, y: float, tol: float = 4.0) -> bool: ...
    def is_water_at(self, x: float, y: float, tol: float = 4.0) -> bool: ...
    def nearest_food_node(self, x: float, y: float) -> dict | None: ...
    def nearest_water_node(self, x: float, y: float) -> dict | None: ...
    def node_by_id(self, node_id: str) -> dict | None: ...


class NullProvider:
    """Provider with no resource nodes: the pre-#34 graceful degradation.

    Kept for tests/scenarios; production uses resources.NodeProvider."""

    def find_food(self, x: float, y: float) -> list[tuple[float, float]]:
        return []

    def find_water(self, x: float, y: float) -> list[tuple[float, float]]:
        return []

    def is_food_at(self, x: float, y: float, tol: float = 4.0) -> bool:
        return False

    def is_water_at(self, x: float, y: float, tol: float = 4.0) -> bool:
        return False

    def nearest_food_node(self, x: float, y: float) -> dict | None:
        return None

    def nearest_water_node(self, x: float, y: float) -> dict | None:
        return None

    def node_by_id(self, node_id: str) -> dict | None:
        return None


def _stub_node_id(kind: str, x: float, y: float) -> str:
    return f"stub:{kind}:{x}:{y}"


def _parse_stub_node_id(node_id: str) -> tuple[str, float, float] | None:
    parts = node_id.split(":")
    if len(parts) != 4 or parts[0] != "stub":
        return None
    try:
        return parts[1], float(parts[2]), float(parts[3])
    except ValueError:
        return None


class StubProvider:
    """Test/scenario provider with a fixed set of known positions."""

    def __init__(
        self,
        food: list[tuple[float, float]] | None = None,
        water: list[tuple[float, float]] | None = None,
    ) -> None:
        self.food = list(food or [])
        self.water = list(water or [])

    def _nearest(
        self, pts: list[tuple[float, float]], x: float, y: float
    ) -> list[tuple[float, float]]:
        return sorted(pts, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)

    def find_food(self, x: float, y: float) -> list[tuple[float, float]]:
        return self._nearest(self.food, x, y)

    def find_water(self, x: float, y: float) -> list[tuple[float, float]]:
        return self._nearest(self.water, x, y)

    def is_food_at(self, x: float, y: float, tol: float = 4.0) -> bool:
        return any(math.hypot(fx - x, fy - y) <= tol for fx, fy in self.food)

    def is_water_at(self, x: float, y: float, tol: float = 4.0) -> bool:
        return any(math.hypot(wx - x, wy - y) <= tol for wx, wy in self.water)

    def nearest_food_node(self, x: float, y: float) -> dict | None:
        pts = self._nearest(self.food, x, y)
        if not pts:
            return None
        fx, fy = pts[0]
        return {
            "node_id": _stub_node_id("food", fx, fy),
            "kind": "food",
            "x": fx,
            "y": fy,
            "amount": 10,
        }

    def nearest_water_node(self, x: float, y: float) -> dict | None:
        pts = self._nearest(self.water, x, y)
        if not pts:
            return None
        wx, wy = pts[0]
        return {
            "node_id": _stub_node_id("water", wx, wy),
            "kind": "water",
            "x": wx,
            "y": wy,
            "amount": 10,
        }

    def node_by_id(self, node_id: str) -> dict | None:
        parsed = _parse_stub_node_id(node_id)
        if parsed is None:
            return None
        kind, x, y = parsed
        pts = self.food if kind == "food" else self.water if kind == "water" else []
        if not any(px == x and py == y for px, py in pts):
            return None
        return {"node_id": node_id, "kind": kind, "x": x, "y": y, "amount": 10}

    def remove_food(self, x: float, y: float, tol: float = 4.0) -> None:
        self.food = [
            (fx, fy) for fx, fy in self.food if math.hypot(fx - x, fy - y) > tol
        ]


_firings: dict[str, deque] = {}
_next_emote_at: dict[str, float] = {}
_current_emote: dict[str, str] = {}


def note_firing(soul_id: str, now: float | None = None) -> None:
    """Record one reflex firing (for the #25 escalation hook)."""
    now = time.time() if now is None else now
    ring = _firings.setdefault(soul_id, deque())
    ring.append(now)


def firing_rate(soul_id: str, window_s: float = 60.0, now: float | None = None) -> int:
    """Reflex firings by this soul inside the trailing window.

    #25 hook: escalate to deliberation at >= ESCALATION_FIRINGS_PER_MIN.
    """
    now = time.time() if now is None else now
    ring = _firings.get(soul_id, deque())
    while ring and ring[0] <= now - window_s:
        ring.popleft()
    return len(ring)


def emote_of(soul_id: str) -> str:
    """Current emote state for a soul (in-memory; viewport hook later)."""
    return _current_emote.get(soul_id, "content")


def schedule_emote(soul_id: str, now: float, rng) -> None:
    """Set the next emote-selection time (tests / boot)."""
    _next_emote_at[soul_id] = now + rng.uniform(EMOTE_MIN_S, EMOTE_MAX_S)


def pick_emote(drive_vec: dict[str, float], satiety: float, hydration: float) -> str:
    """Choose an emote from drive state. Pure function."""
    if satiety < SATIETY_STARVE:
        return "hungry"
    if hydration < HYDRATION_THIRST:
        return "thirsty"
    if drive_vec.get("fear", 0.0) > 0.3:
        return "alert"
    if drive_vec.get("survival", 0.0) >= 0.5:
        return "resting"
    return "content"


def _maybe_emote(
    soul_id: str,
    drive_vec: dict[str, float],
    satiety: float,
    hydration: float,
    now: float,
    rng,
) -> str | None:
    due_at = _next_emote_at.get(soul_id)
    if due_at is None:
        schedule_emote(soul_id, now, rng)
        return None
    if now < due_at:
        return None
    emote = pick_emote(drive_vec, satiety, hydration)
    _current_emote[soul_id] = emote
    schedule_emote(soul_id, now, rng)
    return emote


def _move_to(x: float, y: float, target_kind: str, at: tuple[float, float]) -> dict:
    return {
        "action": "move_to",
        "payload": {
            "x": x,
            "y": y,
            "target_ref": {"kind": target_kind, "at": [at[0], at[1]]},
        },
    }


def _seek_or_consume(
    soul_id: str,
    x: float,
    y: float,
    node: dict | None,
    node_kind: str,
    no_sensation: str,
    cause: str,
    intents: list[dict],
    sensations: list[dict],
    now: float,
) -> None:
    """One need branch: forage the nearest node (gather in reach, else move)."""
    if node is None:
        sensations.append({"text": no_sensation, "cause": cause})
        return
    nx, ny = float(node["x"]), float(node["y"])
    if math.hypot(nx - x, ny - y) <= CONSUME_REACH_WU:
        intents.append({"action": "gather", "payload": {"node_id": node["node_id"]}})
    else:
        intents.append(_move_to(nx, ny, node_kind, (nx, ny)))
    note_firing(soul_id, now)


def evaluate(
    soul_id: str,
    x: float,
    y: float,
    satiety: float,
    hydration: float,
    drive_vec: dict[str, float],
    observations: list[dict],
    provider: FoodWaterProvider,
    rng,
    now: float,
    inventory: dict[str, int] | None = None,
) -> dict:
    """Run the reflex layer. Returns {"intents", "sensations", "emote"}.

    Pure except for the in-memory firing/emote bookkeeping. Emitted
    intents are UNVALIDATED action requests: pool.py validates them
    against the vocabulary before anything is enqueued.

    `inventory` maps item -> qty carried; a starving soul with food eats
    from the pack, otherwise it forages (gather at a node in reach,
    move_to toward it otherwise).
    """
    intents: list[dict] = []
    sensations: list[dict] = []
    inventory = inventory or {}

    if satiety < SATIETY_STARVE:
        if int(inventory.get("food", 0)) > 0:
            intents.append({"action": "eat", "payload": {}})
            note_firing(soul_id, now)
        else:
            _seek_or_consume(
                soul_id,
                x,
                y,
                provider.nearest_food_node(x, y),
                "food",
                NO_FOOD_SENSATION,
                "eat_reflex",
                intents,
                sensations,
                now,
            )
    elif hydration < HYDRATION_THIRST:
        if int(inventory.get("water", 0)) > 0:
            intents.append({"action": "drink", "payload": {}})
            note_firing(soul_id, now)
        else:
            _seek_or_consume(
                soul_id,
                x,
                y,
                provider.nearest_water_node(x, y),
                "water",
                NO_WATER_SENSATION,
                "drink_reflex",
                intents,
                sensations,
                now,
            )
    elif drive_vec.get("fear", 0.0) > FLEE_FEAR:
        threats = [
            o for o in observations if str(o.get("kind", "")) in drives._THREAT_WEIGHTS
        ]
        if threats:
            t = min(threats, key=lambda o: float(o.get("distance", 1e9)))
            dx = x - float(t["position"][0])
            dy = y - float(t["position"][1])
            dist = math.hypot(dx, dy) or 1.0
            intents.append(
                _move_to(
                    x + dx / dist * FLEE_DISTANCE_WU,
                    y + dy / dist * FLEE_DISTANCE_WU,
                    "threat",
                    (float(t["position"][0]), float(t["position"][1])),
                )
            )
            note_firing(soul_id, now)

    emote = _maybe_emote(soul_id, drive_vec, satiety, hydration, now, rng)
    return {"intents": intents, "sensations": sensations, "emote": emote}
