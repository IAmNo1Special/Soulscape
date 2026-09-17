"""Agent pool v0 (issue #24): Hub-side reflex-layer cognition.

A bounded async pool runs one reflex think per due Soul per tick. Each
think gathers senses (vision detail observations + drives + wallet /
biology), evaluates the free reflex layer, validates emitted actions
against the closed action vocabulary, revalidates at execution time
against live state, and enqueues validated intents into the durable
intent queue (#14/#17) for tick-boundary adjudication.

Layout
------
vocab:      closed action vocabulary (VOCAB_VERSION=1), alias table,
            illegal/off-menu -> fixed peaceful sensation.
drives:     arithmetic-only drive vector from nature + live state.
reflex:     free reflex layer (eat/drink/flee thresholds, jittered
            emote selection) against a FoodWaterProvider interface.
scheduler:  per-soul think schedule (jittered interval, minimum gap,
            event pull-forward, per-tick budget). In-memory, rebuilt on
            boot; dormancy wakes reset it via dormancy.reset_think_schedule.
sensations: illegal-intent and notable reflex outcomes are recorded per
            soul in a bounded in-memory ring plus durable journal rows;
            recent sensations ride in the think observation payload.
consume:    tick-side adjudication of the reflex-only eat/drink intents
            (#34 builds real resource nodes; until then a provider that
            returns none makes these intents unreachable in production).
pool:       bounded async pool; execution-time revalidation just before
            enqueue; stale intents rejected silently into the journal.
deliberation: #25 LLM tier: escalation triggers promote thinks,
            flash/pro routing, token-capped prompts, structured JSON
            outputs, provider fallback chain, llm_usage metering.

Design decisions
---------------
- No LLM in the reflex path. Reflexes are pure arithmetic so
  a 500-soul tick stays inside its 14.3ms envelope; the per-tick
  think budget (K=4) spreads work across ticks. The metered LLM tier
  lives in deliberation.py and runs under the same pool semaphore,
  never on the tick hot path.
- Execution-time revalidation is the agent-side pre-enqueue check.
  #14/#17 already revalidate at adjudication; this is the earlier,
  cheaper gate, and stale intents are journaled (not surfaced).
- Escalation triggers for #25 (e.g. >=3 reflex firings/min) are
  recorded in reflex.py; the hook is documented there.
- Food/water production provider returns none until #34 lands. The
  reflex degrades gracefully: starving with no food known records the
  sensation "no food in sight" and emits nothing.
"""

from . import consume, deliberation, drives, pool, reflex, scheduler, sensations, vocab

__all__ = [
    "consume",
    "deliberation",
    "drives",
    "pool",
    "reflex",
    "scheduler",
    "sensations",
    "vocab",
]
