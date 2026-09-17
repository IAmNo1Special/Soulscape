"""Dirty-flag state machine driving the overlay render loop."""


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
