"""Expeditions and the abroad channel (issue #35).

Expedition-lite long walks beyond the home rect, across neighbor plots.

Decisions (documented per the issue; code is the source of truth):

Home plot
    The soul's stable home plot, recorded per soul in soul_home_plots:
    the plot containing the soul's position when its first expedition
    departs, or the plot it most recently claimed (plot_claim sets it).
    "Home rect" for depart/return logic is this plot's 120 wu square.

Trigger: drive-driven, NOT deliberation-chosen
    Deliberation is metered, LLM-dependent, and fires rarely; an
    expedition needs a deterministic always-on trigger. A world-tick
    sweep (every TRIGGER_SWEEP_EVERY_TICKS) evaluates the drive vector
    (agents.drives, arithmetic only): curiosity >= CURIOSITY_MIN (0.6 --
    wanderlust is a nature trait: hasty/naive 0.65 and jolly/impish 0.6
    trigger, calmer natures never do) AND needs met (satiety >= 70,
    hydration >= 60, hp fraction >= 0.5). Cooldown: at most one
    expedition per soul per EXPEDITION_COOLDOWN_S (6 h). The soul must
    be awake (state normal, funded, uncollapsed, not carried) and
    standing in its home plot. Deliberation may still think while the
    soul is away; the expedition owns locomotion and re-steers every
    tick, so stray move intents are overridden until return.

State machine (per soul)
    home -> departing -> abroad -> returning -> home. Persisted in the
    expeditions table (ended_at NULL = active); there is no souls.state
    change -- biology.STATE_TRAVELING stays reserved for v1.5
    fast-travel.
    - departing: steered toward the first route waypoint (which lies
      beyond the home plot border); flips to abroad the tick the soul's
      plot stops being the home plot.
    - abroad: steered along route waypoints to the destination plot.
      On arrival the soul dwells (dwell_s); while dwelling it seeks the
      nearest ready resource node within NODE_SEEK_RADIUS_WU and forages
      on FORAGE_INTERVAL_S cadence -- inventory and XP accrue silently
      server-side (no journal row per gather, like #34's silent XP; the
      trip is bracketed by journal rows). When the dwell expires the
      route home is computed and the phase flips.
    - returning: steered home; the tick the soul's plot IS the home
      plot again the expedition ends, the arrival bubble fires, and the
      viewport stream resumes full state.
    - timeout: past EXPEDITION_MAX_S the expedition is forced home
      (route recomputed); a second timeout cancels it. Any routing
      failure cancels with a journaled reason.

Movement reuses the #14 movement pipeline (velocity/move_target
integration in world_tick), steered server-side each tick -- no client
intent round-trip. Carry (#31) is the contrast: carry is a
tamer-driven drag validated per move against the origin plot and a
120 wu leash, so it can NEVER cross a plot border; expeditions are
server-driven autonomous movement routed across plots through the
plot access-policy graph -- different path, different authority,
which is the whole point.

Plot routing rule
    Destinations are 2-5 plots (Chebyshev) from home. Routes are BFS
    over the 8-neighbor plot grid through plots.can_enter_plot only:
    commons, road rings, open plots, and the soul's own claimed plots
    are traversable; another soul's closed plot is never entered. No
    route -> the expedition is cancelled with a journaled reason
    (expedition_cancelled).

Abroad channel (privacy)
    While abroad/returning, the soul's viewport stream switches: fine
    position, lifecycle state, dormancy, biology, and identity deltas
    are withheld and the soul is replaced by a 1 Hz summary built by
    build_abroad_summary, which constructs the dict literally from an
    allowlist -- EXACTLY {entity_id, state, activity_label, plot}. No
    x/y, no viewport, no biology can ever appear; the privacy test
    fuzzes this. The away soul's fine position never leaves the Hub.

Bubbles
    Departure: "Off to explore plot {dest}!" and return: "Back home
    from plot {dest}!" via the #30 bubble seam (kind "expedition",
    unsolicited -- subject to the client's noise caps like mailbag
    questions). The destination arrival is journaled, not bubbled:
    departure and return are the only cinematic moments.

Journaling (typed, #27)
    expedition_started / expedition_arrived / expedition_returned /
    expedition_cancelled. Loyalty is untouched -- expeditions are
    neutral (no nudge either way).
"""

