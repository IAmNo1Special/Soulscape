import logging
import math
import threading
import time

from . import brain_busy, jev, jev_brain
from .jev_telemetry import JevUsageRow

logger = logging.getLogger("soulscape_hub")

_REQUIRED_ANSWERS = ("threatened", "needs_pressing")


class JevWorker:
    def __init__(
        self,
        *,
        queue,
        breaker,
        sensors=None,
        client=None,
        emit_fn,
        telemetry_fn,
        suppressed_fn=None,
        cooldowns=None,
        wander=None,
        now_fn=time.time,
    ) -> None:
        self._queue = queue
        self._breaker = breaker
        self._injected_client = client
        self._sensors = sensors
        self._emit_fn = emit_fn
        self._telemetry_fn = telemetry_fn
        self._suppressed_fn = suppressed_fn or (lambda sid, snap: False)
        self._cooldowns = cooldowns or jev_brain.Cooldowns()
        self._wander = wander or jev_brain.WanderManager()
        self._wander_intent: dict[str, str] = {}
        self._now = now_fn
        self._inflight: set[str] = set()
        self._pending_record = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        thread = threading.Thread(target=self._run, name="jev-worker", daemon=True)
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            self._queue.wakeup()
            thread.join(timeout=10.0)
            if thread.is_alive():
                logger.warning("jev worker thread still alive after 10s join")
            else:
                self._thread = None

    def _run(self) -> None:
        client_ctx = None
        try:
            while not self._stop.is_set():
                if self._sensors is None:
                    self._sensors, client_ctx = self._make_sensors(client_ctx)
                if self._sensors is None:
                    self._queue.wait_for_work(1.0)
                    continue
                try:
                    now = self._now()
                    job = self._queue.pump(now)
                    if job is not None:
                        self._process_job(job, now)
                except Exception:
                    logger.exception("jev worker loop failed")
                try:
                    deadline = self._queue.earliest_deadline()
                    now = self._now()
                    timeout = max(0.0, deadline - now) if deadline is not None else 1.0
                    self._queue.wait_for_work(min(timeout, 1.0))
                except Exception:
                    logger.exception("jev worker wait failed")
        finally:
            if client_ctx is not None:
                try:
                    client_ctx.__exit__(None, None, None)
                except Exception:
                    logger.exception("jev worker client close failed")

    def _make_sensors(self, client_ctx):
        if self._sensors is not None:
            return self._sensors, client_ctx
        if client_ctx is not None:
            try:
                client_ctx.__exit__(None, None, None)
            except Exception:
                logger.exception("jev worker client close failed")
        entered_ctx = None
        try:
            from goapauto import shared_client

            client = self._injected_client
            if client is None:
                ctx = shared_client(timeout=jev.JUDGE_TIMEOUT_S)
                client = ctx.__enter__()
                entered_ctx = ctx
            return jev.SensorRegistry(client), entered_ctx
        except Exception:
            logger.exception("jev worker sensor init failed; will retry")
            if entered_ctx is not None:
                try:
                    entered_ctx.__exit__(None, None, None)
                except Exception:
                    logger.exception("jev worker client close failed")
            return None, None

    def on_wander_leg_complete(self, soul_id: str, now: float, snapshot: dict) -> None:
        dwell = self._wander.dwell_for(soul_id)
        self._wander_intent.pop(soul_id, None)
        self._queue.note(
            soul_id, "wander_continue", now, snapshot, floor_override=dwell
        )

    def wander_intent_id(self, soul_id: str) -> str | None:
        return self._wander_intent.get(soul_id)

    def note(
        self,
        soul_id: str,
        kind: str,
        now: float,
        snapshot: dict,
        snapshot_at: float | None = None,
        floor_override: float | None = None,
    ) -> None:
        self._queue.note(
            soul_id,
            kind,
            now,
            snapshot,
            snapshot_at=snapshot_at,
            floor_override=floor_override,
        )

    def prune_sensors(self, keep: set[str]) -> None:
        prune = getattr(self._sensors, "prune", None)
        if prune is not None:
            prune(keep)

    def forget(self, soul_id: str) -> None:
        self._queue.forget(soul_id)
        self._cooldowns.forget(soul_id)
        self._wander.cancel(soul_id)
        self._inflight.discard(soul_id)
        brain_busy.release(soul_id)

    def _process_job(self, job, now: float) -> None:
        soul_id = job.soul_id
        if now - job.enqueued_at > jev.SHED_AFTER_S:
            self._telemetry(job, soul_id, None, error_type="shed", shed_count=1)
            return
        if now - job.snapshot_at > jev.SHED_AFTER_S:
            self._telemetry(
                job, soul_id, None, error_type="stale_snapshot", shed_count=1
            )
            return
        if not self._breaker.admit():
            self._telemetry(job, soul_id, None, error_type="breaker_open")
            return
        if soul_id in self._inflight or not brain_busy.acquire(soul_id):
            self._queue.requeue(job, now)
            return
        self._inflight.add(soul_id)
        try:
            self._think(soul_id, job, now)
        except Exception as exc:
            self._telemetry(job, soul_id, None, error_type=type(exc).__name__)
        finally:
            self._inflight.discard(soul_id)
            brain_busy.release(soul_id)

    def _think(self, soul_id: str, job, now: float) -> None:
        snapshot = job.snapshot
        if jev_brain.survival_tripped(snapshot):
            self._wander.cancel(soul_id)
            self._telemetry(job, soul_id, snapshot, error_type="survival")
            return
        judgments, record = self._judge(soul_id, snapshot)
        if record is not None and record.error and not record.stale_cache_hit:
            self._breaker.on_failure()
            self._telemetry(
                job, soul_id, snapshot, record=record, error_type=record.error
            )
            return
        if record is not None and record.error:
            self._breaker.on_failure()
        else:
            self._breaker.on_success()
        if any(judgments.get(q) is None for q in _REQUIRED_ANSWERS):
            self._telemetry(
                job,
                soul_id,
                snapshot,
                record=record,
                error_type="judgment_unavailable",
            )
            return
        goal = jev_brain.arbitrate(
            soul_id, judgments, snapshot, self._cooldowns, now, job.kinds
        )
        active = jev_brain.active_goals(judgments, snapshot, job.kinds)
        if goal is None:
            self._telemetry(
                job,
                soul_id,
                snapshot,
                record=record,
                judgments=judgments,
                active_goals=active,
                error_type="no_goal",
            )
            return
        if goal != jev_brain.GOAL_EXPLORE:
            self._wander.cancel(soul_id)
        plan = jev_brain.plan_for_goal(goal, snapshot)
        if not plan:
            self._telemetry(
                job,
                soul_id,
                snapshot,
                record=record,
                judgments=judgments,
                active_goals=active,
                goal_selected=goal,
                error_type="no_plan",
            )
            return
        action = plan[0]
        if goal == jev_brain.GOAL_REST:
            self._emit_fn(soul_id, goal, action, {})
            self._cooldowns.mark(soul_id, goal, now)
            self._telemetry(
                job,
                soul_id,
                snapshot,
                record=record,
                judgments=judgments,
                active_goals=active,
                goal_selected=goal,
                plan_action_count=1,
            )
            return
        if goal == jev_brain.GOAL_EXPLORE:
            self._emit_explore(soul_id, job, snapshot, record, judgments, active, now)
            return
        payload = jev_brain.payload_for(goal, action, snapshot)
        self._emit_fn(soul_id, goal, action, payload)
        if goal in jev_brain.GOAL_COOLDOWNS:
            self._cooldowns.mark(soul_id, goal, now)
        self._telemetry(
            job,
            soul_id,
            snapshot,
            record=record,
            judgments=judgments,
            active_goals=active,
            goal_selected=goal,
            plan_action_count=len(plan),
        )

    def _emit_explore(
        self, soul_id, job, snapshot, record, judgments, active, now
    ) -> None:
        if self._suppressed_fn(soul_id, snapshot):
            self._wander.cancel(soul_id)
            self._telemetry(
                job,
                soul_id,
                snapshot,
                record=record,
                judgments=judgments,
                active_goals=active,
                goal_selected=jev_brain.GOAL_EXPLORE,
                error_type="wander_suppressed",
            )
            return
        leg = self._wander.next_leg(soul_id)
        pos = snapshot.get("position") or [0.0, 0.0]
        if leg is not None and (
            math.hypot(leg[0] - pos[0], leg[1] - pos[1]) > jev.WANDER_LEG_MAX_U
        ):
            self._wander.cancel(soul_id)
            leg = None
        if leg is None:
            if "wander_continue" in job.kinds and "restlessness" not in job.kinds:
                self._wander.cancel(soul_id)
                self._telemetry(
                    job,
                    soul_id,
                    snapshot,
                    record=record,
                    judgments=judgments,
                    active_goals=active,
                    goal_selected=jev_brain.GOAL_EXPLORE,
                    error_type="episode_complete",
                )
                return
            self._wander.start(soul_id, float(pos[0]), float(pos[1]))
            leg = self._wander.next_leg(soul_id)
        payload = {
            "x": leg[0],
            "y": leg[1],
            "pace": "amble",
            "wander": True,
        }
        intent_id = self._emit_fn(soul_id, jev_brain.GOAL_EXPLORE, "move_to", payload)
        if isinstance(intent_id, str) and intent_id:
            self._wander_intent[soul_id] = intent_id
        else:
            self._wander_intent.pop(soul_id, None)
        self._telemetry(
            job,
            soul_id,
            snapshot,
            record=record,
            judgments=judgments,
            active_goals=active,
            goal_selected=jev_brain.GOAL_EXPLORE,
            plan_action_count=1,
        )

    def _judge(self, soul_id: str, snapshot: dict):
        self._sensors.bind_telemetry(self._capture_record)
        sensor = self._sensors.get(soul_id)
        self._pending_record = None
        judgments = sensor.judge(snapshot)
        return judgments, self._pending_record

    def _capture_record(self, record) -> None:
        self._pending_record = record

    def _telemetry(
        self,
        job,
        soul_id: str,
        snapshot: dict | None,
        *,
        record=None,
        judgments: dict | None = None,
        active_goals: list | None = None,
        goal_selected: str | None = None,
        plan_action_count: int = 0,
        error_type: str | None = None,
        shed_count: int = 0,
    ) -> None:
        now = self._now()
        self._telemetry_fn(
            JevUsageRow(
                soul_id=soul_id,
                at=now,
                trigger_event=job.kinds[0] if job.kinds else "",
                lane=job.lane,
                latency_ms=getattr(record, "latency_ms", None),
                queue_wait_ms=(now - job.enqueued_at) * 1000.0,
                input_tokens=getattr(record, "input_tokens", None),
                questions_asked=len(jev.QUESTIONS),
                snapshot=snapshot or {},
                raw_judgments=judgments or {},
                error_type=error_type,
                stale_cache_hit=bool(getattr(record, "stale_cache_hit", False)),
                shed_count=shed_count,
                coalesced_kinds=list(job.kinds),
                active_goals=active_goals or [],
                goal_selected=goal_selected,
                plan_action_count=plan_action_count,
            )
        )
