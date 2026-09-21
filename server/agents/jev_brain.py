import json
import math
import random

from .. import database, plots, presence, biology
from . import drives, jev, reflex, sensations
from .. import resources

DIST_NEAR_U = 150.0
DIST_MID_U = 400.0
FLEE_AWAY_U = 150.0
SOCIALIZE_APPROACH_GAP = 80.0

GOAL_FLEE = "flee_threat"
GOAL_SATE = "sate_needs"
GOAL_ENGAGE = "engage_user"
GOAL_SOCIALIZE = "socialize"
GOAL_EXPLORE = "explore"
GOAL_REST = "rest"

_GOAL_PRIORITY = (
    GOAL_FLEE,
    GOAL_SATE,
    GOAL_ENGAGE,
    GOAL_SOCIALIZE,
    GOAL_REST,
    GOAL_EXPLORE,
)

GOAL_COOLDOWNS = {
    GOAL_ENGAGE: 60.0,
    GOAL_SOCIALIZE: 120.0,
    GOAL_REST: 300.0,
}

_GOAL_FACTS = {
    GOAL_FLEE: {"threatened": False},
    GOAL_SATE: {"needs_pressing": False},
    GOAL_ENGAGE: {"attempted_engage": True},
    GOAL_SOCIALIZE: {"attempted_socialize": True},
    GOAL_EXPLORE: {"attempted_explore": True},
    GOAL_REST: {"attempted_rest": True},
}

_GOAL_ACTION = {
    GOAL_FLEE: "move_to",
    GOAL_ENGAGE: "look",
    GOAL_SOCIALIZE: "move_to",
    GOAL_EXPLORE: "move_to",
    GOAL_REST: "rest",
}

_QUESTION_GOAL = {
    GOAL_FLEE: "threatened",
    GOAL_SATE: "needs_pressing",
    GOAL_ENGAGE: "user_engaged",
    GOAL_REST: "should_rest",
}


class Cooldowns:
    def __init__(self) -> None:
        self._last: dict[tuple[str, str], float] = {}

    def ready(self, soul_id: str, goal: str, now: float) -> bool:
        return now - self._last.get(
            (soul_id, goal), float("-inf")
        ) >= GOAL_COOLDOWNS.get(goal, 0.0)

    def mark(self, soul_id: str, goal: str, now: float) -> None:
        self._last[(soul_id, goal)] = now

    def forget(self, soul_id: str) -> None:
        for key in [k for k in self._last if k[0] == soul_id]:
            del self._last[key]


def _band(value: float) -> str:
    return biology.need_band(value)


def _distance_band(d: float) -> str:
    if d < DIST_NEAR_U:
        return "near"
    if d < DIST_MID_U:
        return "mid"
    return "far"


def _threat_kinds() -> set[str]:
    return set(getattr(drives, "_THREAT_WEIGHTS", {}))