from __future__ import annotations

import json
import logging
import math
import random
import secrets
import sqlite3
import time

from . import affection
from . import biology
from . import database
from . import dormancy
from . import persistence
from . import plots
from . import resources
from .agents import drives

logger = logging.getLogger("soulscape_hub")

STATE_DEPARTING = "departing"
STATE_ABROAD = "abroad"
STATE_RETURNING = "returning"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"
ACTIVE_STATES = (STATE_DEPARTING, STATE_ABROAD, STATE_RETURNING)

ABROAD_STREAM_STATES = (STATE_ABROAD, STATE_RETURNING)
ABROAD_CADENCE_S = 1.0
TRIGGER_SWEEP_EVERY_TICKS = 300
CURIOSITY_MIN = 0.6
SATIETY_MIN = 70.0
HYDRATION_MIN = 60.0
HP_FRAC_MIN = 0.5
EXPEDITION_COOLDOWN_S = 6.0 * 3600.0
EXPEDITION_DWELL_S = 30.0 * 60.0
EXPEDITION_MAX_S = 4.0 * 3600.0
EXPEDITION_MOVE_SPEED = 600.0
NODE_SEEK_RADIUS_WU = 80.0
FORAGE_INTERVAL_S = 300.0
DEST_MIN_DIST = 2
DEST_MAX_DIST = 5

EVENT_EXPEDITION_STARTED = "expedition_started"
EVENT_EXPEDITION_ARRIVED = "expedition_arrived"
EVENT_EXPEDITION_RETURNED = "expedition_returned"
EVENT_EXPEDITION_CANCELLED = "expedition_cancelled"

ABROAD_SUMMARY_KEYS = ("entity_id", "state", "activity_label", "plot")

BUBBLE_KIND_EXPEDITION = "expedition"


class ExpeditionRefusal(Exception):
    """Business-logic refusal to start an expedition."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create expedition tables (idempotent)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS expeditions (
            expedition_id TEXT PRIMARY KEY,
            soul_id TEXT NOT NULL,
            state TEXT NOT NULL
                CHECK (state IN
                    ('departing','abroad','returning','done','cancelled')),
            home_plot_id TEXT NOT NULL,
            dest_plot_id TEXT NOT NULL,
            waypoints TEXT NOT NULL,
            waypoint_index INTEGER NOT NULL DEFAULT 0,
            dwell_s REAL NOT NULL DEFAULT 1800.0,
            dwell_until REAL,
            last_foraged_at REAL,
            started_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            ended_at REAL,
            cancel_reason TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_expeditions_soul "
        "ON expeditions(soul_id, ended_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS soul_home_plots (
            soul_id TEXT PRIMARY KEY,
            plot_id TEXT NOT NULL,
            assigned_at REAL NOT NULL
        )
        """
    )


def get_home_plot(conn: sqlite3.Connection, soul_id: str) -> str | None:
    """The soul's home plot, or None before the first departure/claim."""
    row = conn.execute(
        "SELECT plot_id FROM soul_home_plots WHERE soul_id = ?", (soul_id,)
    ).fetchone()
    return str(row["plot_id"]) if row else None


def set_home_plot(
    conn: sqlite3.Connection, soul_id: str, plot_id: str, now: float
) -> None:
    """Record (or re-home) a soul's home plot. Called on plot_claim
    and on first expedition departure."""
    conn.execute(
        "INSERT INTO soul_home_plots (soul_id, plot_id, assigned_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(soul_id) DO UPDATE SET plot_id = excluded.plot_id, "
        "assigned_at = excluded.assigned_at",
        (soul_id, plot_id, now),
    )


def _parse_plot_id(plot_id: str) -> tuple[int, int]:
    gx_s, gy_s = plot_id.split(":")
    return int(gx_s), int(gy_s)


def _current_plot(x: float, y: float) -> str:
    return plots.plot_id_for(*plots.plot_at(x, y))


def _enterable(conn: sqlite3.Connection, soul_id: str, gx: int, gy: int) -> bool:
    """The expedition routing rule: can_enter_plot at the plot center.

    Commons, road rings, open plots, and the soul's own claimed plots
    are traversable; another soul's closed plot is never entered.
    """
    cx, cy = plots.plot_center(gx, gy)
    return plots.can_enter_plot(conn, soul_id, cx, cy)


