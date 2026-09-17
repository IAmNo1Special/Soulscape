"""Bounded async agent pool (issue #24).

One think per due soul: gather senses -> reflex evaluation ->
vocabulary validation -> execution-time revalidation -> enqueue.

The pool is asyncio.Semaphore-bounded (MAX_CONCURRENT_THINKS) so the
#25 LLM deliberation path can share the same structure; v0 reflex
thinks are CPU/DB-trivial and never block the loop.

Execution-time revalidation (the agent-side pre-enqueue gate, ahead of
the #14/#17 adjudication revalidation):
  1. soul still exists, awake (not dormant), uncollapsed;
  2. payload well-formed for the action;
  3. target_ref still valid (food/water still at the referenced spot);
  4. wallet still covers the action's price (post/reply today).
A stale intent is rejected SILENTLY: a journal row
(agent_stale_reject), no intent enqueued, nothing surfaced.

Off-menu emissions never reach validation: they normalize to the fixed
peaceful sensation, recorded in the ring + journal.

Enqueue uses session "agent-pool" with a fresh nonce per intent;
custodian_id is the soul's own custodian so the move adjudication's
custody check passes while closed-plot rules still apply (the soul's
own reflex does not bypass other tamers' closed plots).
"""

import asyncio
import json
import logging
import math
import random
import secrets
import time

from .. import biology, database, dormancy, intents, persistence
from . import drives, reflex, scheduler, sensations, vocab

logger = logging.getLogger("soulscape_hub")

#: Session id under which agent-pool intents are enqueued.
POOL_SESSION_ID = "agent-pool"

#: Bound on concurrent think tasks.
MAX_CONCURRENT_THINKS = 8

#: Essence price the revalidation gate checks for paid vocab actions
#: the pool can emit in v0. claim_plot's ring-priced fee is #25 scope.
_PAID_ACTIONS = {"post": 20.0, "reply": 8.0}


def _as_pair(raw) -> tuple[float, float] | None:
    try:
        x, y = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not math.isfinite(x) or not math.isfinite(y):
        return None
    return (x, y)