def build_snapshot(
    agent_pool,
    soul_id: str,
    vision,
    provider,
    kinds: list[str],
    now: float,
    last_event_at: float | None = None,
    position_override: tuple[float, float] | None = None,
) -> dict | None:
    row = agent_pool._load_soul(soul_id)
    if row is None:
        return None
    if position_override is not None:
        pos = [float(position_override[0]), float(position_override[1])]
    else:
        raw_pos = row["position"]
        pos = (
            [float(v) for v in json.loads(raw_pos)]
            if isinstance(raw_pos, str)
            else list(raw_pos or [0.0, 0.0])
        )
    satiety = float(row.get("satiety") or 0.0)
    hydration = float(row.get("hydration") or 0.0)
    hp = float(row.get("hp") or 0.0)
    obs = vision.detail_observations(soul_id)
    drive_vec = drives.compute_drives(
        row.get("nature"),
        satiety,
        hydration,
        hp,
        row.get("max_hp"),
        obs,
        row.get("loyalty"),
    )
    threat_kinds = _threat_kinds()
    nearest_threat = None
    nearest_soul = None
    for ob in obs:
        kind = str(ob.get("kind", ""))
        d = float(ob.get("distance", 1e9))
        opos = list(ob.get("position") or [0.0, 0.0])
        if kind in threat_kinds and (
            nearest_threat is None or d < nearest_threat["distance"]
        ):
            nearest_threat = {"position": opos, "distance": d}
        if kind == "soul" and (nearest_soul is None or d < nearest_soul["distance"]):
            nearest_soul = {
                "kind": kind,
                "distance": d,
                "distance_band": _distance_band(d),
                "position": opos,
            }
    node_info = _nearest_node(provider, pos)
    x, y = pos
    w, h = database.SCREEN_BOUNDS
    region = "commons"
    on_own_plot = False
    has_food = False
    has_water = False
    try:
        gx, gy = plots.plot_at(x, y)
        region = (
            "edge"
            if x < 200 or x > w - 200 or y < 200 or y > h - 200
            else plots.kind_of(gx, gy)
        )
        with database.get_db() as conn:
            plot = plots.get_plot(conn, plots.plot_id_for(gx, gy))
            pack = resources.inventory_for(conn, soul_id)
        has_food = pack.get("food", 0) > 0
        has_water = pack.get("water", 0) > 0
        if (
            plot is not None
            and plot.get("owner_type") == plots.OWNER_SOUL
            and plot.get("owner_id") == soul_id
        ):
            on_own_plot = True
    except Exception:
        pass
    custodian = row.get("custodian_id")
    tamer_present = False
    seconds_since_tamer = None
    last_tamer_kind = None
    if custodian:
        reported = presence.get_presence(str(custodian), now)
        if reported is not None:
            tamer_present = reported.get("presence") != "away" and not reported.get(
                "stale", True
            )
            seconds_since_tamer = max(0.0, now - float(reported.get("updated_at", now)))
            last_tamer_kind = reported.get("last_event")
    try:
        vx, vy = (
            (float(v) for v in json.loads(row["velocity"]))
            if isinstance(row.get("velocity"), str)
            else (list(row.get("velocity") or [0.0, 0.0]))
        )
    except (ValueError, TypeError):
        vx, vy = 0.0, 0.0
    moving = bool(row.get("move_target")) or vx != 0.0 or vy != 0.0
    return {
        "soul_id": soul_id,
        "at": now,
        "event_kinds": list(kinds),
        "position": pos,
        "state": row.get("state") or "normal",
        "moving": moving,
        "drives": drive_vec,
        "fear": float(drive_vec.get("fear", 0.0)),
        "satiety": satiety,
        "hydration": hydration,
        "hp": hp,
        "need_bands": {
            "satiety": _band(satiety),
            "hydration": _band(hydration),
            "hp": _band(hp),
        },
        "sensations": [s.get("text", "") for s in sensations.recent(soul_id, 4)],
        "nearest_threat": nearest_threat,
        "nearest_soul": nearest_soul,
        "nearby_souls": sum(1 for ob in obs if ob.get("kind") == "soul"),
        "tamer_present": tamer_present,
        "seconds_since_tamer": seconds_since_tamer,
        "last_tamer_kind": last_tamer_kind,
        "region": region,
        "on_own_plot": on_own_plot,
        "seconds_since_last_event": None
        if last_event_at is None
        else max(0.0, now - last_event_at),
        "essence": float(row.get("essence") or 0.0),
        "nature": str(row.get("nature") or ""),
        "has_food": has_food,
        "has_water": has_water,
        "node_nearby": bool(node_info and node_info["distance"] <= 150.0),
        "node_exists": node_info is not None,
        "nearest_node": node_info,
    }


def _nearest_node(provider, pos: list) -> dict | None:
    x, y = pos
    best = None
    for finder in (provider.nearest_food_node, provider.nearest_water_node):
        node = finder(x, y)
        if node is None:
            continue
        d = math.hypot(float(node["x"]) - x, float(node["y"]) - y)
        if best is None or d < best["distance"]:
            best = {
                "node_id": node.get("node_id", ""),
                "kind": node.get("kind", ""),
                "position": [float(node["x"]), float(node["y"])],
                "distance": d,
            }
    return best


def survival_tripped(snapshot: dict) -> bool:
    return (
        snapshot.get("satiety", 100.0) < biology.NEED_BAND_LOW
        or snapshot.get("hydration", 100.0) < biology.NEED_BAND_LOW
        or snapshot.get("fear", 0.0) > reflex.FLEE_FEAR
    )


def _active_goals(judgments: dict, snapshot: dict, kinds: list[str]) -> set[str]:
    active = set()
    for goal, question in _QUESTION_GOAL.items():
        score = judgments.get(question)
        if score is not None and score >= jev.JEV_NOUL_THRESHOLD:
            active.add(goal)
    if snapshot.get("nearest_soul") is not None:
        active.add(GOAL_SOCIALIZE)
    if "restlessness" in kinds or "wander_continue" in kinds:
        active.add(GOAL_EXPLORE)
    return active


def active_goals(judgments: dict, snapshot: dict, kinds: list[str]) -> list[str]:
    return [g for g in _GOAL_PRIORITY if g in _active_goals(judgments, snapshot, kinds)]


def arbitrate(
    soul_id: str,
    judgments: dict,
    snapshot: dict,
    cooldowns: Cooldowns,
    now: float,
    kinds: list[str],
) -> str | None:
    active = _active_goals(judgments, snapshot, kinds)
    for goal in _GOAL_PRIORITY:
        if goal in active and cooldowns.ready(soul_id, goal, now):
            return goal
    return None