def route_plots(
    conn: sqlite3.Connection,
    soul_id: str,
    from_plot_id: str,
    to_plot_id: str,
) -> list[str] | None:
    """BFS over the 8-neighbor plot grid through enterable plots only.

    Returns the plot-id path including both endpoints, or None when no
    route exists (the caller cancels with a journaled reason).
    """
    start = _parse_plot_id(from_plot_id)
    goal = _parse_plot_id(to_plot_id)
    if start == goal:
        return [from_plot_id]
    cols, rows = plots.grid_dims()
    if not _enterable(conn, soul_id, *goal):
        return None
    prev: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
    queue: list[tuple[int, int]] = [start]
    while queue:
        cur = queue.pop(0)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nxt = (cur[0] + dx, cur[1] + dy)
                if not (0 <= nxt[0] < cols and 0 <= nxt[1] < rows):
                    continue
                if nxt in prev or not _enterable(conn, soul_id, *nxt):
                    continue
                prev[nxt] = cur
                if nxt == goal:
                    path = [nxt]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])  # type: ignore[arg-type]
                    path.reverse()
                    return [plots.plot_id_for(gx, gy) for gx, gy in path]
                queue.append(nxt)
    return None


def choose_destination(
    conn: sqlite3.Connection,
    soul_id: str,
    home_plot_id: str,
    rng: random.Random | None = None,
) -> str | None:
    """Pick an enterable destination 2-5 plots (Chebyshev) from home."""
    rng = rng if rng is not None else random.Random()
    hgx, hgy = _parse_plot_id(home_plot_id)
    cols, rows = plots.grid_dims()
    candidates = [
        plots.plot_id_for(gx, gy)
        for gx in range(cols)
        for gy in range(rows)
        if DEST_MIN_DIST <= max(abs(gx - hgx), abs(gy - hgy)) <= DEST_MAX_DIST
        and _enterable(conn, soul_id, gx, gy)
    ]
    return rng.choice(candidates) if candidates else None


def _waypoint_centers(path: list[str]) -> list[list[float]]:
    return [list(plots.plot_center(*_parse_plot_id(pid))) for pid in path[1:]]


def _activity_label(exp: dict) -> str:
    state = exp["state"]
    if state == STATE_RETURNING:
        return "returning home"
    if state == STATE_DEPARTING:
        return "departing"
    last_foraged = exp.get("last_foraged_at") or 0.0
    if time.time() - last_foraged < FORAGE_INTERVAL_S:
        return "foraging"
    if exp.get("dwell_until"):
        return "resting"
    return "exploring"


def build_abroad_summary(exp: dict, plot_id: str) -> dict:
    """Build the abroad summary from an allowlist.

    The dict is constructed key-by-key -- never a stripped full soul
    dict -- so no serializer change can leak position, viewport, or
    biology. ``exp`` is the expedition row dict; ``plot_id`` is the
    soul's current plot.
    """
    summary = {
        "entity_id": exp["soul_id"],
        "state": exp["state"],
        "activity_label": _activity_label(exp),
        "plot": plot_id,
    }
    assert set(summary.keys()) == set(ABROAD_SUMMARY_KEYS)
    return summary


def abroad_summaries() -> dict[str, dict]:
    """Current abroad-channel summaries keyed by soul_id.

    Only souls in abroad/returning stream here; departing souls are
    still inside the home plot and stream position normally.
    """
    with database.get_db() as conn:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM expeditions WHERE ended_at IS NULL AND state IN (?, ?)",
            ABROAD_STREAM_STATES,
        ).fetchall()
    if not rows:
        return {}
    positions = persistence.read_positions_through()
    out: dict[str, dict] = {}
    for row in rows:
        exp = dict(row)
        pos = positions.get(exp["soul_id"])
        if pos is None:
            continue
        out[exp["soul_id"]] = build_abroad_summary(exp, _current_plot(*pos))
    return out


def active_expedition(conn: sqlite3.Connection, soul_id: str) -> dict | None:
    """The soul's active expedition row, or None."""
    row = conn.execute(
        "SELECT * FROM expeditions WHERE soul_id = ? AND ended_at IS NULL",
        (soul_id,),
    ).fetchone()
    return dict(row) if row else None


