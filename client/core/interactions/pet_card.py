"""Viewport info card content (issue #31).

Pure data builder for the right-click info card: needs, activity,
essence, whereabouts, and presence. The app renders the returned
lines; this module stays headless and testable.
"""

from __future__ import annotations


def build_info_card(
    soul_id: str,
    identity: dict | None,
    biology: dict | None,
    essence: float | None,
    whereabouts: str,
    presence: str,
) -> list[str]:
    """Build the card lines for a soul from viewport state.

    identity: {"name", "species", "level", "activity"} (may be None).
    biology: {"satiety", "hydration", "hp", "max_hp"} (may be None).
    presence: "online" | "stale" | "offline".
    """
    identity = identity or {}
    biology = biology or {}
    name = identity.get("name") or soul_id[:8]
    species = identity.get("species") or "Unknown"
    level = identity.get("level") or 1
    activity = identity.get("activity") or "idle"
    lines = [
        f"{name}",
        f"{species} - Lvl {level}",
        f"activity: {activity}",
    ]
    satiety = biology.get("satiety")
    hydration = biology.get("hydration")
    hp = biology.get("hp")
    max_hp = biology.get("max_hp") or 100.0
    if satiety is not None or hydration is not None or hp is not None:
        needs = (
            f"needs: satiety {_fmt(satiety)} / "
            f"hydration {_fmt(hydration)} / "
            f"hp {_fmt(hp)}/{_fmt(max_hp)}"
        )
        lines.append(needs)
    if essence is not None:
        lines.append(f"essence: {essence:.1f}")
    lines.append(f"whereabouts: {whereabouts}")
    lines.append(f"presence: {presence}")
    return lines


def _fmt(value: float | None) -> str:
    return f"{value:.0f}" if value is not None else "?"


def presence_status(
    tracked: bool,
    stale: bool,
) -> str:
    """Presence word for the card: online / stale / offline."""
    if not tracked:
        return "offline"
    return "stale" if stale else "online"