def plan_for_goal(goal: str, facts: dict) -> list[str]:
    from goapauto import Goal, Planner, WorldState

    if goal == GOAL_FLEE:
        actions = [("move_to", {"threatened": True}, {"threatened": False}, 1.0)]
        state = {"threatened": True}
    elif goal == GOAL_SATE:
        actions = [
            (
                "move_to",
                {"node_exists": True, "node_nearby": False},
                {"node_nearby": True},
                2.0,
            ),
            ("gather", {"node_nearby": True}, {"has_food": True}, 1.0),
            ("eat", {"has_food": True}, {"needs_pressing": False}, 1.0),
            ("drink", {"has_water": True}, {"needs_pressing": False}, 1.0),
        ]
        state = {
            "node_exists": bool(facts.get("node_exists")),
            "node_nearby": bool(facts.get("node_nearby")),
            "has_food": bool(facts.get("has_food")),
            "has_water": bool(facts.get("has_water")),
            "needs_pressing": True,
        }
    else:
        effect = _GOAL_FACTS.get(goal)
        action_name = _GOAL_ACTION.get(goal)
        if effect is None or action_name is None:
            return []
        actions = [(action_name, {}, dict(effect), 1.0)]
        state = {}
    planner = Planner(actions_list=actions, verbose=False)
    result = planner.generate_plan(
        WorldState(**state), Goal(target_state=_GOAL_FACTS[goal], priority=1, name=goal)
    )
    return list(result.plan) if result.plan else []


def payload_for(goal: str, action: str, snapshot: dict) -> dict:
    pos = snapshot.get("position") or [0.0, 0.0]
    if goal == GOAL_FLEE and action == "move_to":
        threat = snapshot.get("nearest_threat")
        if threat is None:
            rng = random.Random(f"flee:{snapshot.get('soul_id', '')}")
            angle = rng.uniform(0.0, 2.0 * math.pi)
            tx, ty = (
                pos[0] + math.cos(angle) * FLEE_AWAY_U,
                pos[1] + math.sin(angle) * FLEE_AWAY_U,
            )
        else:
            tp = threat["position"]
            dx, dy = pos[0] - tp[0], pos[1] - tp[1]
            d = math.hypot(dx, dy) or 1.0
            tx, ty = pos[0] + dx / d * FLEE_AWAY_U, pos[1] + dy / d * FLEE_AWAY_U
        return {"x": tx, "y": ty}
    if goal == GOAL_SATE and action == "gather":
        node = snapshot.get("nearest_node") or {}
        return {"node_id": node.get("node_id", "")}
    if goal == GOAL_SATE and action == "move_to":
        node = snapshot.get("nearest_node") or {}
        npos = node.get("position") or pos
        return {"x": float(npos[0]), "y": float(npos[1])}
    if goal == GOAL_SATE and action in ("eat", "drink"):
        return {}
    if goal == GOAL_SOCIALIZE and action == "move_to":
        other = (snapshot.get("nearest_soul") or {}).get("position") or pos
        dx, dy = other[0] - pos[0], other[1] - pos[1]
        d = math.hypot(dx, dy) or 1.0
        stop = max(0.0, d - SOCIALIZE_APPROACH_GAP)
        return {
            "x": pos[0] + dx / d * stop,
            "y": pos[1] + dy / d * stop,
            "pace": "amble",
        }
    return {}


class WanderManager:
    def __init__(self, seed: int = 0) -> None:
        self._seed = seed
        self._seq: dict[str, int] = {}
        self._legs: dict[str, list[tuple[float, float]]] = {}
        self._dwell: dict[str, float] = {}

    def start(self, soul_id: str, x: float, y: float) -> list[tuple[float, float]]:
        seq = self._seq.get(soul_id, 0) + 1
        self._seq[soul_id] = seq
        rng = random.Random(f"{self._seed}:{soul_id}:{seq}")
        w, h = database.SCREEN_BOUNDS
        legs: list[tuple[float, float]] = []
        cx, cy = x, y
        for _ in range(rng.randint(jev.WANDER_LEGS_MIN, jev.WANDER_LEGS_MAX)):
            angle = rng.uniform(0.0, 2.0 * math.pi)
            dist = rng.uniform(jev.WANDER_LEG_MIN_U, jev.WANDER_LEG_MAX_U)
            cx = min(max(cx + math.cos(angle) * dist, 10.0), w - 10.0)
            cy = min(max(cy + math.sin(angle) * dist, 10.0), h - 10.0)
            legs.append((cx, cy))
        self._legs[soul_id] = list(legs)
        self._dwell[soul_id] = rng.uniform(
            jev.WANDER_DWELL_MIN_S, jev.WANDER_DWELL_MAX_S
        )
        return legs

    def active(self, soul_id: str) -> bool:
        return soul_id in self._legs

    def legs_remaining(self, soul_id: str) -> int:
        return len(self._legs.get(soul_id, []))

    def next_leg(self, soul_id: str) -> tuple[float, float] | None:
        legs = self._legs.get(soul_id)
        if not legs:
            return None
        leg = legs.pop(0)
        if not legs:
            self._legs.pop(soul_id, None)
        return leg

    def dwell_for(self, soul_id: str) -> float:
        return self._dwell.get(soul_id, 3.0)

    def cancel(self, soul_id: str) -> None:
        self._legs.pop(soul_id, None)
        self._dwell.pop(soul_id, None)