def last_ended_at(conn: sqlite3.Connection, soul_id: str) -> float | None:
    row = conn.execute(
        "SELECT MAX(ended_at) AS m FROM expeditions "
        "WHERE soul_id = ? AND ended_at IS NOT NULL",
        (soul_id,),
    ).fetchone()
    return float(row["m"]) if row and row["m"] is not None else None


def _owner_of(conn: sqlite3.Connection, soul_id: str) -> str | None:
    row = conn.execute(
        "SELECT owner_id FROM souls WHERE soul_id = ?", (soul_id,)
    ).fetchone()
    return str(row["owner_id"]) if row and row["owner_id"] else None


def _fan_bubble(owner_id: str | None, soul_id: str, text: str) -> None:
    """Post-commit fan-out of an expedition bubble (kind "expedition",
    unsolicited -- the client's noise caps apply, like #32 questions)."""
    if not owner_id:
        return
    from . import viewport

    viewport.viewport.notify_bubble(
        owner_id,
        soul_id,
        text,
        kind=BUBBLE_KIND_EXPEDITION,
        solicited=False,
    )


def _parse_position(raw: object) -> tuple[float, float] | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        x, y = float(raw[0]), float(raw[1])  # type: ignore[index]
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        return (x, y)
    except (ValueError, TypeError, IndexError):
        return None


def start_expedition(
    conn: sqlite3.Connection,
    soul_id: str,
    now: float,
    tick_id: int,
    dest_plot_id: str | None = None,
    dwell_s: float | None = None,
    rng: random.Random | None = None,
) -> tuple[dict, str]:
    """Create an expedition (departing). No commit -- the caller owns
    the transaction and fans the returned bubble text post-commit.

    Raises ExpeditionRefusal when the soul may not depart.
    """
    ensure_schema(conn)
    row = conn.execute(
        "SELECT position, state, COALESCE(essence, 0.0) AS essence "
        "FROM souls WHERE soul_id = ?",
        (soul_id,),
    ).fetchone()
    if row is None:
        raise ExpeditionRefusal("soul_not_found", f"soul {soul_id} not found")
    if (row["state"] or biology.STATE_NORMAL) != biology.STATE_NORMAL:
        raise ExpeditionRefusal("not_awake", "only awake souls depart")
    if dormancy.is_dormant(row["essence"]):
        raise ExpeditionRefusal("soul_dormant", "dormant souls don't wander")
    if active_expedition(conn, soul_id) is not None:
        raise ExpeditionRefusal("already_abroad", "expedition already active")
    pos = _parse_position(row["position"])
    if pos is None:
        raise ExpeditionRefusal("no_position", "soul has no position")
    home = get_home_plot(conn, soul_id)
    if home is None:
        home = _current_plot(*pos)
        set_home_plot(conn, soul_id, home, now)
    if _current_plot(*pos) != home:
        raise ExpeditionRefusal("not_home", "expeditions depart from the home plot")
    if dest_plot_id is None:
        dest_plot_id = choose_destination(conn, soul_id, home, rng)
    if dest_plot_id is None:
        raise ExpeditionRefusal("no_destination", "no reachable destination plot")
    hgx, hgy = _parse_plot_id(home)
    dgx, dgy = _parse_plot_id(dest_plot_id)
    if max(abs(dgx - hgx), abs(dgy - hgy)) < DEST_MIN_DIST:
        raise ExpeditionRefusal("dest_too_close", "destination must be 2+ plots away")
    if not _enterable(conn, soul_id, dgx, dgy):
        raise ExpeditionRefusal("dest_closed", "destination plot is not enterable")
    path = route_plots(conn, soul_id, home, dest_plot_id)
    if not path or len(path) < 3:
        raise ExpeditionRefusal("no_route", "no enterable route to destination")
    expedition_id = "exp_" + secrets.token_urlsafe(12)
    conn.execute(
        "INSERT INTO expeditions "
        "(expedition_id, soul_id, state, home_plot_id, dest_plot_id, "
        "waypoints, waypoint_index, dwell_s, dwell_until, last_foraged_at, "
        "started_at, updated_at, ended_at, cancel_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, ?, ?, NULL, NULL)",
        (
            expedition_id,
            soul_id,
            STATE_DEPARTING,
            home,
            dest_plot_id,
            json.dumps(_waypoint_centers(path)),
            dwell_s if dwell_s is not None else EXPEDITION_DWELL_S,
            now,
            now,
        ),
    )
    persistence.append_event(
        conn,
        tick_id,
        EVENT_EXPEDITION_STARTED,
        {
            "soul_id": soul_id,
            "expedition_id": expedition_id,
            "home_plot": home,
            "dest_plot": dest_plot_id,
        },
    )
    exp = active_expedition(conn, soul_id)
    assert exp is not None
    return exp, f"Off to explore plot {dest_plot_id}!"


