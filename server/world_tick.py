"""Hub world tick (issue #7, process-split in issue #37).

Fixed-timestep 20 Hz simulation loop that owns Soul positions and
movement physics. Owned and run by the sim process
(`server/sim_process.py`); the API never ticks. The legacy
`HUB_AUTHORITATIVE` flag that once gated the API-embedded tick is
retired -- the sim always ticks when it runs.

The hot path touches only SQLite. No network calls, no LLM calls.

Intent adjudication (issue #14): each tick pumps the durable intent queue
before integrating motion. A move_to intent re-validates custody against
live state, then clamps the target to the reachable radius
`INTENT_MOVE_SPEED * INTENT_HORIZON_SECONDS` and steers the Soul toward it
at INTENT_MOVE_SPEED. The clamp choice: the tick runs at 20 Hz and steering
needs several ticks to converge, so the horizon is deliberately generous —
any target a Tamer could plausibly click (within 3000 px) is accepted
verbatim; only far-away forged targets are pulled back along the original
direction, so a clamped move still heads the way the client asked. Arrival
stops within INTENT_ARRIVAL_EPS of the target.

Durable persistence (issue #16) lives in server/persistence.py: the tick
integrates positions into an in-memory dirty set and flushes it to SQLite
every 5 s or 1000 dirty entries (RPO <= 5 s + WAL fsync). Adjudicated
intent outcomes are journaled in the same commit as the intent status
update (RPO = 0). See persistence.py for the full RPO statement.

Spatial index and vision rings (issue #20) live in server/world.py: the
tick rebuilds the world's SpatialHashGrid from the read-through position
view every tick and diffs coarse-vision enter/exit events every 5th tick.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import threading
import time

from . import database
from . import determinism
from . import intents
from . import mailbag
from . import market
from . import persistence
from . import plots
from . import presence as presence_module
from . import recap
from . import social
from . import world
from . import biology
from . import dormancy
from . import affection
from . import bridge
from . import agents
from . import expeditions
from . import resources
from . import wander as wander_mod
from .agents import metering
from .agents import reflex
from .agents import jev as jev_mod
from .agents import jev_brain as jev_brain_mod
from .agents import jev_queue as jev_queue_mod
from .agents import jev_telemetry as jev_telemetry_mod
from .agents import jev_worker as jev_worker_mod

logger = logging.getLogger("soulscape_hub")

TICK_HZ = 20
TICK_DT = 1.0 / TICK_HZ
WORLD_EPOCH = 0.0
_POSITION_MARGIN = persistence.POSITION_MARGIN

INTENT_MOVE_SPEED = 600.0
INTENT_HORIZON_SECONDS = 5.0
INTENT_ARRIVAL_EPS = persistence.ARRIVAL_EPS


def hub_authoritative_enabled() -> bool:
    return os.getenv("HUB_AUTHORITATIVE", "").lower() in ("1", "true", "yes")


def _parse_pair(raw) -> tuple[float, float]:
    if raw is None:
        return (0.0, 0.0)
    if isinstance(raw, str):
        raw = json.loads(raw)
    x, y = float(raw[0]), float(raw[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("non-finite pair")
    return (x, y)


class WorldTick:
    def __init__(self, tick_dt: float = TICK_DT, scenario_seed: str | None = None):
        self.tick_dt = tick_dt
        # Issue #38: one seed drives every adjudication RNG (see
        # determinism.py for the derivation). None = legacy unseeded
        # behavior; the replay CLI sets it explicitly.
        self.scenario_seed = determinism.current_seed(scenario_seed)
        # Issue #38: set to a determinism.ReplayContext by the replay
        # CLI; None in live mode. Adjudication consults it for the
        # frozen clock and record-and-replay ids.
        self.replay: determinism.ReplayContext | None = None
        self.tick_id = 0
        self.enabled = False
        self.running = False
        self.last_duration_ms = 0.0
        self.avg_duration_ms = 0.0
        self.max_duration_ms = 0.0
        self.souls_moved_last_tick = 0
        self.skipped_ticks = 0
        self._task: asyncio.Task | None = None
        self._step_lock = threading.RLock()
        self._last_flush_at = time.monotonic()
        self._last_snapshot_tick = 0
        self.vision = world.WorldVision()
        self.agent_pool = agents.pool.AgentPool()
        self._agent_soul_count: int | None = None
        self._jev_worker: jev_worker_mod.JevWorker | None = None
        self._jev_queue: jev_queue_mod.JevEventQueue | None = None
        self._jev_boot_at = 0.0
        self._jev_last_restless_sweep = 0.0

    async def start(self) -> None:
        if self.running:
            return
        self.enabled = True
        self.running = True
        # Issue #38: journal the run header (scenario seed + RNG
        # derivation contract) once at sim boot; idempotent.
        with database.get_db() as conn:
            persistence.append_run_start(
                conn, self.tick_id, self.scenario_seed, self.tick_dt
            )
            conn.commit()
        self._jev_init()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await asyncio.to_thread(self._finalize)
        await asyncio.to_thread(self._jev_shutdown)
        self.enabled = False

    def _finalize(self) -> None:
        with self._step_lock:
            try:
                self.flush()
                with database.get_db() as conn:
                    persistence.take_snapshot(
                        conn, self.tick_id, scenario_seed=self.scenario_seed
                    )
                    persistence.prune_snapshots(conn)
                self._last_snapshot_tick = self.tick_id
            except Exception:
                logger.exception("final flush/snapshot on stop failed")

    def flush(self) -> int:
        with database.get_db() as conn:
            count = persistence.flush_dirty(conn, self.tick_id)
        self._last_flush_at = time.monotonic()
        return count

    def _jev_init(self) -> None:
        if self._jev_worker is not None:
            return
        if not jev_mod.enabled():
            logger.info("jev tier disabled: SOULSCAPE_JEV_TIER not set")
            return
        if not jev_mod.api_key_present():
            logger.warning("jev tier auto-disabled: TYPESAFE_API_KEY not set")
            return
        queue = jev_queue_mod.JevEventQueue()
        worker = jev_worker_mod.JevWorker(
            queue=queue,
            breaker=jev_mod.JevBreaker(),
            emit_fn=self._jev_emit,
            telemetry_fn=self._jev_telemetry,
            suppressed_fn=self._jev_suppressed,
        )
        self._jev_queue = queue
        self._jev_worker = worker
        self._jev_boot_at = time.time()
        self._jev_last_restless_sweep = self._jev_boot_at
        self.agent_pool.jev_note = self._jev_note
        try:
            worker.start()
        except Exception:
            logger.exception("jev tier failed to start; continuing without it")
            self._jev_shutdown()
            return
        logger.info("jev tier enabled: dedicated worker started")

    def _jev_shutdown(self) -> None:
        worker = self._jev_worker
        if worker is None:
            return
        self.agent_pool.jev_note = None
        self._jev_worker = None
        self._jev_queue = None
        try:
            worker.stop()
        except Exception:
            logger.exception("jev worker stop failed")

    def _jev_note(
        self, soul_id: str, kind: str, floor_override: float | None = None
    ) -> None:
        worker = self._jev_worker
        queue = self._jev_queue
        if worker is None or queue is None:
            return
        now = time.time()
        snapshot = self._jev_snapshot(soul_id, [kind], now)
        if snapshot is None:
            return
        try:
            worker.note(soul_id, kind, now, snapshot, floor_override=floor_override)
        except Exception:
            logger.exception("jev note failed for %s", soul_id)

    def _jev_carry_changed(self, intent: dict, result: dict | None) -> None:
        if not isinstance(result, dict):
            return
        phase = result.get("phase")
        if phase == "grab":
            pass
        elif phase == "release":
            if not result.get("was_carried"):
                return
        elif phase == "move":
            if not result.get("escaped"):
                return
        else:
            return
        self._jev_note(intent["soul_id"], "carry_change")

    def _jev_snapshot(
        self,
        soul_id: str,
        kinds: list[str],
        now: float,
        position_override: tuple[float, float] | None = None,
    ) -> dict | None:
        queue = self._jev_queue
        if queue is None:
            return None
        try:
            return jev_brain_mod.build_snapshot(
                self.agent_pool,
                soul_id,
                self.vision,
                resources.node_provider(),
                kinds,
                now,
                queue.last_event_at(soul_id),
                position_override=position_override,
            )
        except Exception:
            logger.exception("jev snapshot failed for %s", soul_id)
            return None

    def _jev_emit(
        self, soul_id: str, goal: str, action: str, payload: dict
    ) -> str | None:
        try:
            intent_id = self.agent_pool.validate_and_enqueue(
                soul_id,
                action,
                payload,
                resources.node_provider(),
                self.tick_id,
            )
            if intent_id is None:
                logger.debug("jev emit rejected for %s: %s", soul_id, action)
            return intent_id
        except Exception:
            logger.exception("jev emit failed for %s", soul_id)
            return None

    def _jev_telemetry(self, row: jev_telemetry_mod.JevUsageRow) -> None:
        try:
            with database.get_db() as conn:
                jev_telemetry_mod.record_usage(conn, row)
                conn.commit()
        except Exception:
            logger.exception("jev telemetry write failed")

    def _jev_suppressed(self, soul_id: str, snapshot: dict) -> bool:
        if snapshot.get("moving"):
            return True
        bands = snapshot.get("need_bands") or {}
        if bands.get("satiety") == "low" or bands.get("hydration") == "low":
            return True
        if snapshot.get("fear", 0.0) > reflex.FLEE_FEAR:
            return True
        if snapshot.get("state") == biology.STATE_COLLAPSED:
            return True
        try:
            if dormancy.is_dormant(snapshot.get("essence")):
                return True
        except Exception:
            logger.exception("jev dormancy check failed")
        try:
            if soul_id in affection.carried_souls():
                return True
        except Exception:
            logger.exception("jev carried check failed")
        return False

    @staticmethod
    def _jev_jitter(soul_id: str) -> float:
        digest = hashlib.sha256(soul_id.encode()).digest()
        return (int.from_bytes(digest[:2], "big") % 1500) / 100.0

    def _jev_restlessness_sweep(self, now: float) -> None:
        queue = self._jev_queue
        worker = self._jev_worker
        if queue is None or worker is None:
            return
        try:
            candidates = self._agent_candidates()
        except Exception:
            logger.exception("jev restlessness sweep: candidates failed")
            return
        for soul_id in candidates:
            try:
                last = self._jev_boot_at
                queued = queue.last_event_at(soul_id)
                if queued is not None:
                    last = max(last, queued)
                emitted = self.agent_pool.last_emit_at.get(soul_id)
                if emitted is not None:
                    last = max(last, emitted)
                if now - last >= jev_mod.RESTLESS_AFTER_S + self._jev_jitter(soul_id):
                    self._jev_note(soul_id, "restlessness")
            except Exception:
                logger.exception("jev restlessness sweep failed for %s", soul_id)
        try:
            worker.prune_sensors(set(candidates))
        except Exception:
            logger.exception("jev sensor prune failed")

    def _jev_movement_completed(self, arrived: list[tuple[str, float, float]]) -> None:
        worker = self._jev_worker
        queue = self._jev_queue
        if worker is None or queue is None or not arrived:
            return
        now = time.time()
        with database.get_db() as conn:
            for soul_id, tx, ty in arrived:
                snapshot = self._jev_snapshot(
                    soul_id, ["movement_complete"], now, position_override=(tx, ty)
                )
                if snapshot is None:
                    continue
                try:
                    worker.note(soul_id, "movement_complete", now, snapshot)
                except Exception:
                    logger.exception("jev movement note failed for %s", soul_id)
                    continue
                intent_id = worker.wander_intent_id(soul_id)
                if intent_id is None:
                    continue
                try:
                    row = conn.execute(
                        "SELECT payload, result FROM intents WHERE intent_id = ?",
                        (intent_id,),
                    ).fetchone()
                except Exception:
                    logger.exception("jev completion lookup failed for %s", soul_id)
                    continue
                if row is None:
                    continue
                try:
                    payload = json.loads(row["payload"] or "{}")
                    result = json.loads(row["result"] or "{}")
                except Exception:
                    logger.exception("jev completion parse failed for %s", soul_id)
                    continue
                if payload.get("wander") is not True:
                    continue
                if result.get("x") != tx or result.get("y") != ty:
                    continue
                try:
                    worker.on_wander_leg_complete(soul_id, now, snapshot)
                except Exception:
                    logger.exception("jev wander leg complete failed for %s", soul_id)

    async def _run_loop(self) -> None:
        next_deadline = time.monotonic() + self.tick_dt
        while self.running:
            now = time.monotonic()
            delay = next_deadline - now
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                missed = int(-delay // self.tick_dt)
                if missed:
                    self.skipped_ticks += missed
                    self.tick_id += missed
                    next_deadline += missed * self.tick_dt
            next_deadline += self.tick_dt
            started = time.perf_counter()
            await asyncio.to_thread(self.step)
            self._record_duration((time.perf_counter() - started) * 1000.0)

    def _record_duration(self, duration_ms: float) -> None:
        self.last_duration_ms = duration_ms
        if self.avg_duration_ms == 0.0:
            self.avg_duration_ms = duration_ms
        else:
            self.avg_duration_ms += (duration_ms - self.avg_duration_ms) * 0.1
        self.max_duration_ms = max(self.max_duration_ms, duration_ms)

    def pump_intents(self) -> int:
        done = 0
        with self._step_lock:
            done = self._pump_intents_locked()
        return done

    def _pump_intents_locked(self) -> int:
        done = 0
        for intent in intents.pending_intents():
            self._adjudicate_one(intent)
            done += 1
        return done

    def _adjudicate_one(self, intent: dict) -> None:
        """Adjudicate a single intent through the real dispatch.

        Extracted from the pump loop so the replay CLI (issue #38)
        re-runs exactly this code path per intent, in journal order,
        against a temp DB -- same function, no fork.
        """
        try:
            if intent["kind"] == "move_to":
                self._adjudicate_move_to(intent)
            elif intent["kind"] in market.MARKET_KINDS:
                market.adjudicate_market_intent(self, intent)
            elif intent["kind"] in social.SOCIAL_KINDS:
                social.adjudicate_social_intent(self, intent)
            elif intent["kind"] in plots.PLOT_KINDS:
                plots.adjudicate_plot_intent(self, intent)
            elif intent["kind"] in biology.BIOLOGY_KINDS:
                biology.adjudicate_feed_soul(self, intent)
            elif intent["kind"] in metering.METERING_KINDS:
                metering.adjudicate_metering_intent(self, intent)
            elif intent["kind"] in agents.consume.CONSUME_KINDS:
                agents.consume.adjudicate_consume(
                    self, intent, resources.node_provider()
                )
            elif intent["kind"] in resources.RESOURCE_KINDS:
                resources.adjudicate_gather(
                    self, intent, now=determinism.tick_now(self)
                )
            elif intent["kind"] == presence_module.KIND_TAMER_PRESENCE:
                presence_module.adjudicate_presence_intent(self, intent)
            elif intent["kind"] in affection.AFFECTION_KINDS:
                outcome = affection.adjudicate_affection_intent(self, intent)
                if outcome["status"] == "adjudicated":
                    if intent["kind"] in (affection.KIND_CHIRP, affection.KIND_PET):
                        self._jev_note(intent["soul_id"], "tamer_poke")
                    elif intent["kind"] == affection.KIND_CARRY_MOVE:
                        self._jev_carry_changed(intent, outcome["result"])
            elif intent["kind"] == bridge.BRIDGE_INTENT_KIND:
                bridge.adjudicate_bridge_event(self, intent)
            elif intent["kind"] == "look":
                self._adjudicate_look(intent)
            elif intent["kind"] == "rest":
                self._adjudicate_rest(intent)
            elif intent["kind"] == "wait":
                self._adjudicate_wait(intent)
            else:
                with database.get_db() as conn:
                    self._reject(conn, intent, "unknown_kind")
        except Exception:
            logger.exception("Intent adjudication failed: %s", intent["intent_id"])
            with database.get_db() as conn:
                try:
                    self._reject(conn, intent, "internal")
                except Exception:
                    logger.exception("Intent rejection failed: %s", intent["intent_id"])

    def _reject(self, conn, intent: dict, reason: str) -> None:
        result = {"reason": reason}
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                ("rejected", json.dumps(result), intent["intent_id"]),
            )
            persistence.append_event(
                conn,
                self.tick_id,
                persistence.EVENT_INTENT_REJECTED,
                {
                    "intent_id": intent["intent_id"],
                    "kind": intent["kind"],
                    "soul_id": intent["soul_id"],
                    "reason": reason,
                },
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        # #26: rejections are notable intent outcomes (salience-gated:
        # adjudications are too frequent to log; rejections are rare).
        # #28: tamer_presence intents are tamer-scoped and may carry a
        # synthetic soul_id ("tamer:<id>") -- never an episode key.
        if intent["kind"] != presence_module.KIND_TAMER_PRESENCE:
            agents.memory.log_episode(
                intent["soul_id"],
                "intent_outcome",
                {
                    "summary": f"intent {intent['kind']} rejected: {reason}",
                    "intent_id": intent["intent_id"],
                    "kind": intent["kind"],
                    "reason": reason,
                },
                salience=0.6,
            )

    def _adjudicate_move_to(self, intent: dict) -> None:
        intent_id = intent["intent_id"]
        soul_id = intent["soul_id"]
        payload = intent["payload"] or {}
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT position, custodian_id, owner_id, satiety, state, "
                "COALESCE(essence, 0.0) AS essence "
                "FROM souls WHERE soul_id = ?",
                (soul_id,),
            )
            row = cursor.fetchone()
            if row is None:
                self._reject(conn, intent, "soul_not_found")
                return
            if (row["state"] or biology.STATE_NORMAL) == biology.STATE_COLLAPSED:
                # Statues don't walk: a collapsed soul cannot move.
                self._reject(conn, intent, "collapsed")
                return
            # Dormancy (issue #22): frozen souls don't move. Re-checked
            # here (defense in depth) because the WS ingress rejects
            # early but REST/unknown paths may not.
            if dormancy.is_dormant(row["essence"]):
                self._reject(conn, intent, "soul_dormant")
                return
            # Carry (issue #31): while a soul is being carried, its
            # normal movement intents are suspended.
            if soul_id in affection.carried_souls():
                self._reject(conn, intent, "carried")
                return
            custodian = row["custodian_id"] or row["owner_id"]
            if intent["custodian_id"] is not None and (
                custodian != intent["custodian_id"]
            ):
                self._reject(conn, intent, "custody")
                return
            # Hungry souls (satiety <= 50) move at 75% speed (issue #21).
            if payload.get("wander") is True and payload.get("pace") == "amble":
                speed = jev_mod.JEV_AMBLE_SPEED
            else:
                speed = INTENT_MOVE_SPEED * biology.speed_multiplier(
                    biology.full_or_100(row["satiety"])
                )
            x, y = _parse_pair(row["position"])
            unflushed = persistence.dirty_get(soul_id)
            if unflushed is not None and unflushed.get("position") is not None:
                x, y = unflushed["position"]
            tx = min(
                max(float(payload["x"]), _POSITION_MARGIN),
                database.SCREEN_BOUNDS[0] - _POSITION_MARGIN,
            )
            ty = min(
                max(float(payload["y"]), _POSITION_MARGIN),
                database.SCREEN_BOUNDS[1] - _POSITION_MARGIN,
            )
            if not plots.can_enter_plot(
                conn,
                soul_id,
                tx,
                ty,
                is_operator=intent["custodian_id"] is None,
            ):
                self._reject(conn, intent, "plot_closed")
                return
            dx, dy = tx - x, ty - y
            dist = math.hypot(dx, dy)
            clamped = False
            max_reach = speed * INTENT_HORIZON_SECONDS
            if dist > max_reach:
                ratio = max_reach / dist
                tx, ty = x + dx * ratio, y + dy * ratio
                clamped = True
                logger.warning(
                    "Intent %s target clamped to reachable radius", intent_id
                )
            result = {
                "x": tx,
                "y": ty,
                "clamped": clamped,
                "speed": speed,
            }
            if math.hypot(tx - x, ty - y) <= INTENT_ARRIVAL_EPS:
                velocity = [0.0, 0.0]
                move_target = None
            else:
                dist = math.hypot(tx - x, ty - y)
                velocity = [
                    (tx - x) / dist * speed,
                    (ty - y) / dist * speed,
                ]
                move_target = [tx, ty]
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute(
                    "UPDATE souls SET velocity = ?, move_target = ? WHERE soul_id = ?",
                    (
                        json.dumps(velocity),
                        json.dumps(move_target) if move_target is not None else None,
                        soul_id,
                    ),
                )
                cursor.execute(
                    "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                    ("adjudicated", json.dumps(result), intent_id),
                )
                persistence.append_event(
                    conn,
                    self.tick_id,
                    persistence.EVENT_INTENT_ADJUDICATED,
                    {
                        "intent_id": intent_id,
                        "kind": "move_to",
                        "soul_id": soul_id,
                        "params": result,
                        "velocity": velocity,
                        "move_target": move_target,
                    },
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        persistence.dirty.mark(
            soul_id, position=[x, y], velocity=velocity, move_target=move_target
        )

    def step(self) -> int:
        with self._step_lock:
            # #26: cheap nightly-summarizer check (in-memory guard;
            # hits the DB at most once per hour).
            agents.memory.tick_maintenance()
            # #27: metering batcher -- ingest + cadence/threshold batch
            # creation. New debit intents settle in pump_intents() below,
            # on this same tick.
            metering.maybe_run_batch()
            self.pump_intents()
            # Issue #35: expedition trigger sweep every 60 s (drive-driven,
            # never raises), then advance active expeditions BEFORE movement
            # integration so this tick's steering moves souls immediately.
            if self.tick_id % expeditions.TRIGGER_SWEEP_EVERY_TICKS == 0:
                try:
                    expeditions.maybe_trigger(self.tick_id, time.time())
                except Exception:
                    logger.exception("expedition trigger sweep failed")
            try:
                expeditions.advance_tick(self.tick_id, time.time())
            except Exception:
                logger.exception("expedition advance failed")
            if (self.tick_id + 1) % biology.BIOLOGY_EVERY_TICKS == 0:
                # 0.1 Hz server-side biology: needs decay, starvation-only
                # collapse, fed+rested recovery (issue #21). Fires after
                # every 50th step -- never on the very first step.
                with database.get_db() as conn:
                    biology.apply_biology_tick(
                        conn, self.tick_id, time.time(), self.tick_dt
                    )
            moved = 0
            bounds = database.SCREEN_BOUNDS
            # Carry (issue #31): snapshot once per tick; a carried soul's
            # position is owned by the tamer's drag stream.
            carried = affection.carried_souls()
            arrived: list[tuple[str, float, float]] = []
            with database.get_db() as conn:
                rows = conn.execute(
                    "SELECT soul_id, position, velocity, move_target, "
                    "COALESCE(essence, 0.0) AS essence FROM souls"
                ).fetchall()
                closed_barrier = plots.has_closed_plots(conn)
                for row in rows:
                    soul_id = row["soul_id"]
                    # Dormancy (issue #22): dormant souls are skipped in
                    # physics -- position frozen, no velocity integration.
                    # Their move_target is left intact (frozen mid-stride);
                    # motion resumes on wake.
                    if dormancy.is_dormant(row["essence"]):
                        continue
                    # Carry (issue #31): normal integration is suspended
                    # for the carry duration.
                    if soul_id in carried:
                        continue
                    try:
                        x, y = _parse_pair(row["position"])
                        vx, vy = _parse_pair(row["velocity"])
                    except (ValueError, TypeError, KeyError, IndexError):
                        continue
                    unflushed = persistence.dirty_get(soul_id)
                    if unflushed is not None:
                        if unflushed.get("position") is not None:
                            x, y = unflushed["position"]
                        if unflushed.get("velocity") is not None:
                            vx, vy = unflushed["velocity"]
                        target = unflushed.get("move_target", None)
                        if "move_target" not in unflushed:
                            target = self._parse_target(row["move_target"])
                    else:
                        target = self._parse_target(row["move_target"])
                    if vx == 0.0 and vy == 0.0:
                        continue
                    (nx, ny), (nvx, nvy), new_target = persistence.integrate_soul(
                        (x, y), (vx, vy), target, self.tick_dt, bounds
                    )
                    did_arrive = target is not None and new_target is None
                    if closed_barrier and not plots.can_enter_plot(
                        conn, soul_id, nx, ny
                    ):
                        (nx, ny), (nvx, nvy), new_target = (
                            (x, y),
                            (0.0, 0.0),
                            None,
                        )
                        did_arrive = False
                    persistence.dirty.mark(
                        soul_id,
                        position=[nx, ny],
                        velocity=[nvx, nvy],
                        move_target=new_target,
                    )
                    moved += 1
                    if did_arrive:
                        arrived.append((soul_id, target[0], target[1]))
            self.vision.rebuild()
            coarse_events: dict[str, dict] = {}
            # Issue #34: resource-node respawn sweep every 100 ticks
            # (0.05 Hz). Silent unless nodes regrow; never raises out.
            if self.tick_id % resources.RESPAWN_SWEEP_EVERY_TICKS == 0:
                try:
                    with database.get_db() as conn:
                        resources.respawn_sweep(conn, time.time())
                except Exception:
                    logger.exception("resource respawn sweep failed")
            if self.tick_id % world.COARSE_DIFF_EVERY_TICKS == 0:
                coarse_events = self.vision.diff_coarse()
            # Issue #32: mailbag expiry sweep every 60 s (no new thread;
            # the existing tick cadence). Questions pending past 48 h
            # expire unanswered, decay loyalty, and queue recap rows.
            if self.tick_id % mailbag.MAILBAG_SWEEP_EVERY_TICKS == 0:
                try:
                    mailbag.sweep_expired(time.time(), self.tick_id)
                except Exception:
                    logger.exception("mailbag sweep failed")
            # Issue #33: ambient recap sweep every 12 min (no new thread;
            # the lazy on-unlock path is primary -- this backstops souls
            # whose tamers never unlock) and 30-day retention compaction
            # daily. The sweep skips tick 0 (boot): generation writes
            # journal rows, and the first tick must stay journal-silent
            # for the persistence/replay tests; the next sweep 12 min
            # later covers any overdue souls.
            if self.tick_id > 0 and self.tick_id % recap.RECAP_SWEEP_EVERY_TICKS == 0:
                try:
                    recap.sweep_recaps(time.time())
                except Exception:
                    logger.exception("recap sweep failed")
            if self.tick_id % recap.COMPACT_EVERY_TICKS == 0:
                try:
                    recap.compact_retention(time.time())
                except Exception:
                    logger.exception("retention compaction failed")
            self._run_agent_pool(coarse_events)
            if wander_mod.ENABLED:
                try:
                    wander_mod.sweep(self)
                except Exception:
                    logger.exception("baseline wander sweep failed")
            try:
                self._jev_movement_completed(arrived)
            except Exception:
                logger.exception("jev movement completion failed")
            now = time.time()
            if now - self._jev_last_restless_sweep >= jev_mod.SWEEP_EVERY_S:
                self._jev_last_restless_sweep = now
                try:
                    self._jev_restlessness_sweep(now)
                except Exception:
                    logger.exception("jev restlessness sweep failed")
            self.tick_id += 1
            self.souls_moved_last_tick = moved
            self._maybe_flush()
            self._maybe_snapshot()
            return moved

    def _adjudicate_look(self, intent: dict) -> None:
        intent_id = intent["intent_id"]
        soul_id = intent["soul_id"]
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT soul_id FROM souls WHERE soul_id = ?", (soul_id,)
            ).fetchone()
            if row is None:
                self._reject(conn, intent, "soul_not_found")
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                    ("adjudicated", json.dumps({"at": self.tick_id}), intent_id),
                )
                persistence.append_event(
                    conn,
                    self.tick_id,
                    persistence.EVENT_INTENT_ADJUDICATED,
                    {
                        "intent_id": intent_id,
                        "kind": "look",
                        "soul_id": soul_id,
                    },
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _adjudicate_rest(self, intent: dict) -> None:
        intent_id = intent["intent_id"]
        soul_id = intent["soul_id"]
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT soul_id FROM souls WHERE soul_id = ?", (soul_id,)
                ).fetchone()
                if row is None:
                    conn.rollback()
                    self._reject(conn, intent, "soul_not_found")
                    return
                conn.execute(
                    "UPDATE souls SET activity = 'rest' WHERE soul_id = ?",
                    (soul_id,),
                )
                persistence.dirty.mark(
                    soul_id,
                    velocity=[0.0, 0.0],
                    move_target=None,
                )
                conn.execute(
                    "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                    ("adjudicated", json.dumps({"at": self.tick_id}), intent_id),
                )
                persistence.append_event(
                    conn,
                    self.tick_id,
                    persistence.EVENT_INTENT_ADJUDICATED,
                    {
                        "intent_id": intent_id,
                        "kind": "rest",
                        "soul_id": soul_id,
                    },
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _adjudicate_wait(self, intent: dict) -> None:
        intent_id = intent["intent_id"]
        soul_id = intent["soul_id"]
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT soul_id FROM souls WHERE soul_id = ?", (soul_id,)
                ).fetchone()
                if row is None:
                    conn.rollback()
                    self._reject(conn, intent, "soul_not_found")
                    return
                persistence.dirty.mark(
                    soul_id,
                    velocity=[0.0, 0.0],
                    move_target=None,
                )
                conn.execute(
                    "UPDATE intents SET status = ?, result = ? WHERE intent_id = ?",
                    ("adjudicated", json.dumps({"at": self.tick_id}), intent_id),
                )
                persistence.append_event(
                    conn,
                    self.tick_id,
                    persistence.EVENT_INTENT_ADJUDICATED,
                    {
                        "intent_id": intent_id,
                        "kind": "wait",
                        "soul_id": soul_id,
                    },
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _note_vision_events(self, coarse_events: dict[str, dict], now: float) -> None:
        sched = agents.scheduler.default()
        for soul_id, events in coarse_events.items():
            if events.get("entered"):
                sched.note_vision_enter(soul_id, now)
                self._jev_note(soul_id, "vision_enter")
            if events.get("exited"):
                self._jev_note(soul_id, "vision_exit")

    def _run_agent_pool(self, coarse_events: dict[str, dict]) -> None:
        """Agent pool phase (issue #24): bounded reflex thinks.

        Coarse-vision enter events pull think times forward; then up to
        THINK_MAX_PER_TICK due souls think (budget spreads work across
        ticks so the 500-soul 14.3ms envelope holds). Dormant and
        collapsed souls are skipped before scheduling.

        Steady-state cost is ~zero: a COUNT(*) plus the scheduler's
        earliest_due() lets the phase return before touching the souls
        table when no soul can be due. A changed soul count refreshes
        candidates (newborns think promptly) and prunes stale entries.
        """
        now = time.time()
        sched = agents.scheduler.default()
        tracker = agents.deliberation.tracker()
        tracker.bind_scheduler(sched)
        self._note_vision_events(coarse_events, now)
        with database.get_db() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM souls").fetchone()["c"]
        if sched.needs_boot() or count != self._agent_soul_count:
            candidates = self._agent_candidates()
            self._agent_soul_count = count
            if sched.needs_boot():
                sched.rebuild_on_boot(candidates, now)
                tracker.seed_stagger(candidates, now)
            else:
                sched.prune(set(candidates))
        else:
            earliest = sched.earliest_due()
            if earliest is not None and earliest > now:
                return
            candidates = self._agent_candidates()
        due = sched.due(now, candidates)
        # #25 promotion: escalated souls deliberate instead of reflex-
        # thinking. Idle-floor souls (eligible at most every 5 min) are
        # lowest priority and capped per tick -- idle souls think rarely.
        escalations: dict[str, str] = {}
        for soul_id in due:
            reason = tracker.should_escalate(soul_id, now)
            if reason is not None:
                escalations[soul_id] = reason
        idle_left = agents.deliberation.IDLE_DELIBERATIONS_PER_TICK
        for soul_id in due:
            if soul_id in escalations or idle_left <= 0:
                continue
            if tracker.idle_eligible(soul_id, now):
                escalations[soul_id] = "idle"
                idle_left -= 1
        reflex_ids = [s for s in due if s not in escalations]
        if due:
            self.agent_pool.run_thinks(
                reflex_ids,
                self.vision,
                resources.node_provider(),
                self.tick_id,
                now,
                deliberate_ids=set(escalations),
                escalations=escalations,
            )

    def _agent_candidates(self) -> list[str]:
        """Soul ids eligible for a think: awake and uncollapsed."""
        with database.get_db() as conn:
            rows = conn.execute(
                "SELECT soul_id, state, COALESCE(essence, 0.0) AS essence FROM souls"
            ).fetchall()
        return [
            row["soul_id"]
            for row in rows
            if (row["state"] or biology.STATE_NORMAL) != biology.STATE_COLLAPSED
            and not dormancy.is_dormant(row["essence"])
        ]

    @staticmethod
    def _parse_target(raw) -> tuple[float, float] | None:
        if not raw:
            return None
        try:
            return _parse_pair(raw)
        except (ValueError, TypeError, IndexError):
            return None

    def _maybe_flush(self) -> None:
        if (
            time.monotonic() - self._last_flush_at >= persistence.FLUSH_INTERVAL_S
            or len(persistence.dirty) >= persistence.FLUSH_MAX_DIRTY
        ):
            self.flush()

    def _maybe_snapshot(self) -> None:
        if self.tick_id - self._last_snapshot_tick >= persistence.SNAPSHOT_EVERY_TICKS:
            with database.get_db() as conn:
                persistence.take_snapshot(
                    conn, self.tick_id, scenario_seed=self.scenario_seed
                )
                persistence.prune_snapshots(conn)
            self._last_snapshot_tick = self.tick_id

    def snapshot(self) -> dict:
        positions = [
            {"soul_id": soul_id, "x": x, "y": y}
            for soul_id, (x, y) in persistence.read_positions_through().items()
        ]
        with database.get_db() as conn:
            journal_seq = persistence.journal_head(conn)
            snaps = persistence.latest_snapshots(conn, limit=1)
        return {
            "enabled": self.enabled,
            "running": self.running,
            "tick_hz": round(1.0 / self.tick_dt, 3),
            "tick_id": self.tick_id,
            "world_time": WORLD_EPOCH + self.tick_id * self.tick_dt,
            "last_tick_duration_ms": round(self.last_duration_ms, 3),
            "avg_tick_duration_ms": round(self.avg_duration_ms, 3),
            "max_tick_duration_ms": round(self.max_duration_ms, 3),
            "souls_moved_last_tick": self.souls_moved_last_tick,
            "skipped_ticks": self.skipped_ticks,
            "soul_count": len(positions),
            "positions": positions,
            "vision_entities": len(self.vision.grid),
            "dirty_entries": len(persistence.dirty),
            "journal_head_seq": journal_seq,
            "last_snapshot": snaps[0] if snaps else None,
            "rpo_seconds": persistence.FLUSH_INTERVAL_S,
        }