def validate_agent_payload(action: str, payload: dict) -> dict | None:
    """Canonical payload for an agent-emitted action, or None if bad.

    Covers the actions the v0 reflex layer emits. Deliberation actions
    (#25) get full validators there.
    """
    payload = payload or {}
    if action == "move_to":
        try:
            x, y = float(payload["x"]), float(payload["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        out = {"x": x, "y": y}
        ref = payload.get("target_ref")
        if isinstance(ref, dict) and _as_pair(ref.get("at")) is not None:
            out["target_ref"] = {
                "kind": str(ref.get("kind", "")),
                "at": [float(ref["at"][0]), float(ref["at"][1])],
            }
        return out
    if action == "eat":
        pair = _as_pair(payload.get("food"))
        return {"food": [pair[0], pair[1]]} if pair else None
    if action == "drink":
        pair = _as_pair(payload.get("water"))
        return {"water": [pair[0], pair[1]]} if pair else None
    if action in ("look", "rest", "wait"):
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    return None


def _journal(conn, tick_id: int, event_type: str, payload: dict) -> None:
    persistence.append_event(conn, tick_id, event_type, payload)
    conn.commit()


def record_illegal(soul_id: str, action: str, tick_id: int = 0) -> dict:
    """Normalize an off-menu emission to the peaceful sensation.

    Recorded in the ring and the journal; never becomes an intent.
    """
    with database.get_db() as conn:
        sensation = sensations.record(
            soul_id,
            vocab.PEACEFUL_SENSATION,
            cause=f"illegal_action:{action}",
            tick_id=tick_id,
            journal_conn=conn,
        )
        conn.commit()
    return sensation


def execution_revalidate(
    row: dict,
    action: str,
    payload: dict,
    provider: reflex.FoodWaterProvider,
) -> tuple[bool, str]:
    """Pre-enqueue gate against live state. (True, "") or (False, reason)."""
    if row is None:
        return False, "soul_not_found"
    if (row.get("state") or biology.STATE_NORMAL) == biology.STATE_COLLAPSED:
        return False, "collapsed"
    if dormancy.is_dormant(row.get("essence")):
        return False, "soul_dormant"
    canonical = validate_agent_payload(action, payload)
    if canonical is None:
        return False, "bad_payload"
    ref = canonical.get("target_ref") if action == "move_to" else None
    if ref is not None:
        kind, (ax, ay) = ref["kind"], ref["at"]
        if kind == "food" and not provider.is_food_at(ax, ay):
            return False, "target_gone"
        if kind == "water" and not provider.is_water_at(ax, ay):
            return False, "target_gone"
    price = _PAID_ACTIONS.get(action)
    if price is not None and float(row.get("essence") or 0.0) < price:
        return False, "insufficient_essence"
    return True, ""


class AgentPool:
    def __init__(
        self,
        max_concurrent: int = MAX_CONCURRENT_THINKS,
        think_scheduler: scheduler.ThinkScheduler | None = None,
        seed: int | None = None,
    ) -> None:
        self.max_concurrent = max_concurrent
        self.think_scheduler = think_scheduler or scheduler.default()
        self._rng = random.Random(seed)
        self._observations: dict[str, dict] = {}

    def last_observation(self, soul_id: str) -> dict | None:
        """Most recent think observation payload for a soul.

        Carries the soul's recent sensations (sensations requirement).
        The viewport stream does not include these yet -- hook for #30.
        """
        return self._observations.get(soul_id)

    def _load_soul(self, soul_id: str) -> dict | None:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT soul_id, position, velocity, nature, state, "
                "COALESCE(essence, 0.0) AS essence, "
                "COALESCE(satiety, 100.0) AS satiety, "
                "COALESCE(hydration, 100.0) AS hydration, "
                "COALESCE(hp, 100.0) AS hp, "
                "COALESCE(max_hp, 100.0) AS max_hp, "
                "custodian_id, owner_id "
                "FROM souls WHERE soul_id = ?",
                (soul_id,),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _parse_pair(raw) -> tuple[float, float]:
        if raw is None:
            return (0.0, 0.0)
        if isinstance(raw, str):
            raw = json.loads(raw)
        x, y = float(raw[0]), float(raw[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("non-finite pair")
        return (x, y)

    def _enqueue(self, row: dict, action: str, payload: dict) -> dict:
        custodian = row.get("custodian_id") or row.get("owner_id")
        nonce = "pool_" + secrets.token_urlsafe(16)
        return intents.enqueue_intent(
            POOL_SESSION_ID, nonce, custodian, row["soul_id"], action, payload
        )

    def validate_and_enqueue(
        self,
        soul_id: str,
        action: str,
        payload: dict,
        provider: reflex.FoodWaterProvider,
        tick_id: int,
    ) -> str | None:
        """Validate one emitted action and enqueue it, or handle rejection.

        Off-menu actions normalize to the peaceful sensation (never an
        intent). Stale intents are rejected silently into the journal.
        Returns the intent_id when enqueued, else None.
        """
        name, ok = vocab.validate(action)
        if not ok:
            record_illegal(soul_id, str(action), tick_id)
            return None
        canonical = validate_agent_payload(name, payload or {})
        if canonical is None:
            return None
        live = self._load_soul(soul_id)
        good, reason = execution_revalidate(live, name, canonical, provider)
        if not good:
            with database.get_db() as conn:
                sensations.journal_stale_reject(
                    conn, tick_id, soul_id, name, canonical, reason
                )
                conn.commit()
            return None
        return self._enqueue(live, name, canonical)["intent_id"]

    async def think(
        self,
        soul_id: str,
        vision,
        provider: reflex.FoodWaterProvider,
        tick_id: int,
        now: float,
    ) -> dict:
        """One reflex think for one soul. Returns a small summary dict."""
        row = self._load_soul(soul_id)
        if row is None:
            self.think_scheduler.forget(soul_id)
            return {"soul_id": soul_id, "status": "missing"}
        if (row.get("state") or biology.STATE_NORMAL) == biology.STATE_COLLAPSED:
            self.think_scheduler.schedule_next(soul_id, now)
            return {"soul_id": soul_id, "status": "collapsed"}
        if dormancy.is_dormant(row.get("essence")):
            self.think_scheduler.schedule_next(soul_id, now)
            return {"soul_id": soul_id, "status": "dormant"}
        try:
            x, y = self._parse_pair(row.get("position"))
        except (ValueError, TypeError, IndexError):
            x, y = 0.0, 0.0
        observations = vision.detail_observations(soul_id)
        # Feed #25's escalation tracker: wallet sightings (every ledger
        # applier flows through here) and detail-vision enters. Lazy
        # import: deliberation imports this module at top level.
        from . import deliberation as _delib

        _delib.tracker().note_wallet(soul_id, float(row.get("essence") or 0.0), now)
        _delib.tracker().check_detail_enters(soul_id, observations, now)
        drive_vec = drives.compute_drives(
            row.get("nature"),
            row.get("satiety"),
            row.get("hydration"),
            row.get("hp"),
            row.get("max_hp"),
            observations,
        )
        observation = {
            "soul_id": soul_id,
            "position": [x, y],
            "satiety": row.get("satiety"),
            "hydration": row.get("hydration"),
            "hp": row.get("hp"),
            "essence": row.get("essence"),
            "nature": row.get("nature"),
            "state": row.get("state"),
            "drives": drive_vec,
            "observations": observations,
            "emote": reflex.emote_of(soul_id),
        }
        result = reflex.evaluate(
            soul_id,
            x,
            y,
            float(row.get("satiety") or 100.0),
            float(row.get("hydration") or 100.0),
            drive_vec,
            observations,
            provider,
            self._rng,
            now,
        )
        with database.get_db() as conn:
            for s in result["sensations"]:
                sensations.record(
                    soul_id, s["text"], s["cause"], tick_id, journal_conn=conn
                )
            conn.commit()
        observation["sensations"] = sensations.recent(soul_id)
        observation["emote"] = result["emote"] or reflex.emote_of(soul_id)
        self._observations[soul_id] = observation
        enqueued: list[str] = []
        for emitted in result["intents"]:
            intent_id = self.validate_and_enqueue(
                soul_id,
                str(emitted.get("action")),
                emitted.get("payload") or {},
                provider,
                tick_id,
            )
            if intent_id is not None:
                enqueued.append(intent_id)
        self.think_scheduler.schedule_next(soul_id, now)
        return {
            "soul_id": soul_id,
            "status": "thought",
            "enqueued": enqueued,
            "emote": result["emote"],
        }

    async def deliberate(
        self,
        soul_id: str,
        vision,
        provider: reflex.FoodWaterProvider,
        tick_id: int,
        now: float,
        escalation: str | None,
        deliberator=None,
    ) -> dict:
        """One #25 deliberation for one soul (the promoted think path).

        Runs under the same semaphore as reflex thinks. The deliberator
        walks the provider chain; validated intents go through the same
        validate_and_enqueue path as reflex intents. On total provider
        failure (or any unexpected crash) the soul still acts: the #24
        reflex think runs instead. Nothing here ever raises out.
        """
        from . import deliberation as _delib

        row = self._load_soul(soul_id)
        if row is None:
            self.think_scheduler.forget(soul_id)
            return {"soul_id": soul_id, "status": "missing"}
        if (row.get("state") or biology.STATE_NORMAL) == biology.STATE_COLLAPSED:
            self.think_scheduler.schedule_next(soul_id, now)
            return {"soul_id": soul_id, "status": "collapsed"}
        if dormancy.is_dormant(row.get("essence")):
            self.think_scheduler.schedule_next(soul_id, now)
            return {"soul_id": soul_id, "status": "dormant"}
        _delib.tracker().note_wallet(soul_id, float(row.get("essence") or 0.0), now)
        thinker = deliberator or _delib.Deliberator(self)
        try:
            result = thinker.deliberate(
                soul_id, row, vision, provider, now, escalation, tick_id
            )
        except Exception:
            logger.exception("deliberation crashed for %s; reflex fallback", soul_id)
            result = {
                "status": "heuristic",
                "intents": [],
                "fallback_used": True,
            }
        if result.get("status") == "heuristic":
            summary = await self.think(soul_id, vision, provider, tick_id, now)
            summary["deliberation"] = "heuristic_fallback"
            summary["degraded_reason"] = result.get("degraded_reason")
            return summary
        enqueued: list[str] = []
        for action, payload in result.get("intents", []):
            intent_id = self.validate_and_enqueue(
                soul_id, action, payload, provider, tick_id
            )
            if intent_id is not None:
                enqueued.append(intent_id)
        self.think_scheduler.schedule_next(soul_id, now)
        return {
            "soul_id": soul_id,
            "status": "deliberated",
            "escalation": escalation,
            "tier": result.get("tier"),
            "provider": result.get("provider"),
            "model": result.get("model"),
            "fallback_used": result.get("fallback_used"),
            "enqueued": enqueued,
        }

    async def think_batch(
        self,
        soul_ids: list[str],
        vision,
        provider: reflex.FoodWaterProvider,
        tick_id: int,
        now: float,
        deliberate_ids: frozenset[str] | set[str] | None = None,
        escalations: dict[str, str] | None = None,
        deliberator=None,
    ) -> list[dict]:
        """Run thinks for due souls under the concurrency bound.

        Souls in deliberate_ids take the #25 deliberation path (with
        their escalation reason); the rest take the reflex path. One
        shared deliberator keeps flash-failure counters across the batch.
        """
        deliberate_ids = deliberate_ids or frozenset()
        escalations = escalations or {}
        if deliberate_ids and deliberator is None:
            from . import deliberation as _delib

            deliberator = _delib.Deliberator(self)
        sem = asyncio.Semaphore(self.max_concurrent)

        async def _one(soul_id: str) -> dict:
            async with sem:
                try:
                    if soul_id in deliberate_ids:
                        return await self.deliberate(
                            soul_id,
                            vision,
                            provider,
                            tick_id,
                            now,
                            escalations.get(soul_id),
                            deliberator,
                        )
                    return await self.think(soul_id, vision, provider, tick_id, now)
                except Exception:
                    logger.exception("agent think failed for %s", soul_id)
                    return {"soul_id": soul_id, "status": "error"}

        return list(await asyncio.gather(*(_one(sid) for sid in soul_ids)))

    def run_thinks(
        self,
        soul_ids: list[str],
        vision,
        provider: reflex.FoodWaterProvider,
        tick_id: int,
        now: float | None = None,
        deliberate_ids: frozenset[str] | set[str] | None = None,
        escalations: dict[str, str] | None = None,
        deliberator=None,
    ) -> list[dict]:
        """Sync entry for the tick loop (which runs off the event loop)."""
        now = time.time() if now is None else now
        return asyncio.run(
            self.think_batch(
                soul_ids,
                vision,
                provider,
                tick_id,
                now,
                deliberate_ids=deliberate_ids,
                escalations=escalations,
                deliberator=deliberator,
            )
        )