def cancel_expedition(
    conn: sqlite3.Connection,
    soul_id: str,
    reason: str,
    tick_id: int,
    now: float,
) -> dict | None:
    """Cancel the active expedition with a journaled reason. No commit;
    the caller owns the transaction. Returns the ended row, if any."""
    exp = active_expedition(conn, soul_id)
    if exp is None:
        return None
    conn.execute(
        "UPDATE expeditions SET state = ?, ended_at = ?, updated_at = ?, "
        "cancel_reason = ? WHERE expedition_id = ?",
        (STATE_CANCELLED, now, now, reason, exp["expedition_id"]),
    )
    conn.execute(
        "UPDATE souls SET velocity = ?, move_target = NULL WHERE soul_id = ?",
        (json.dumps([0.0, 0.0]), soul_id),
    )
    persistence.append_event(
        conn,
        tick_id,
        EVENT_EXPEDITION_CANCELLED,
        {
            "soul_id": soul_id,
            "expedition_id": exp["expedition_id"],
            "reason": reason,
            "from_state": exp["state"],
        },
    )
    persistence.dirty.mark(soul_id, velocity=[0.0, 0.0], move_target=None)
    exp = dict(exp)
    exp["state"] = STATE_CANCELLED
    exp["ended_at"] = now
    exp["cancel_reason"] = reason
    return exp


def _steer(
    conn: sqlite3.Connection,
    soul_id: str,
    x: float,
    y: float,
    tx: float,
    ty: float,
    satiety: float | None,
    steer_marks: list,
) -> None:
    """Server-side steering through the #14 movement pipeline: set
    velocity/move_target; world_tick integrates it the same tick."""
    speed = EXPEDITION_MOVE_SPEED * biology.speed_multiplier(
        biology.full_or_100(satiety)
    )
    dx, dy = tx - x, ty - y
    dist = math.hypot(dx, dy)
    if dist <= persistence.ARRIVAL_EPS:
        velocity: list[float] = [0.0, 0.0]
        target: list[float] | None = None
    else:
        velocity = [dx / dist * speed, dy / dist * speed]
        target = [tx, ty]
    conn.execute(
        "UPDATE souls SET velocity = ?, move_target = ? WHERE soul_id = ?",
        (
            json.dumps(velocity),
            json.dumps(target) if target is not None else None,
            soul_id,
        ),
    )
    steer_marks.append((soul_id, velocity, target))


def _zero(conn: sqlite3.Connection, soul_id: str, steer_marks: list) -> None:
    conn.execute(
        "UPDATE souls SET velocity = ?, move_target = NULL WHERE soul_id = ?",
        (json.dumps([0.0, 0.0]), soul_id),
    )
    steer_marks.append((soul_id, [0.0, 0.0], None))


def _set_phase(
    conn: sqlite3.Connection, exp: dict, state: str, now: float
) -> dict:
    conn.execute(
        "UPDATE expeditions SET state = ?, updated_at = ? WHERE expedition_id = ?",
        (state, now, exp["expedition_id"]),
    )
    exp = dict(exp)
    exp["state"] = state
    exp["updated_at"] = now
    return exp


