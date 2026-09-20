"""Deterministic adjudication support (issue #38).

The replay CLI (`server/replay.py`) re-runs intent adjudication through
the real tick-pump code and diffs the outcomes against the recorded
journal. For that to be bit-identical, every nondeterminism source on
the adjudication path must be controlled. This module is the single
place where that control lives:

* **Scenario seed** (`SOULSCAPE_SCENARIO_SEED`): the sim accepts one
  seed; every adjudication RNG derives from it. Sub-seeds are
  SHA-256 over ``seed + NUL + scope + NUL + intent_id``, taken as a
  big-endian int into `random.Random` -- an explicit, documented
  contract (`DERIVATION_VERSION`), stable across processes and
  immune to PYTHONHASHSEED. Per-intent derivation (not a shared
  stream) keeps draws independent of adjudication order.
* **Wall clock**: adjudication never calls the clock directly; it goes
  through `tick_now(tick)`. Live, that is `time.time()` (behavior
  unchanged). In replay, the tick carries a `ReplayContext` whose
  frozen time is the recorded journal `created_at` of the intent being
  re-adjudicated (record-and-replay for the clock).
* **Generated ids**: `tick_gen_id` restores the recorded id from the
  journal in replay (record-and-replay for values that cannot be
  seeded, e.g. `listing_id` / `message_id` minted with `secrets` at
  adjudication time); live, the existing `secrets` fallback runs.
* **Arrival order**: `intents.pending_intents()` is
  `ORDER BY created_at, rowid` -- recorded in the DB, replayed in
  journal order. No code change needed.

Nondeterminism audit (adjudication path only; the full tick loop --
agent pool, LLM quips, sweeps -- is intentionally out of replay scope):

* RNG: `affection.roll_escape` (carry escape rolls) -- seeded via
  `tick_rng(tick, "affection", intent_id)`; falls back to the module
  `_rng` when no scenario seed is configured (live behavior unchanged).
  `resources` node placement was already seeded (#34,
  `SOULSCAPE_RESOURCE_SEED`); expedition/agent-pool/quip randomness is
  sim-driven or ingest-time, never inside intent adjudication.
* Wall clock (`time.time()` inside adjudication): market ledger +
  listing timestamps, social message/ledger timestamps, plot
  claimed_at/ledger, biology ledger, metering settlement ledger +
  `settled_at`, resource `respawns_at` (already had a `now` param --
  the pump now passes the tick clock), presence record/return/unlock,
  affection pet cooldowns + carry sessions, bridge event `created_at`
  and its 1h recency window, dormancy flip payloads. All go through
  `tick_now(tick)` now.
* Ids minted at adjudication: market `listing_id` (only when the
  client did not supply one), social `message_id` -- record-and-replay
  via `tick_gen_id`. Every other id (intent, escrow, nonce, event) is
  minted at ingress and travels with the recorded intent row.
* Journal `created_at` and snapshot `created_at` remain wall clock:
  they are row metadata, not adjudication outcomes, and the replay
  diff deliberately ignores them (it diffs intent results).
"""

from __future__ import annotations

import hashlib
import os
import random
import time
from typing import Any, Callable

#: Env var carrying the scenario seed for the sim and the replay CLI.
SCENARIO_SEED_ENV = "SOULSCAPE_SCENARIO_SEED"

#: Version of the seed -> RNG derivation contract below. Recorded in
#: the snapshot header and the run_start journal event; a replay must
#: use the same derivation or seeded draws will differ.
DERIVATION_VERSION = "sha256-nuljoin-int-v1"


def current_seed(explicit: str | None = None) -> str | None:
    """Resolve the scenario seed: explicit arg wins, then the env var."""
    if explicit:
        return explicit
    return os.environ.get(SCENARIO_SEED_ENV) or None


def derive_rng(seed: str, scope: str, intent_id: str) -> random.Random:
    """Deterministic per-intent RNG.

    ``scope`` names the subsystem ("affection", ...); ``intent_id``
    scopes the stream to one adjudication so draws never depend on
    pump order. The sub-seed is SHA-256 over the NUL-joined parts,
    taken as a big-endian int: explicit and stable across processes,
    Python versions, and PYTHONHASHSEED values.
    """
    digest = hashlib.sha256(
        b"\x00".join(
            part.encode("utf-8") for part in (seed, scope, intent_id)
        )
    ).digest()
    return random.Random(int.from_bytes(digest, "big"))


def tick_now(tick: Any) -> float:
    """The adjudication clock: frozen replay time, else wall clock."""
    replay = getattr(tick, "replay", None)
    if replay is not None:
        now = replay.current_now()
        if now is not None:
            return now
    return time.time()


def tick_rng(tick: Any, scope: str, intent_id: str) -> random.Random | None:
    """Seeded RNG for one adjudication, or None when unseeded.

    None means "keep today's behavior" (the module-level unseeded
    RNG); callers treat None as the legacy default.
    """
    seed = getattr(tick, "scenario_seed", None) or current_seed()
    if not seed:
        return None
    return derive_rng(seed, scope, intent_id)


def tick_gen_id(
    tick: Any,
    intent_id: str,
    result_key: str,
    make: Callable[[], str],
) -> str:
    """Adjudication-time id with record-and-replay.

    In replay, the id recorded in the journal for this intent is
    restored so replayed rows land on the same keys; otherwise the
    live ``make()`` fallback (usually `secrets`-based) runs.
    """
    replay = getattr(tick, "replay", None)
    if replay is not None:
        recorded = replay.recorded_id(intent_id, result_key)
        if recorded is not None:
            return recorded
    return make()


class ReplayContext:
    """Per-replay deterministic overrides carried on the tick.

    ``recorded`` maps intent_id -> {"status", "result", "tick_id",
    "created_at", "seq"}. The replay loop calls `begin_intent` before
    each adjudication; `tick_now`/`tick_gen_id` consult it.
    """

    def __init__(
        self, seed: str | None, recorded: dict[str, dict[str, Any]]
    ) -> None:
        self.seed = seed
        self.recorded = recorded
        self._current_intent_id: str | None = None

    def begin_intent(self, intent_id: str) -> None:
        self._current_intent_id = intent_id

    def current_now(self) -> float | None:
        rec = self.recorded.get(self._current_intent_id or "")
        if rec is None:
            return None
        created_at = rec.get("created_at")
        return float(created_at) if created_at is not None else None

    def recorded_id(self, intent_id: str, result_key: str) -> str | None:
        rec = self.recorded.get(intent_id) or {}
        result = rec.get("result") or {}
        value = result.get(result_key)
        return value if isinstance(value, str) and value else None
