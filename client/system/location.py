"""Client-side plot location labels (issue #30).

Mirrors the pure geometry of server/plots.py (PLOT_SIZE, grid dims,
ring/kind math) so the tray Whereabouts submenu can label each soul
with a human location string from its Hub world coordinates, without
a Hub round-trip:

    "Commons (plot 4:5)"      -- the origin Commons
    "Road ring 3 (plot 1:7)"  -- unclaimable road rings
    "Wilds (plot 2:3)"        -- unclaimed claimable plot
    "Claimed plot 6:6"        -- claimed (owner unknown client-side)
    "home"                    -- soul with no streamed position yet

Souls whose Hub state is "traveling" read as "traveling..." -- that state
is reserved for v1.5 fast travel. Expedition souls stream abroad
summaries instead (issue #35): abroad_label() renders the away line
and abroad_tooltip() the tray tooltip, e.g.:

    "Brave is abroad — plot 7:3, foraging"
"""

from __future__ import annotations

import math

PLOT_SIZE = 120.0
KIND_COMMONS = "commons"
KIND_ROAD = "road"
KIND_CLAIMABLE = "claimable"

#: Tray glyph marking a soul that is away on expedition.
AWAY_GLYPH = "✈"


def grid_dims(bounds: tuple[float, float] | None) -> tuple[int, int]:
    width, height = bounds if bounds else (1920.0, 1080.0)
    return max(1, math.ceil(width / PLOT_SIZE)), max(1, math.ceil(height / PLOT_SIZE))


def plot_at(
    x: float, y: float, bounds: tuple[float, float] | None = None
) -> tuple[int, int]:
    cols, rows = grid_dims(bounds)
    gx = min(max(int(x // PLOT_SIZE), 0), cols - 1)
    gy = min(max(int(y // PLOT_SIZE), 0), rows - 1)
    return gx, gy


def _origin(bounds: tuple[float, float] | None) -> tuple[int, int]:
    cols, rows = grid_dims(bounds)
    return cols // 2, rows // 2


def kind_of(
    grid_x: int, grid_y: int, bounds: tuple[float, float] | None = None
) -> tuple[str, int]:
    ox, oy = _origin(bounds)
    ring = max(abs(grid_x - ox), abs(grid_y - oy))
    if ring == 0:
        return KIND_COMMONS, ring
    if ring > 0 and ring % 3 == 0:
        return KIND_ROAD, ring
    return KIND_CLAIMABLE, ring


def plot_label(
    x: float,
    y: float,
    bounds: tuple[float, float] | None = None,
    claimed: bool = False,
) -> str:
    """Human location string for Hub world coordinates."""
    gx, gy = plot_at(x, y, bounds)
    kind, ring = kind_of(gx, gy, bounds)
    plot_id = f"{gx}:{gy}"
    if kind == KIND_COMMONS:
        return f"Commons (plot {plot_id})"
    if kind == KIND_ROAD:
        return f"Road ring {ring} (plot {plot_id})"
    if claimed:
        return f"Claimed plot {plot_id}"
    return f"Wilds (plot {plot_id})"


def whereabouts_label(
    state: str | None,
    x: float | None,
    y: float | None,
    bounds: tuple[float, float] | None = None,
) -> str:
    """Whereabouts string for the tray submenu.

    Traveling souls show "traveling..." -- destinations arrive with #35.
    Souls with no position yet read as "home".
    """
    if state == "traveling":
        return "traveling..."
    if x is None or y is None:
        return "home"
    return plot_label(x, y, bounds)


def abroad_label(summary: dict) -> str:
    """Away line for the tray Whereabouts submenu (issue #35).

    Renders from the 4-key abroad summary only -- the client never
    knows the soul's fine position while it is away.
    """
    return (
        f"{AWAY_GLYPH} abroad — plot {summary.get('plot', '?')}, "
        f"{summary.get('activity_label', '?')}"
    )


def abroad_tooltip(name: str, summary: dict) -> str:
    """Tray tooltip for an away soul, e.g.
    "Brave is abroad — plot 7:3, foraging"."""
    return (
        f"{name} is abroad — plot {summary.get('plot', '?')}, "
        f"{summary.get('activity_label', '?')}"
    )


def walkoff_direction(
    x: float, y: float, screen_w: float, screen_h: float
) -> tuple[float, float]:
    """Nearest-edge drift direction for the expedition walk-off (issue #35).

    A soul newly on the abroad channel keeps its sprite briefly while
    fading out and drifting toward the nearest screen edge along the
    dominant axis, so it visibly exits the scene instead of drifting
    back toward center. Returns (dx, dy) with the non-dominant axis
    zeroed.
    """
    dx = -1.0 if x < screen_w / 2 else 1.0
    dy = -1.0 if y < screen_h / 2 else 1.0
    if abs(x - screen_w / 2) > abs(y - screen_h / 2):
        dy = 0.0
    else:
        dx = 0.0
    return dx, dy