def _forage(
    conn: sqlite3.Connection,
    exp: dict,
    soul_id: str,
    node: dict,
    now: float,
) -> None:
    """One server-side abroad gather: node -> inventory + silent XP.

    Reuses the #34 resource primitives; no journal row per gather (like
    #34's silent XP -- the trip is bracketed by started/arrived/returned).
    """
    fresh = resources.get_node(conn, node["node_id"])
    if (
        fresh is None
        or fresh["state"] != resources.STATE_READY
        or fresh["amount"] <= 0
    ):
        return
    new_amount = fresh["amount"] - resources.GATHER_YIELD
    if new_amount <= 0:
        conn.execute(
            "UPDATE resource_nodes SET amount = 0, state = 'depleted', "
            "respawns_at = ? WHERE node_id = ?",
            (now + resources.RESPAWN_SECONDS, node["node_id"]),
        )
    else:
        conn.execute(
            "UPDATE resource_nodes SET amount = ? WHERE node_id = ?",
            (new_amount, node["node_id"]),
        )
    resources.add_item(conn, soul_id, fresh["kind"], resources.GATHER_YIELD)
    resources.award_xp(conn, soul_id, resources.XP_GATHER)
    conn.execute(
        "UPDATE expeditions SET last_foraged_at = ?, updated_at = ? "
        "WHERE expedition_id = ?",
        (now, now, exp["expedition_id"]),
    )
    exp["last_foraged_at"] = now


def _nearest_forage_node(
    conn: sqlite3.Connection, soul_id: str, x: float, y: float
) -> dict | None:
    """Nearest ready node of any kind within foraging reach, on an
    enterable plot; the expedition must not forage inside a closed
    plot it may not enter."""
    best: dict | None = None
    best_d = NODE_SEEK_RADIUS_WU
    for kind in resources.NODE_KINDS:
        node = resources.nearest_node(conn, kind, x, y)
        if node is None:
            continue
        d = math.hypot(node["x"] - x, node["y"] - y)
        if d >= best_d:
            continue
        ngx, ngy = plots.plot_at(node["x"], node["y"])
        if not _enterable(conn, soul_id, ngx, ngy):
            continue
        best, best_d = node, d
    return best


