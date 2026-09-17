"""Noise policy for speech bubbles (issue #30).

Every bubble the client wants to show passes through here first. The
decisions are pure functions of (config, clock, history) so they are
simulated-clock testable; the stateful ``NoisePolicy`` class just holds
the per-soul show history.

Rules, in evaluation order:

1. **Mute**: a muted soul shows nothing -- unsolicited AND solicited.
   Muting means "leave this soul alone"; the tamer unmutes in settings.
2. **Work mode**: suppresses unsolicited bubbles. Solicited ones
   (the tamer asked) still show.
3. **Quiet hours**: the configured local-time window (default
   22:00-07:00) suppresses unsolicited bubbles. Solicited still show.
4. **Cap**: at most ``caps_per_hour`` unsolicited bubbles per soul per
   rolling hour (default 4, the arch doc's hard budget). Solicited
   bubbles never consume the budget.

Solicited means the tamer asked for it: a quip request, a mailbag tap,
or something opened from settings. The documented contract: solicited
bypasses caps, quiet hours, and work mode, but NOT per-soul mutes.
"""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field
from typing import Callable

REASON_OK = "ok"
REASON_MUTED = "muted"
REASON_WORK_MODE = "work_mode"
REASON_QUIET_HOURS = "quiet_hours"
REASON_CAP = "cap"


@dataclass
class NoiseSettings:
    caps_per_hour: int = 4
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    mutes: frozenset[str] = field(default_factory=frozenset)
    work_mode: bool = False


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        hour_s, min_s = value.strip().split(":")
        hour, minute = int(hour_s), int(min_s)
    except (ValueError, AttributeError):
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return (hour, minute)
    return None


def in_quiet_hours(
    now: float,
    quiet_start: str,
    quiet_end: str,
    localtime: Callable[[float], time.struct_time] = time.localtime,
) -> bool:
    """Whether ``now`` falls inside the quiet window (local time).

    The window may cross midnight (22:00-07:00). Unparseable bounds
    fail soft to "not quiet".
    """
    start = _parse_hhmm(quiet_start)
    end = _parse_hhmm(quiet_end)
    if start is None or end is None:
        return False
    if start == end:
        return False
    lt = localtime(now)
    cur = (lt.tm_hour, lt.tm_min)
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def decide(
    settings: NoiseSettings,
    soul_id: str,
    solicited: bool,
    history: list[float],
    now: float,
    localtime: Callable[[float], time.struct_time] = time.localtime,
    wall: float | None = None,
) -> str:
    """Pure noise decision. Returns a REASON_* constant.

    ``now`` is the lifecycle clock used for the rolling hourly cap.
    Quiet hours are evaluated against ``wall`` (the wall clock), which
    defaults to ``now`` for the pure function; the stateful policy
    passes its own wall clock so a monotonic lifecycle clock never
    leaks into local-time checks.
    """
    if soul_id in settings.mutes:
        return REASON_MUTED
    if not solicited:
        if settings.work_mode:
            return REASON_WORK_MODE
        if in_quiet_hours(
            wall if wall is not None else now,
            settings.quiet_start,
            settings.quiet_end,
            localtime,
        ):
            return REASON_QUIET_HOURS
        cap = max(0, settings.caps_per_hour)
        recent = [t for t in history if now - t < 3600.0]
        if len(recent) >= cap:
            return REASON_CAP
    return REASON_OK


class NoisePolicy:
    """Stateful noise policy: holds per-soul unsolicited show history."""

    def __init__(
        self,
        settings: NoiseSettings | None = None,
        clock: Callable[[], float] | None = None,
        localtime: Callable[[float], time.struct_time] | None = None,
        wall: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings or NoiseSettings()
        self._clock = clock or time.time
        self._localtime = localtime or time.localtime
        # Wall clock for quiet-hours local-time checks. Deliberately
        # separate from ``clock`` (the lifecycle clock, often
        # monotonic) so monotonic timestamps never leak into the
        # local-time quiet window evaluation.
        self._wall = wall or time.time
        self._history: dict[str, collections.deque[float]] = {}

    def update_settings(self, settings: NoiseSettings) -> None:
        self.settings = settings

    def set_muted(self, soul_id: str, muted: bool) -> None:
        mutes = set(self.settings.mutes)
        if muted:
            mutes.add(soul_id)
        else:
            mutes.discard(soul_id)
        self.settings.mutes = frozenset(mutes)

    def decide(
        self, soul_id: str, solicited: bool = False, now: float | None = None
    ) -> str:
        moment = self._clock() if now is None else now
        history = list(self._history.get(soul_id, ()))
        return decide(
            self.settings,
            soul_id,
            solicited,
            history,
            moment,
            self._localtime,
            wall=self._wall(),
        )

    def note_shown(
        self, soul_id: str, solicited: bool = False, now: float | None = None
    ) -> None:
        """Record a shown bubble. Only unsolicited shows consume the
        hourly budget; solicited ones are recorded nowhere."""
        if solicited:
            return
        moment = self._clock() if now is None else now
        dq = self._history.setdefault(soul_id, collections.deque())
        dq.append(moment)
        while dq and moment - dq[0] >= 3600.0:
            dq.popleft()

    def prune(self, now: float | None = None) -> None:
        moment = self._clock() if now is None else now
        for dq in self._history.values():
            while dq and moment - dq[0] >= 3600.0:
                dq.popleft()
