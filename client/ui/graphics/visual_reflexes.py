"""Client-side water-cooler reflexes (issue #29).

Small visual state machines, one per soul, driven by #28's presence
events and a coarse input-activity signal:

    unlock / tamer_return -> greeting ritual (brightness swell + bob)
    idle bucket "30+"     -> nap (slow dim pulse) until activity
    input-activity burst  -> reaction (pulse spike) + typing-dip opacity

Durations:
    greeting: 2.0 s (see GREETING_DURATION_S in soul_uniforms)
    nap: persistent while the idle bucket stays "30+"; any activity,
        unlock, or tamer_return clears it
    reaction: 1.2 s
    typing-dip: opacity dip decaying linearly over 0.6 s after a burst

Latency budget: the greeting engages within 0.5 s of the unlock event
(GREETING_LATENCY_BUDGET_S). Presence events arrive synchronously on
the controller's poll inside the client's update loop, so engagement
is immediate on receipt.

Privacy posture (explicit, per #28's hard contract): this controller
is a LOCAL-ONLY consumer of the narrow PresenceSampler interface. It
reads ONLY last_input_age_s() and is_locked() -- the interface exposes
no keystroke content, no keystroke counts, no window titles, no URLs,
so none of those can be collected here. The burst detector counts
coarse input-activity EDGES (the age counter resetting), never
keystrokes; it cannot tell typing from mouse movement. Nothing this
module computes is transmitted, logged, or persisted: there is no
network send, no log call carrying the signal, and no disk write. The
#28 presence uplink is a separate pipeline with its own trust
boundary; this module never touches it.
"""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ...system.presence import (
    BUCKET_30_PLUS,
    EVENT_TAMER_RETURN,
    EVENT_UNLOCK,
    PresenceRedactor,
    PresenceSampler,
    build_sampler,
)
from .soul_uniforms import (
    GREETING_DURATION_S,
    REACTION_DURATION_S,
    REFLEX_GREETING,
    REFLEX_NAP,
    REFLEX_REACTION,
)

GREETING_LATENCY_BUDGET_S = 0.5
BURST_WINDOW_S = 2.0
BURST_EDGE_THRESHOLD = 3
BURST_DEBOUNCE_S = 1.0
TYPING_DIP_DECAY_S = 0.6
_INPUT_EDGE_DROP_S = 0.05


@dataclass
class VisualOverlay:
    """Per-soul visual overlay for one frame."""

    reflex: Optional[str]
    reflex_t: float
    typing_dip: float


class TypingBurstDetector:
    """Detects coarse input-activity bursts from the sampler, local-only.

    An "edge" is the last-input age counter dropping between polls,
    meaning some input happened. Edges are counted in a sliding
    window; BURST_EDGE_THRESHOLD edges inside BURST_WINDOW_S seconds
    declares a burst. No keystroke content or counts are involved --
    the sampler interface cannot provide them.
    """

    def __init__(
        self,
        sampler: PresenceSampler,
        clock: Callable[[], float] = time.monotonic,
        window_s: float = BURST_WINDOW_S,
        threshold: int = BURST_EDGE_THRESHOLD,
    ) -> None:
        self._sampler = sampler
        self._clock = clock
        self._window_s = window_s
        self._threshold = threshold
        self._prev_age: Optional[float] = None
        self._edges: collections.deque[float] = collections.deque()
        self._last_burst_at: Optional[float] = None

    def poll(self, now: Optional[float] = None) -> bool:
        """Poll once; True when a burst is declared on this poll."""
        now = self._clock() if now is None else now
        age = self._sampler.last_input_age_s()
        if self._prev_age is not None and age < self._prev_age - _INPUT_EDGE_DROP_S:
            self._edges.append(now)
        self._prev_age = age
        while self._edges and now - self._edges[0] > self._window_s:
            self._edges.popleft()
        if len(self._edges) >= self._threshold and (
            self._last_burst_at is None or now - self._last_burst_at >= BURST_DEBOUNCE_S
        ):
            self._edges.clear()
            self._last_burst_at = now
            return True
        return False

    def dip(self, now: Optional[float] = None) -> float:
        """Typing-dip amount 0..1, decaying over TYPING_DIP_DECAY_S."""
        now = self._clock() if now is None else now
        if self._last_burst_at is None:
            return 0.0
        elapsed = now - self._last_burst_at
        if elapsed >= TYPING_DIP_DECAY_S:
            return 0.0
        return max(0.0, 1.0 - elapsed / TYPING_DIP_DECAY_S)


class VisualReflexController:
    """Owns the per-soul reflex state machines for the water-cooler hooks.

    Polls its own PresenceRedactor (local only, never shipped) plus the
    typing-burst detector each frame() call. Keyed by soul id; the
    client applies the returned overlays to its Soul objects.
    """

    def __init__(
        self,
        sampler: Optional[PresenceSampler] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        sampler = sampler if sampler is not None else build_sampler()
        self._redactor = PresenceRedactor(sampler, clock=clock)
        self._bursts = TypingBurstDetector(sampler, clock=clock)
        self._reflexes: dict[str, tuple[str, float]] = {}

    def _set_reflex(self, soul_ids: list[str], kind: str, now: float) -> None:
        for sid in soul_ids:
            self._reflexes[sid] = (kind, now)

    def frame(
        self, soul_ids: list[str], now: Optional[float] = None
    ) -> dict[str, VisualOverlay]:
        """Advance all reflex state machines; return per-soul overlays."""
        now = self._clock() if now is None else now
        payload = self._redactor.sample()
        event = payload.get("event")
        bucket = payload.get("idle_bucket")

        if event in (EVENT_UNLOCK, EVENT_TAMER_RETURN):
            self._set_reflex(soul_ids, REFLEX_GREETING, now)
        if bucket == BUCKET_30_PLUS:
            self._set_reflex(soul_ids, REFLEX_NAP, now)

        if self._bursts.poll(now):
            self._set_reflex(soul_ids, REFLEX_REACTION, now)

        overlays: dict[str, VisualOverlay] = {}
        dip = self._bursts.dip(now)
        for sid in soul_ids:
            entry = self._reflexes.get(sid)
            reflex: Optional[str] = None
            reflex_t = 0.0
            if entry is not None:
                kind, started = entry
                elapsed = now - started
                duration = (
                    GREETING_DURATION_S
                    if kind == REFLEX_GREETING
                    else REACTION_DURATION_S
                    if kind == REFLEX_REACTION
                    else float("inf")
                )
                if elapsed < duration:
                    if kind == REFLEX_NAP and bucket != BUCKET_30_PLUS:
                        del self._reflexes[sid]
                    else:
                        reflex, reflex_t = kind, elapsed
                else:
                    del self._reflexes[sid]
            overlays[sid] = VisualOverlay(
                reflex=reflex, reflex_t=reflex_t, typing_dip=dip
            )
        return overlays
