"""Transient speech-bubble renderer interface (issue #30).

The seam every other system calls to put words above a soul's orb:

    manager.show_bubble(soul_id, text, kind="speech", solicited=False)

``kind`` is a closed set: ``speech`` (ambient chatter), ``quip``
(tamer-requested personalized quip), ``system`` (wallet/Hub feedback),
``greeting`` (unlock/hello reflexes), ``mailbag`` (issue #32: a soul's
question for its tamer -- tapping it opens the mailbag answer surface),
``bridge`` (issue #36: pivotal external-agent events from the tamer's
tools).
Unknown kinds raise ValueError.

Bubbles are transient: each kind auto-dismisses after its documented
duration (speech 6s, quip 8s, system 5s, greeting 5s, mailbag 10s). At
most ``max_visible_per_soul`` (default 1) bubbles render above one orb
at a time; the rest queue (default depth 3, oldest queued dropped on
overflow) and promote as visible ones expire.

Every show goes through the ``NoisePolicy``: unsolicited bubbles are
capped per hour, suppressed in quiet hours / work mode, and muted
souls stay silent. Solicited bubbles bypass caps, quiet hours, and work
mode, but never a per-soul mute (see client/system/noise.py).

Server-driven bubbles arrive as viewport ``bubble`` ops; the app drains
them from the ViewportConsumer into this manager. Local systems
(#29's water-cooler reflexes, future mailbag taps) call
``show_bubble`` directly.

Taps: ``set_tap_handler`` registers a callback invoked by ``tap_at``
when a click lands on a visible bubble. The app wires it to open the
mailbag answer surface for mailbag-kind bubbles. Bubbles may carry an
opaque ``payload`` (e.g. the mailbag question_id) for the handler.

Rendering is pyglet-free here on purpose: ``layout()`` turns visible
bubbles into draw jobs ``(x, y, text)`` given soul screen positions,
and the scene renderer draws them with pyglet labels. Headless tests
exercise everything up to layout.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from ..system.bubble_config import DEFAULT_DURATIONS
from ..system.noise import (
    REASON_OK,
    NoisePolicy,
    NoiseSettings,
)

KIND_SPEECH = "speech"
KIND_QUIP = "quip"
KIND_SYSTEM = "system"
KIND_GREETING = "greeting"
KIND_MAILBAG = "mailbag"
#: Issue #36: pivotal external-agent events (test_failed, needs_review,
#: alert) surface as transient bubbles via the Hub's bridge seam.
KIND_BRIDGE = "bridge"
#: Issue #33: the overnight morning-note bubble. Solicited-adjacent
#: (the tamer's own unlock), so it bypasses caps/quiet hours/work
#: mode -- but per-soul mutes still block it (see system/noise.py).
KIND_MORNING_NOTE = "morning_note"
#: Issue #35: expedition departure/arrival bubbles. Unsolicited
#: cinematic moments, subject to the noise caps like mailbag questions.
KIND_EXPEDITION = "expedition"

BUBBLE_KINDS = frozenset(
    {
        KIND_SPEECH,
        KIND_QUIP,
        KIND_SYSTEM,
        KIND_GREETING,
        KIND_MAILBAG,
        KIND_MORNING_NOTE,
        KIND_EXPEDITION,
        KIND_BRIDGE,
    }
)

#: Pixels above the orb center where the bubble baseline sits.
BUBBLE_Y_OFFSET = 64.0

#: Hit-test box for taps: half-width / half-height in pixels around the
#: layout point (the bubble glyph is drawn centered-ish above the orb).
BUBBLE_TAP_HALF_W = 90.0
BUBBLE_TAP_HALF_H = 28.0


@dataclass
class Bubble:
    soul_id: str
    text: str
    kind: str
    solicited: bool
    created_at: float
    expires_at: float
    payload: dict | None = None


@dataclass
class BubbleResult:
    accepted: bool
    reason: str
    bubble: "Bubble | None" = None


class BubbleManager:
    """Lifecycle + noise gating for transient speech bubbles."""

    def __init__(
        self,
        policy: NoisePolicy | None = None,
        clock: Callable[[], float] | None = None,
        durations: dict[str, float] | None = None,
        max_visible_per_soul: int = 1,
        queue_depth: int = 3,
    ) -> None:
        self.policy = policy or NoisePolicy(NoiseSettings())
        self._clock = clock or time.monotonic
        self.durations = dict(DEFAULT_DURATIONS)
        if durations:
            for kind, secs in durations.items():
                if kind in BUBBLE_KINDS:
                    self.durations[kind] = float(secs)
        self.max_visible_per_soul = max(1, max_visible_per_soul)
        self.queue_depth = max(1, queue_depth)
        self._visible: dict[str, list[Bubble]] = {}
        self._queued: dict[str, deque[Bubble]] = {}
        self._tap_handler: Callable[[Bubble], None] | None = None

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    def set_tap_handler(self, handler: Callable[[Bubble], None] | None) -> None:
        """Register the tap callback (issue #32: tapping a mailbag
        bubble opens the answer surface)."""
        self._tap_handler = handler

    def tap_at(
        self, x: float, y: float, positions: dict[str, tuple[float, float]]
    ) -> Bubble | None:
        """Hit-test a click against visible bubbles.

        Returns the topmost visible bubble whose tap box contains the
        point (or None), and invokes the tap handler when one is set.
        The handler is how the app opens the mailbag answer surface.
        """
        self.tick()
        hit: Bubble | None = None
        for bubble in self.visible_bubbles():
            pos = positions.get(bubble.soul_id)
            if pos is None:
                continue
            cx, cy = pos[0], pos[1] + BUBBLE_Y_OFFSET
            if (
                abs(x - cx) <= BUBBLE_TAP_HALF_W
                and abs(y - cy) <= BUBBLE_TAP_HALF_H
            ):
                hit = bubble
                break
        if hit is not None and self._tap_handler is not None:
            self._tap_handler(hit)
        return hit

    def show_bubble(
        self,
        soul_id: str,
        text: str,
        kind: str = KIND_SPEECH,
        solicited: bool = False,
        now: float | None = None,
        payload: dict | None = None,
    ) -> BubbleResult:
        """Show a transient bubble above a soul's orb.

        Returns a BubbleResult: accepted=False with the noise reason
        (muted / quiet_hours / work_mode / cap) when suppressed.
        ``payload`` is opaque handler data (e.g. {"question_id": ...}
        for mailbag bubbles); it rides along for tap_at's handler.
        """
        if kind not in BUBBLE_KINDS:
            raise ValueError(f"unknown bubble kind: {kind!r}")
        text = str(text or "").strip()
        if not text:
            return BubbleResult(False, "empty", None)
        moment = self._now(now)
        reason = self.policy.decide(soul_id, solicited=solicited, now=moment)
        if reason != REASON_OK:
            return BubbleResult(False, reason, None)
        self.policy.note_shown(soul_id, solicited=solicited, now=moment)
        bubble = Bubble(
            soul_id=soul_id,
            text=text[:280],
            kind=kind,
            solicited=solicited,
            created_at=moment,
            expires_at=moment + self.durations[kind],
            payload=dict(payload) if payload else None,
        )
        visible = self._visible.setdefault(soul_id, [])
        if len(visible) < self.max_visible_per_soul:
            visible.append(bubble)
        else:
            queue = self._queued.setdefault(soul_id, deque())
            queue.append(bubble)
            while len(queue) > self.queue_depth:
                queue.popleft()
        return BubbleResult(True, REASON_OK, bubble)

    def tick(self, now: float | None = None) -> list[Bubble]:
        """Expire old bubbles, promote queued ones. Returns expired."""
        moment = self._now(now)
        expired: list[Bubble] = []
        for soul_id in list(self._visible):
            visible = self._visible[soul_id]
            still = [b for b in visible if b.expires_at > moment]
            expired.extend(b for b in visible if b.expires_at <= moment)
            queue = self._queued.get(soul_id)
            while len(still) < self.max_visible_per_soul and queue:
                still.append(queue.popleft())
            if still:
                self._visible[soul_id] = still
            else:
                self._visible.pop(soul_id, None)
                self._queued.pop(soul_id, None)
        return expired

    def visible_bubbles(self, soul_id: str | None = None) -> list[Bubble]:
        if soul_id is not None:
            return list(self._visible.get(soul_id, []))
        out: list[Bubble] = []
        for bubbles in self._visible.values():
            out.extend(bubbles)
        return out

    def queued_count(self, soul_id: str) -> int:
        return len(self._queued.get(soul_id, ()))

    def clear_soul(self, soul_id: str) -> None:
        self._visible.pop(soul_id, None)
        self._queued.pop(soul_id, None)

    def layout(
        self, positions: dict[str, tuple[float, float]]
    ) -> list[tuple[float, float, str]]:
        """Draw jobs for visible bubbles: (x, y, text) with y already
        offset above the orb. The scene renderer draws these."""
        self.tick()
        jobs: list[tuple[float, float, str]] = []
        for bubble in self.visible_bubbles():
            pos = positions.get(bubble.soul_id)
            if pos is None:
                continue
            jobs.append((pos[0], pos[1] + BUBBLE_Y_OFFSET, bubble.text))
        return jobs
