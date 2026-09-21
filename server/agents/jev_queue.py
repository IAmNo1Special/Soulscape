import threading
from dataclasses import dataclass, field

from . import jev


@dataclass
class JevJob:
    soul_id: str
    kinds: list[str] = field(default_factory=list)
    lane: int = jev.LANE_NORMAL
    deadline: float = 0.0
    enqueued_at: float = 0.0
    snapshot: dict = field(default_factory=dict)
    snapshot_at: float = 0.0
    seq: int = 0


class JevEventQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._jobs: dict[str, JevJob] = {}
        self._last_event_at: dict[str, float] = {}
        self._last_think: dict[str, float] = {}
        self._seq = 0

    def _lane(self, kind: str) -> int:
        return jev.KIND_LANE.get(kind, jev.LANE_NORMAL)

    def note(
        self,
        soul_id: str,
        kind: str,
        now: float,
        snapshot: dict,
        snapshot_at: float | None = None,
        floor_override: float | None = None,
    ) -> bool:
        with self._lock:
            self._last_event_at[soul_id] = now
            job = self._jobs.get(soul_id)
            if job is None:
                job = JevJob(soul_id=soul_id)
                self._jobs[soul_id] = job
                is_new = True
            else:
                is_new = False
            if kind not in job.kinds:
                job.kinds.append(kind)
            job.lane = min([self._lane(k) for k in job.kinds])
            job.snapshot = snapshot
            job.snapshot_at = now if snapshot_at is None else snapshot_at
            job.enqueued_at = now
            floor = floor_override
            if floor is None:
                floor = (
                    jev.FLOOR_TAMER_S
                    if job.lane == jev.LANE_TAMER
                    else jev.FLOOR_DEFAULT_S
                )
            old_deadline = job.deadline
            last_think = self._last_think.get(soul_id)
            if floor_override is not None:
                job.deadline = now + floor_override
            else:
                job.deadline = (
                    now if last_think is None else max(now, last_think + floor)
                )
            self._seq += 1
            job.seq = self._seq
            self._cond.notify_all()
            return is_new or job.deadline < old_deadline

    def pump(self, now: float) -> JevJob | None:
        with self._lock:
            best: JevJob | None = None
            for job in self._jobs.values():
                if job.deadline > now:
                    continue
                if best is None or (job.lane, job.seq) < (best.lane, best.seq):
                    best = job
            if best is None:
                return None
            del self._jobs[best.soul_id]
            self._last_think[best.soul_id] = now
            return best

    def requeue(self, job: JevJob, now: float, delay: float = 0.5) -> None:
        with self._lock:
            current = self._jobs.get(job.soul_id)
            if current is None:
                job.deadline = now + delay
                self._seq += 1
                job.seq = self._seq
                self._jobs[job.soul_id] = job
            else:
                for kind in job.kinds:
                    if kind not in current.kinds:
                        current.kinds.append(kind)
                current.lane = min(current.lane, job.lane)
            self._cond.notify_all()

    def earliest_deadline(self) -> float | None:
        with self._lock:
            if not self._jobs:
                return None
            return min(job.deadline for job in self._jobs.values())

    def wait_for_work(self, timeout: float | None) -> None:
        with self._cond:
            self._cond.wait(timeout=timeout)

    def wakeup(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def has_pending(self, soul_id: str) -> bool:
        with self._lock:
            return soul_id in self._jobs

    def last_event_at(self, soul_id: str) -> float | None:
        with self._lock:
            return self._last_event_at.get(soul_id)

    def forget(self, soul_id: str) -> None:
        with self._lock:
            self._jobs.pop(soul_id, None)
            self._last_event_at.pop(soul_id, None)
            self._last_think.pop(soul_id, None)
