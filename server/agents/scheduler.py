"""Per-soul think scheduler (issue #24).

Each soul carries `next_think`: the earliest time it should be
considered for a reflex think. Semantics:

- Jittered interval: after a think, next_think = now + U(0.8, 1.2) *
  THINK_BASE_INTERVAL (600 s -> 480..720 s), per the arch.
- Minimum gap: THINK_MIN_GAP (60 s) between two thinks of the same
  soul. The jittered interval already satisfies it; the gap binds
  pull-forwards: an event can never schedule a think sooner than
  last_think + THINK_MIN_GAP.
- Event pull-forward: coarse-vision enter events, wallet deltas and
  tamer orders pull next_think earlier (never later). Wallet deltas
  arrive via dormancy.reset_think_schedule on wake; vision enters are
  wired in world_tick; tamer orders have no ingress in v1, so
  note_order() is an unwired hook.
- Per-tick budget: due() returns at most THINK_MAX_PER_TICK souls,
  oldest next_think first. Overflow souls stay past-due and are first
  in line next tick.

Persistence: in-memory, rebuilt on boot. Boot rebuild staggers
initial thinks over U(0, THINK_BASE_INTERVAL) so 500 souls do not
thunder-herd the first tick. Dormant/collapsed souls are skipped by
the caller (pool double-checks live state before thinking anyway);
reset() gives a woken soul a prompt fresh schedule instead of a stale
cadence.
"""

import random
import time

#: Base think interval (seconds). Post-think: next = now + U(0.8,1.2)*base.
THINK_BASE_INTERVAL = 600.0

#: Jitter half-width around the base interval (fraction).
THINK_JITTER = 0.2

#: Minimum gap between two thinks of the same soul (seconds).
THINK_MIN_GAP = 60.0

#: Max souls evaluated per tick; overflow rolls to the next tick.
THINK_MAX_PER_TICK = 4

#: How far forward an event pulls a think: now + PULL_FORWARD_DELAY,
#: clamped by the minimum gap.
PULL_FORWARD_DELAY = 5.0


class ThinkScheduler:
    def __init__(self, seed: int | None = None) -> None:
        self._next: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._rng = random.Random(seed)

    def schedule_next(self, soul_id: str, now: float) -> float:
        """Set the post-think schedule: jittered interval from now."""
        lo = THINK_BASE_INTERVAL * (1.0 - THINK_JITTER)
        hi = THINK_BASE_INTERVAL * (1.0 + THINK_JITTER)
        nxt = now + self._rng.uniform(lo, hi)
        self._next[soul_id] = nxt
        self._last[soul_id] = now
        return nxt

    def next_think(self, soul_id: str) -> float | None:
        return self._next.get(soul_id)

    def reset(self, soul_id: str, now: float | None = None) -> float:
        """Fresh prompt schedule (dormancy wake): think almost now.

        Honors the minimum gap so a soul that thought seconds ago is
        not double-thought by a wake event.
        """
        now = time.time() if now is None else now
        last = self._last.get(soul_id)
        earliest = now + 1.0
        if last is not None:
            earliest = max(earliest, last + THINK_MIN_GAP)
        self._next[soul_id] = earliest
        return earliest

    def pull_forward(self, soul_id: str, now: float) -> float | None:
        """Pull a soul's next think earlier in response to an event.

        Never pushes it later; never violates the minimum gap. Souls
        with no schedule are unaffected (boot rebuild covers them).
        """
        current = self._next.get(soul_id)
        if current is None:
            return None
        target = now + PULL_FORWARD_DELAY
        last = self._last.get(soul_id)
        if last is not None:
            target = max(target, last + THINK_MIN_GAP)
        if target < current:
            self._next[soul_id] = target
            return target
        return current

    def note_vision_enter(self, soul_id: str, now: float) -> float | None:
        """Coarse-vision enter event (#20) pulls the think forward."""
        return self.pull_forward(soul_id, now)

    def note_wallet_delta(self, soul_id: str, now: float) -> float | None:
        """Wallet delta pulls the think forward (hook; wake uses reset)."""
        return self.pull_forward(soul_id, now)

    def note_order(self, soul_id: str, now: float) -> float | None:
        """Tamer-order pull-forward. Unwired in v1: no order ingress yet."""
        return self.pull_forward(soul_id, now)

    def due(
        self,
        now: float,
        candidates: list[str],
        max_per_tick: int = THINK_MAX_PER_TICK,
    ) -> list[str]:
        """Up to max_per_tick due souls, oldest next_think first.

        Candidates are soul ids the caller already filtered (awake,
        alive). Souls with no schedule count as due immediately.
        Overflow stays past-due and wins next tick.
        """
        due = [
            (self._next.get(sid, 0.0), sid)
            for sid in candidates
            if self._next.get(sid, 0.0) <= now
        ]
        due.sort()
        return [sid for _, sid in due[:max_per_tick]]

    def earliest_due(self) -> float | None:
        """Earliest next_think across scheduled souls, or None if empty.

        Lets the tick loop skip its candidate query entirely when no
        soul can possibly be due yet.
        """
        return min(self._next.values()) if self._next else None

    def prune(self, keep_ids: set[str]) -> None:
        """Drop schedules for souls no longer in the world."""
        for sid in [sid for sid in self._next if sid not in keep_ids]:
            self.forget(sid)

    def rebuild_on_boot(self, soul_ids: list[str], now: float) -> None:
        """Fresh staggered schedules for every soul at boot."""
        for sid in soul_ids:
            self._next[sid] = now + self._rng.uniform(0.0, THINK_BASE_INTERVAL)

    def needs_boot(self) -> bool:
        """True when no schedules exist yet (first tick after boot)."""
        return not self._next

    def forget(self, soul_id: str) -> None:
        self._next.pop(soul_id, None)
        self._last.pop(soul_id, None)


_default: ThinkScheduler | None = None


def default() -> ThinkScheduler:
    """Process-global scheduler: the one the tick loop and the
    dormancy wake hook share."""
    global _default
    if _default is None:
        _default = ThinkScheduler()
    return _default