def _advance_one(
    conn: sqlite3.Connection,
    tick_id: int,
    now: float,
    exp: dict,
    pos: tuple[float, float],
    satiety: float | None,
    steer_marks: list,
    bubbles: list,
) -> None:
    soul_id = exp["soul_id"]
    x, y = pos
    home = exp["home_plot_id"]
    cur_plot = _current_plot(x, y)
    state = exp["state"]

    if state == STATE_DEPARTING:
        if cur_plot != home:
            _set_phase(conn, exp, STATE_ABROAD, now)
            return
        waypoints = json.loads(exp["waypoints"])
        if not waypoints:
            cancel_expedition(conn, soul_id, "no_waypoints", tick_id, now)
            return
        tx, ty = waypoints[0]
        _steer(conn, soul_id, x, y, tx, ty, satiety, steer_marks)
        return

    if state == STATE_ABROAD:
        waypoints = json.loads(exp["waypoints"])
        idx = int(exp["waypoint_index"])
        if idx < len(waypoints):
            tx, ty = waypoints[idx]
            if math.hypot(tx - x, ty - y) <= persistence.ARRIVAL_EPS:
                idx += 1
                exp["waypoint_index"] = idx
                conn.execute(
                    "UPDATE expeditions SET waypoint_index = ?, updated_at = ? "
                    "WHERE expedition_id = ?",
                    (idx, now, exp["expedition_id"]),
                )
                if idx >= len(waypoints):
                    dwell_until = now + float(exp["dwell_s"])
                    exp["dwell_until"] = dwell_until
                    conn.execute(
                        "UPDATE expeditions SET dwell_until = ? "
                        "WHERE expedition_id = ?",
                        (dwell_until, exp["expedition_id"]),
                    )
                    _zero(conn, soul_id, steer_marks)
                    persistence.append_event(
                        conn,
                        tick_id,
                        EVENT_EXPEDITION_ARRIVED,
                        {
                            "soul_id": soul_id,
                            "expedition_id": exp["expedition_id"],
                            "dest_plot": exp["dest_plot_id"],
                        },
                    )
            else:
                _steer(conn, soul_id, x, y, tx, ty, satiety, steer_marks)
            return
        dwell_until = exp.get("dwell_until") or 0.0
        if now >= dwell_until:
            path = route_plots(conn, soul_id, cur_plot, home)
            if not path:
                cancel_expedition(conn, soul_id, "no_route_home", tick_id, now)
                return
            exp["state"] = STATE_RETURNING
            exp["waypoint_index"] = 0
            conn.execute(
                "UPDATE expeditions SET state = ?, waypoints = ?, "
                "waypoint_index = 0, updated_at = ? WHERE expedition_id = ?",
                (
                    STATE_RETURNING,
                    json.dumps(_waypoint_centers(path)),
                    now,
                    exp["expedition_id"],
                ),
            )
            return
        node = _nearest_forage_node(conn, soul_id, x, y)
        if node is None:
            _zero(conn, soul_id, steer_marks)
            return
        d = math.hypot(node["x"] - x, node["y"] - y)
        if d > resources.GATHER_REACH_WU:
            _steer(conn, soul_id, x, y, node["x"], node["y"], satiety, steer_marks)
            return
        last_foraged = exp.get("last_foraged_at") or 0.0
        if now - last_foraged >= FORAGE_INTERVAL_S:
            _forage(conn, exp, soul_id, node, now)
        else:
            _zero(conn, soul_id, steer_marks)
        return

    if state == STATE_RETURNING:
        if cur_plot == home:
            conn.execute(
                "UPDATE expeditions SET state = ?, ended_at = ?, updated_at = ? "
                "WHERE expedition_id = ?",
                (STATE_DONE, now, now, exp["expedition_id"]),
            )
            _zero(conn, soul_id, steer_marks)
            persistence.append_event(
                conn,
                tick_id,
                EVENT_EXPEDITION_RETURNED,
                {
                    "soul_id": soul_id,
                    "expedition_id": exp["expedition_id"],
                    "dest_plot": exp["dest_plot_id"],
                    "home_plot": home,
                },
            )
            bubbles.append((soul_id, f"Back home from plot {exp['dest_plot_id']}!"))
            return
        waypoints = json.loads(exp["waypoints"])
        idx = int(exp["waypoint_index"])
        if idx < len(waypoints):
            tx, ty = waypoints[idx]
            if math.hypot(tx - x, ty - y) <= persistence.ARRIVAL_EPS:
                conn.execute(
                    "UPDATE expeditions SET waypoint_index = ?, updated_at = ? "
                    "WHERE expedition_id = ?",
                    (idx + 1, now, exp["expedition_id"]),
                )
            else:
                _steer(conn, soul_id, x, y, tx, ty, satiety, steer_marks)
        else:
            _zero(conn, soul_id, steer_marks)


