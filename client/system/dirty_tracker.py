"""Dirty-flag state machine driving the overlay render loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.soul.soul import Soul


def frame_needs_redraw(
    souls: list[Soul], sim_paused: bool, last_snapshot: list | None
) -> tuple[bool, list]:
    """Decide whether the overlay must redraw this frame.

    A position change always needs a redraw. Independently, any
    non-statue soul advances animation-only uniforms every frame
    (plasma pulse, hover bob, camera orbit via visual_tick), so the
    scene must keep redrawing while one is visible. Otherwise an
    idle soul freezes on a single frame until an unrelated event
    happens to mark the scene dirty.
    """
    snapshot = [
        (soul.biology.soul_id, round(soul.x, 3), round(soul.y, 3)) for soul in souls
    ]
    moved = snapshot != last_snapshot
    animating = not sim_paused and any(
        not soul.statue for soul in souls
    )
    return (moved or animating, snapshot)


class DirtyTracker:
    """Tracks whether the overlay scene needs a redraw.

    Starts dirty so the first frame always renders. Consumers call
    mark_dirty() when the scene changes and consume() once per frame;
    consume() returns True exactly once per mark.
    """

    def __init__(self) -> None:
        """Initializes the tracker in the dirty state."""
        self._dirty = True

    def mark_dirty(self) -> None:
        """Flag the scene as needing a redraw."""
        self._dirty = True

    def consume(self) -> bool:
        """Return True if dirty, clearing the flag.

        Returns:
            bool: True when a redraw was pending.
        """
        dirty = self._dirty
        self._dirty = False
        return dirty

    @property
    def is_dirty(self) -> bool:
        """Return True when a redraw is pending without clearing the flag.

        Returns:
            bool: Current dirty state.
        """
        return self._dirty
