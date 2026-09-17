"""Pet interaction gestures (issue #31).

The viewport grammar is affection/attention only -- no direct commands:

- short click (press + release, no drag)        -> chirp  (attention)
- press-and-hold >= 0.8 s (no drag)            -> pet    (affection)
- drag past the start threshold                -> carry  (grab / move / release)

The detector is headless: it consumes pointer events with an injected
clock and emits gesture events. The app maps events to signed intents
(see gesture_intent()) and converts screen coords to world coords.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

#: Hold duration that turns a press into a pet instead of a chirp.
HOLD_PET_THRESHOLD_S = 0.8
#: Pointer travel (px) that turns a press into a carry grab.
DRAG_START_PX = 6.0
#: Minimum seconds between streamed carry_move updates (bounded cadence).
CARRY_CADENCE_S = 0.2

CHIRP = "chirp"
PET = "pet"
CARRY_BEGIN = "carry_begin"
CARRY_MOVE = "carry_move"
CARRY_END = "carry_end"


@dataclass(frozen=True)
class GestureEvent:
    kind: str
    x: float
    y: float


class PetGestureDetector:
    """Press/drag/release state machine for the pet grammar.

    The clock is injectable so tests drive it with a fake clock.
    poll() must be called regularly (e.g. once per frame): a hold
    becomes a pet as soon as the threshold passes, without waiting
    for release.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._press: tuple[float, float, float] | None = None
        self._carrying = False
        self._pet_fired = False
        self._last_stream = 0.0

    def reset(self) -> None:
        self._press = None
        self._carrying = False
        self._pet_fired = False

    def press(self, x: float, y: float) -> None:
        self._press = (x, y, self._clock())
        self._carrying = False
        self._pet_fired = False

    def drag(self, x: float, y: float) -> list[GestureEvent]:
        if self._press is None:
            return []
        if self._pet_fired:
            # One press yields one gesture: once the hold became a pet,
            # a later drag can't re-interpret it as a carry.
            return []
        px, py, _ = self._press
        dist = ((x - px) ** 2 + (y - py) ** 2) ** 0.5
        if not self._carrying:
            if dist < DRAG_START_PX:
                return []
            self._carrying = True
            self._last_stream = self._clock()
            return [GestureEvent(CARRY_BEGIN, x, y)]
        now = self._clock()
        if now - self._last_stream < CARRY_CADENCE_S:
            return []
        self._last_stream = now
        return [GestureEvent(CARRY_MOVE, x, y)]

    def poll(self) -> list[GestureEvent]:
        """Fire a pet the moment the hold threshold passes."""
        if (
            self._press is None
            or self._carrying
            or self._pet_fired
        ):
            return []
        _, _, t0 = self._press
        if self._clock() - t0 >= HOLD_PET_THRESHOLD_S:
            self._pet_fired = True
            x, y, _ = self._press
            return [GestureEvent(PET, x, y)]
        return []

    def release(self, x: float, y: float) -> list[GestureEvent]:
        if self._press is None:
            return []
        _, _, t0 = self._press
        held = self._clock() - t0
        self._press = None
        if self._carrying:
            self._carrying = False
            return [GestureEvent(CARRY_END, x, y)]
        if self._pet_fired:
            # poll() already emitted the pet for this hold.
            self._pet_fired = False
            return []
        if held >= HOLD_PET_THRESHOLD_S:
            return [GestureEvent(PET, x, y)]
        return [GestureEvent(CHIRP, x, y)]


def gesture_intent(
    event: GestureEvent, soul_id: str, world_xy: tuple[float, float] | None = None
) -> tuple[str, dict]:
    """Map a gesture event to a signed intent (kind, payload).

    world_xy is the event position converted to world coordinates; it is
    required for carry events and ignored for chirp/pet.
    """
    if event.kind == CHIRP:
        return "chirp", {}
    if event.kind == PET:
        return "affection_pet", {}
    if event.kind in (CARRY_BEGIN, CARRY_MOVE, CARRY_END):
        if world_xy is None:
            raise ValueError("carry gesture needs world coordinates")
        phase = {
            CARRY_BEGIN: "grab",
            CARRY_MOVE: "move",
            CARRY_END: "release",
        }[event.kind]
        return "carry_move", {"x": world_xy[0], "y": world_xy[1], "phase": phase}
    raise ValueError(f"unknown gesture: {event.kind}")