def advance_tick(tick_id: int, now: float) -> None:
    """Advance every active expedition one tick: phase transitions plus
    server-side steering through the #14 movement pipeline. Never
    raises -- the world tick must not die on expedition work."""
    try:
        with database.get_db() as conn:
            ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM expeditions WHERE ended_at IS NULL"
            ).fetchall()
            if not rows:
                return
            positions = persistence.read_positions_through()
            carried = affection.carried_souls()
            conn.execute("BEGIN IMMEDIATE")
            steer_marks: list = []
            bubbles: list = []
            try:
                for row in rows:
                    exp = dict(row)
                    soul_id = exp["soul_id"]
                    if soul_id in carried:
                        continue
                    pos = positions.get(soul_id)
                    if pos is None:
                        continue
                    srow = conn.execute(
                        "SELECT state, satiety, COALESCE(essence, 0.0) AS essence "
                        "FROM souls WHERE soul_id = ?",
                        (soul_id,),
                    ).fetchone()
                    if srow is None:
                        cancel_expedition(conn, soul_id, "soul_gone", tick_id, now)
                        continue
                    if (srow["state"] or biology.STATE_NORMAL) != biology.STATE_NORMAL:
                        continue
                    if dormancy.is_dormant(srow["essence"]):
                        continue
                    if now - exp["started_at"] > EXPEDITION_MAX_S:
                        if exp["state"] == STATE_RETURNING:
                            cancel_expedition(conn, soul_id, "timeout", tick_id, now)
                        else:
                            path = route_plots(
                                conn, soul_id, _current_plot(*pos), exp["home_plot_id"]
                            )
                            if path:
                                conn.execute(
                                    "UPDATE expeditions SET state = ?, "
                                    "waypoints = ?, waypoint_index = 0, "
                                    "updated_at = ? WHERE expedition_id = ?",
                                    (
                                        STATE_RETURNING,
                                        json.dumps(_waypoint_centers(path)),
                                        now,
                                        exp["expedition_id"],
                                    ),
                                )
                            else:
                                cancel_expedition(
                                    conn, soul_id, "timeout_no_route", tick_id, now
                                )
                        continue
                    try:
                        _advance_one(
                            conn,
                            tick_id,
                            now,
                            exp,
                            pos,
                            srow["satiety"],
                            steer_marks,
                            bubbles,
                        )
                    except Exception:
                        logger.exception("expedition advance failed for %s", soul_id)
                        try:
                            cancel_expedition(conn, soul_id, "internal", tick_id, now)
                        except Exception:
                            logger.exception("expedition cancel failed for %s", soul_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        for soul_id, velocity, target in steer_marks:
            persistence.dirty.mark(soul_id, velocity=velocity, move_target=target)
        for soul_id, text in bubbles:
            with database.get_db() as oconn:
                _fan_bubble(_owner_of(oconn, soul_id), soul_id, text)
    except Exception:
        logger.exception("expedition advance_tick failed")


def _trigger_eligible(
    conn: sqlite3.Connection, soul: dict, now: float, carried: set[str]
) -> str | None:
    """Return a refusal reason, or None when the soul may depart."""
    soul_id = soul["soul_id"]
    if (soul["state"] or biology.STATE_NORMAL) != biology.STATE_NORMAL:
        return "not_awake"
    if dormancy.is_dormant(soul["essence"]):
        return "dormant"
    if soul_id in carried:
        return "carried"
    if active_expedition(conn, soul_id) is not None:
        return "already_abroad"
    ended = last_ended_at(conn, soul_id)
    if ended is not None and now - ended < EXPEDITION_COOLDOWN_S:
        return "cooldown"
    pos = _parse_position(soul.get("position"))
    if pos is None:
        return "no_position"
    home = get_home_plot(conn, soul_id)
    if home is not None and _current_plot(*pos) != home:
        return "not_home"
    vec = drives.compute_drives(
        soul.get("nature"),
        soul.get("satiety"),
        soul.get("hydration"),
        soul.get("hp"),
        soul.get("max_hp"),
        [],
        soul.get("loyalty"),
    )
    if vec["curiosity"] < CURIOSITY_MIN:
        return "low_curiosity"
    if (soul.get("satiety") or 0.0) < SATIETY_MIN:
        return "hungry"
    if (soul.get("hydration") or 0.0) < HYDRATION_MIN:
        return "thirsty"
    hp, max_hp = soul.get("hp"), soul.get("max_hp")
    if hp is not None and max_hp and float(hp) / float(max_hp) < HP_FRAC_MIN:
        return "hurt"
    return None


def maybe_trigger(tick_id: int, now: float) -> int:
    """Drive-driven expedition trigger sweep. Starts expeditions for
    eligible wanderlust souls. Returns the count started. Never raises.
    """
    started = 0
    try:
        with database.get_db() as conn:
            ensure_schema(conn)
            souls = conn.execute(
                "SELECT soul_id, nature, satiety, hydration, hp, max_hp, "
                "loyalty, state, COALESCE(essence, 0.0) AS essence, position "
                "FROM souls"
            ).fetchall()
        if not souls:
            return 0
        carried = affection.carried_souls()
        for srow in souls:
            soul = dict(srow)
            try:
                with database.get_db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        reason = _trigger_eligible(conn, soul, now, carried)
                        if reason is not None:
                            conn.rollback()
                            continue
                        _, bubble = start_expedition(conn, soul["soul_id"], now, tick_id)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                with database.get_db() as oconn:
                    owner = _owner_of(oconn, soul["soul_id"])
                _fan_bubble(owner, soul["soul_id"], bubble)
                started += 1
            except ExpeditionRefusal as refusal:
                logger.debug(
                    "expedition trigger refused for %s: %s",
                    soul["soul_id"],
                    refusal.reason,
                )
            except Exception:
                logger.exception("expedition trigger failed for %s", soul["soul_id"])
    except Exception:
        logger.exception("expedition maybe_trigger failed")
    return started
