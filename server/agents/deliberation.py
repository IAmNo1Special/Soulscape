"""LLM deliberation tier (issue #25).

Event-driven metered deliberation atop the #24 reflex layer.
Escalation triggers promote a scheduled reflex think into a
deliberation: one token-capped prompt, a flash/pro tier route, a
provider fallback chain, and structured-intent JSON outputs that go
through the same validation + execution-time revalidation as reflex
intents before anything is enqueued.

Escalation triggers (arch spec):
  reflex_rate    >=3 reflex firings in the trailing 60 s (reflex.py)
  detail_enter   an entity appears in #20 detail vision (observation
                 diff, fed by every reflex think and every deliberation)
  converse_turn  incoming converse turn (stub: no converse sessions in
                 v1; note_converse_turn is the trigger point)
  tamer_order    tamer-issued order (minimal channel: issue_order; the
                 order text rides the prompt as a high-priority block)
  wallet_delta   |essence delta| > 10% between wallet sightings. The
                 sighting happens on every think, so ALL ledger
                 appliers (market, social, plots, biology, dormancy)
                 feed it without per-applier hooks.
  tamer_return   tamer returns after >30 min absence (stub: presence is
                 #28; note_tamer_return is the trigger point)
  idle           5-minute awake-idle floor: an awake soul with no
                 escalation is eligible for deliberation at most every
                 5 min, and only when pool budget allows (lowest
                 priority, capped per tick). Reading: the floor is a
                 ceiling on idle thinking -- idle souls think rarely.

Tier routing (documented rule):
  - flash by default (cheap/fast).
  - pro when the escalation is high-stakes: tamer_order,
    tamer_return, or wallet_delta with magnitude >= 50%.
  - pro when flash output failed validation twice in a row for the
    soul (counter resets on any successful deliberation).

Token-capped prompt assembly (<=800 tokens):
  identity (cached per soul, never truncated) + optional tamer order
  + long-term memory (#26: restart-wake block once per boot, then
  per-query semantic memories + working notes) + working memory
  (recent sensations, newest first) + observation (drives, wallet,
  nearest detail entities) + priced action menu. Semantic retrieval
  is agents/memory.py's metadata-first store (the old documented
  stub now delegates to it).
  Token counting is estimate_tokens(): ceil(chars/4). The estimator
  is conservative for English prose (~4.7 chars/token on cl100k-like
  tokenizers) and assembly targets a 760-token soft budget, leaving a
  40-token margin for denser tokenizers (JSON/code ~3.5 chars/token).
  Truncation priority (kept first): identity > order > action menu >
  working memory > observations. assemble_prompt asserts the measured
  total is <= 800.

Structured outputs: the prompt demands JSON
{"intents": [{"action", "params"} x 1-3], "rationale": str}.
parse_output is defensive (fences, leading/trailing noise); every
intent is validated against the closed vocabulary and payload-
canonicalized exactly like #24's pool path. Malformed/illegal/empty/
>3-intent outputs count as failed attempts: they feed the flash->pro
promotion counter and the fallback chain.

Provider fallback chain: tamer's preferred provider+model -> other
providers with active vault keys (key_vault.list_key_metadata) ->
deterministic heuristic (the pool runs the #24 reflex think instead).
Any exception/timeout (PROVIDER_TIMEOUT_S = 30 s) moves to the next
link. Keys are touched ONLY via key_vault.use_key(); plaintext never
reaches logs (redaction filter is the backstop). All provider I/O
goes through the injectable _call boundary; tests inject fakes and
never touch the network.

Metering: every deliberation (including heuristic fallbacks) writes
one llm_usage row {soul_id, tier, provider, model, prompt_tokens,
completion_tokens, estimated_cost_usd, latency_ms, fallback_used}
for #27 to consume. Cost comes from MODEL_COSTS (documented
estimates per 1K tokens). The rationale goes to the journal
(llm_deliberation); degradations are journaled (llm_degraded).

The world never crashes: deliberator.deliberate contains provider
errors, pool.deliberate contains everything else and drops to the
reflex think as the ultimate fallback.
"""

import json
import logging
import math
import random
import re
import time
import urllib.request

from .. import database, key_vault, persistence, plots
from . import drives, memory, metering, reflex, scheduler, sensations, vocab
from . import pool as agent_pool

logger = logging.getLogger("soulscape_hub")

#: Hard cap on assembled prompt size, in estimated tokens.
PROMPT_TOKEN_CAP = 800

#: Soft assembly budget: the 40-token margin covers estimator error
#: on denser tokenizers (JSON/code ~3.5 chars/token).
PROMPT_SOFT_BUDGET = 760

#: Per-attempt provider timeout (seconds).
PROVIDER_TIMEOUT_S = 30.0

#: Idle floor: an awake soul deliberates at most this often without
#: an escalation, and only when pool budget allows.
IDLE_FLOOR_S = 300.0

#: Idle deliberations per tick (lowest priority; escalated souls win).
IDLE_DELIBERATIONS_PER_TICK = 1

#: |essence delta| fraction that escalates a deliberation.
WALLET_DELTA_TRIGGER = 0.10

#: Wallet delta fraction that routes straight to the pro tier.
WALLET_DELTA_PRO = 0.50

#: Absence after which a tamer's return escalates (presence is #28).
TAMER_RETURN_ABSENCE_S = 1800.0

#: Consecutive flash validation failures before pro promotion.
FLASH_FAIL_PROMOTION = 2

#: Max intents per deliberation output.
MAX_INTENTS = 3

#: Journal event types.
EVENT_LLM_DELIBERATION = "llm_deliberation"
EVENT_LLM_DEGRADED = "llm_degraded"

#: Provider attempt order (tamer preference jumps the queue).
PROVIDER_ORDER = ("google", "openai", "anthropic")

#: Tier models per provider. Config defaults, documented; override by
#: editing TIER_MODELS (no keys anywhere near here).
TIER_MODELS = {
    "google": {"flash": "gemini-2.5-flash", "pro": "gemini-2.5-pro"},
    "openai": {"flash": "gpt-5-mini", "pro": "gpt-5"},
    "anthropic": {"flash": "claude-haiku-4-5", "pro": "claude-sonnet-4-5"},
}

#: (input, output) USD per 1K tokens. Documented estimates, not quotes.
MODEL_COSTS = {
    "gemini-2.5-flash": (0.00030, 0.00250),
    "gemini-2.5-pro": (0.00125, 0.01000),
    "gpt-5-mini": (0.00025, 0.00200),
    "gpt-5": (0.00125, 0.01000),
    "claude-haiku-4-5": (0.00100, 0.00500),
    "claude-sonnet-4-5": (0.00300, 0.01500),
}

#: Escalations that route straight to the pro tier.
_HIGH_STAKES = ("tamer_order", "tamer_return")

#: One-shot flag priority when several fire at once.
_FLAG_PRIORITY = (
    "tamer_order",
    "tamer_return",
    "wallet_delta",
    "converse_turn",
    "detail_enter",
)

#: Max entity ids remembered per soul for detail-enter diffing.
_SEEN_ENTITIES_CAP = 64


class DeliberationFailure(Exception):
    """A provider attempt produced no usable output."""


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: ceil(chars / 4).

    English prose averages ~4.7 chars/token on cl100k-like tokenizers,
    so chars/4 overestimates slightly -- the safe direction for a cap.
    JSON/code can run denser (~3.5); the 40-token soft-budget margin in
    assemble_prompt covers that. Measured, not guessed, in tests.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD estimate from MODEL_COSTS; 0.0 for unknown models."""
    rates = MODEL_COSTS.get(model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    return round(
        prompt_tokens / 1000.0 * in_rate + completion_tokens / 1000.0 * out_rate,
        6,
    )


_identity_cache: dict[str, tuple[str, str]] = {}


def _identity_fingerprint(row: dict) -> str:
    parts = [
        str(row.get("name") or ""),
        str(row.get("first_name") or ""),
        str(row.get("family_name") or ""),
        str(row.get("species") or ""),
        str(row.get("nature") or ""),
        str(row.get("level") or ""),
    ]
    return "|".join(parts)


def identity_block(soul_id: str, row: dict) -> str:
    """Cached identity block; rebuilt only when identity fields change."""
    fingerprint = _identity_fingerprint(row)
    cached = _identity_cache.get(soul_id)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    name = (
        str(row.get("name") or "").strip()
        or " ".join(
            p
            for p in (
                str(row.get("first_name") or "").strip(),
                str(row.get("family_name") or "").strip(),
            )
            if p
        )
        or soul_id
    )
    species = str(row.get("species") or "unknown species")
    nature = str(row.get("nature") or "unknown nature")
    level = row.get("level")
    block = (
        f"You are {name}, a {species} soul"
        f" (nature: {nature}"
        f"{', level ' + str(level) if level else ''}). "
        "You think in intents from your action menu. "
        "Be terse, stay in character, never break the fourth wall."
    )
    _identity_cache[soul_id] = (fingerprint, block)
    return block


def clear_identity_cache(soul_id: str | None = None) -> None:
    if soul_id is None:
        _identity_cache.clear()
    else:
        _identity_cache.pop(soul_id, None)


def retrieve_semantic(soul_id: str, query: str = "") -> list[str]:
    """Semantic memory retrieval (#26).

    Was a documented stub; now delegates to the SQLite-backed
    semantic store in agents/memory.py: metadata pre-filter
    (soul, 30d recency, salience >= 0.3), then combined
    cosine/salience/recency/kind ranking. Returns top-k texts.
    """
    return memory.retrieve_semantic(soul_id, query)


def action_menu() -> list[tuple[str, str]]:
    """Priced action menu: (action, price text).

    Real costs: post/reply from the pool's revalidation gate;
    claim_plot from plots.BASE_CLAIM_FEE (ring-scaled at adjudication).
    Everything else is free in v0.
    """
    paid = dict(agent_pool._PAID_ACTIONS)
    claim = f"{plots.BASE_CLAIM_FEE:.0f}e x (ring+1)"
    menu = []
    for action in sorted(vocab.LEGAL_ACTIONS):
        if action == "claim_plot":
            menu.append((action, claim))
        elif action in paid:
            menu.append((action, f"{paid[action]:.0f}e"))
        else:
            menu.append((action, "free"))
    return menu


def _format_observations(observations: list[dict], limit: int) -> str:
    lines = []
    for obs in sorted(observations, key=lambda o: float(o.get("distance", 1e9)))[
        :limit
    ]:
        kind = str(obs.get("kind", "?"))[:24]
        dist = float(obs.get("distance", 0.0))
        lines.append(f"- {kind} {dist:.0f}wu")
    return "\n".join(lines)


def _format_memory(sens_list: list[dict], limit: int) -> str:
    lines = []
    for sens in sens_list[-limit:] if limit else []:
        text = str(sens.get("text", ""))[:120]
        cause = str(sens.get("cause", ""))[:40]
        lines.append(f"- [{cause}] {text}")
    return "\n".join(lines)


def assemble_prompt(
    soul_id: str,
    row: dict,
    *,
    sensations_list: list[dict] | None = None,
    observations: list[dict] | None = None,
    drive_vec: dict[str, float] | None = None,
    tamer_order: str | None = None,
    memory_block: str | None = None,
) -> tuple[str, dict]:
    """Assemble the one-shot prompt, measured, within the token cap.

    Returns (prompt, report). report carries prompt_tokens, the
    per-section token counts, and what got truncated. memory_block is
    the pre-composed LONG-TERM MEMORY section (#26: restart-wake
    block + semantic memories + working notes), or None. Truncation
    order: observations (farthest first), then working memory (oldest
    first), then the long-term memory block; identity, order, and the
    action menu are never truncated. The measured total is asserted
    <= PROMPT_TOKEN_CAP.
    """
    sensations_list = sensations_list or []
    observations = observations or []
    drive_vec = drive_vec or {}
    identity = identity_block(soul_id, row)
    order_block = ""
    if tamer_order:
        order_block = f"TAMER ORDER: {tamer_order[:280]}"
    menu_lines = [f"- {action} ({price})" for action, price in action_menu()]
    menu_block = "ACTION MENU (essence cost):\n" + "\n".join(menu_lines)

    def _body(n_sens: int, n_obs: int, keep_memory: bool) -> str:
        drives_txt = ", ".join(f"{k}={v:.2f}" for k, v in sorted(drive_vec.items()))
        obs_block = (
            "OBSERVATION:\n"
            f"essence={float(row.get('essence') or 0.0):.0f} "
            f"satiety={float(row.get('satiety') or 0.0):.0f} "
            f"hydration={float(row.get('hydration') or 0.0):.0f} "
            f"hp={float(row.get('hp') or 0.0):.0f}\n"
            f"drives: {drives_txt}\n"
            f"entities seen:\n{_format_observations(observations, n_obs)}"
        )
        working_block = "WORKING MEMORY (recent sensations):\n" + _format_memory(
            sensations_list, n_sens
        )
        sections = [identity]
        if order_block:
            sections.append(order_block)
        if keep_memory and memory_block:
            sections.append("LONG-TERM MEMORY:\n" + memory_block[:1600])
        sections += [working_block, obs_block, menu_block]
        contract = (
            "Respond with JSON ONLY, no prose: "
            '{"intents": [{"action": "<menu action>", '
            '"params": {}}], "rationale": "<one sentence>"}. '
            f"1 to {MAX_INTENTS} intents, every action from the menu, "
            "params as the action needs."
        )
        return "\n\n".join(sections) + "\n\n" + contract

    n_sens, n_obs = 6, 12
    keep_memory = True
    prompt = _body(n_sens, n_obs, keep_memory)
    truncated = {"sensations": 0, "observations": 0, "memory": 0}
    while estimate_tokens(prompt) > PROMPT_SOFT_BUDGET and (
        n_obs > 0 or n_sens > 0 or keep_memory
    ):
        if n_obs > 0:
            n_obs = max(0, n_obs - 4)
            truncated["observations"] += 1
        elif n_sens > 0:
            n_sens = max(0, n_sens - 2)
            truncated["sensations"] += 1
        elif keep_memory:
            keep_memory = False
            truncated["memory"] += 1
        prompt = _body(n_sens, n_obs, keep_memory)
    measured = estimate_tokens(prompt)
    assert measured <= PROMPT_TOKEN_CAP, (
        f"prompt over cap: {measured} > {PROMPT_TOKEN_CAP}"
    )
    report = {
        "prompt_tokens": measured,
        "identity_tokens": estimate_tokens(identity),
        "menu_tokens": estimate_tokens(menu_block),
        "n_sensations": n_sens,
        "n_observations": n_obs,
        "memory_block": bool(memory_block),
        "memory_dropped": bool(memory_block) and not keep_memory,
        "truncated": truncated,
    }
    return prompt, report


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def parse_output(raw: str) -> tuple[list[tuple[str, dict]], str]:
    """Parse the structured output. Raises DeliberationFailure."""
    text = (raw or "").strip()
    if not text:
        raise DeliberationFailure("empty_response")
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise DeliberationFailure("no_json")
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise DeliberationFailure(f"bad_json: {exc}") from exc
    if not isinstance(data, dict):
        raise DeliberationFailure("not_an_object")
    intents = data.get("intents")
    if not isinstance(intents, list) or not 1 <= len(intents) <= MAX_INTENTS:
        raise DeliberationFailure(
            f"intent_count_out_of_range: "
            f"{len(intents) if isinstance(intents, list) else type(intents)}"
        )
    parsed: list[tuple[str, dict]] = []
    for item in intents:
        if not isinstance(item, dict):
            raise DeliberationFailure("intent_not_an_object")
        action = item.get("action")
        params = item.get("params", {})
        if not isinstance(action, str) or not action.strip():
            raise DeliberationFailure("intent_missing_action")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise DeliberationFailure("intent_params_not_an_object")
        parsed.append((action, params))
    rationale = data.get("rationale", "")
    rationale = str(rationale)[:2000] if rationale is not None else ""
    return parsed, rationale


class EscalationTracker:
    """One-shot escalation flags + rate conditions per soul.

    note_* methods set flags (and pull the think scheduler forward so
    the escalation promotes thinking promptly). should_escalate
    consumes flags and checks the reflex-rate condition. Dormant souls
    never escalate: callers filter them, and deliberate() guards again.
    """

    def __init__(self, seed: int | None = None) -> None:
        self._flags: dict[str, dict[str, dict]] = {}
        self._essence: dict[str, float] = {}
        self._wallet_mag: dict[str, float] = {}
        self._seen: dict[str, dict[str, None]] = {}
        self._orders: dict[str, str] = {}
        self._last_deliberated: dict[str, float] = {}
        self._scheduler: scheduler.ThinkScheduler | None = None
        self._rng = random.Random(seed)

    def bind_scheduler(self, sched: scheduler.ThinkScheduler) -> None:
        self._scheduler = sched

    def _pull(self, soul_id: str, now: float) -> None:
        if self._scheduler is not None:
            self._scheduler.pull_forward(soul_id, now)

    def _flag(self, soul_id: str, reason: str, now: float, **extra) -> None:
        entry = {"at": now, **extra}
        self._flags.setdefault(soul_id, {})[reason] = entry
        self._pull(soul_id, now)

    def note_converse_turn(self, soul_id: str, now: float) -> None:
        """Stub trigger point: incoming converse turn (#30 sessions)."""
        self._flag(soul_id, "converse_turn", now)

    def issue_order(self, soul_id: str, text: str, now: float) -> None:
        """Minimal tamer-order channel: records the order, escalates."""
        self._orders[soul_id] = str(text or "")[:280]
        self._flag(soul_id, "tamer_order", now)

    def pop_order(self, soul_id: str) -> str | None:
        return self._orders.pop(soul_id, None)

    def note_tamer_return(self, soul_id: str, now: float) -> None:
        """Stub trigger point: tamer back after >30 min (#28 presence)."""
        self._flag(soul_id, "tamer_return", now)

    def note_wallet(self, soul_id: str, essence: float, now: float) -> float:
        """Record a wallet sighting; flag when |delta| > 10%.

        Called on every think, so every ledger applier feeds it.
        Returns the signed fractional delta (0.0 on first sighting).
        """
        before = self._essence.get(soul_id)
        self._essence[soul_id] = essence
        if before is None or before <= 0:
            return 0.0
        delta = (essence - before) / before
        mag = abs(delta)
        self._wallet_mag[soul_id] = mag
        if mag > WALLET_DELTA_TRIGGER:
            self._flag(soul_id, "wallet_delta", now, magnitude=round(mag, 4))
        return delta

    def last_wallet_delta(self, soul_id: str) -> float:
        return self._wallet_mag.get(soul_id, 0.0)

    def check_detail_enters(
        self, soul_id: str, observations: list[dict], now: float
    ) -> list[str]:
        """Diff detail-vision entities; new ids escalate (#20 event)."""
        seen = self._seen.setdefault(soul_id, {})
        entered = []
        for obs in observations or []:
            eid = str(obs.get("id", ""))
            if not eid or eid in seen:
                continue
            seen[eid] = None
            entered.append(eid)
        while len(seen) > _SEEN_ENTITIES_CAP:
            seen.pop(next(iter(seen)))
        if entered:
            self._flag(soul_id, "detail_enter", now, count=len(entered))
        return entered

    def should_escalate(self, soul_id: str, now: float) -> str | None:
        """Highest-priority pending escalation, consuming one-shot flags."""
        flags = self._flags.pop(soul_id, {})
        for reason in _FLAG_PRIORITY:
            if reason in flags:
                return reason
        if (
            reflex.firing_rate(soul_id, window_s=60.0, now=now)
            >= reflex.ESCALATION_FIRINGS_PER_MIN
        ):
            return "reflex_rate"
        return None

    def idle_eligible(self, soul_id: str, now: float) -> bool:
        """True when the soul may take its 5-minute idle deliberation."""
        return now - self._last_deliberated.get(soul_id, 0.0) >= IDLE_FLOOR_S

    def mark_deliberated(self, soul_id: str, now: float) -> None:
        self._last_deliberated[soul_id] = now

    def seed_stagger(self, soul_ids: list[str], now: float) -> None:
        """Stagger idle eligibility at boot (no thundering herd)."""
        for sid in soul_ids:
            if sid not in self._last_deliberated:
                self._last_deliberated[sid] = now - self._rng.uniform(0.0, IDLE_FLOOR_S)

    def forget(self, soul_id: str) -> None:
        for store in (
            self._flags,
            self._essence,
            self._wallet_mag,
            self._seen,
            self._orders,
            self._last_deliberated,
        ):
            store.pop(soul_id, None)


_tracker: EscalationTracker | None = None


def tracker() -> EscalationTracker:
    """Process-global escalation tracker (bound to the tick scheduler
    by world_tick)."""
    global _tracker
    if _tracker is None:
        _tracker = EscalationTracker()
    return _tracker


def reset_tracker() -> None:
    global _tracker
    _tracker = None


def record_usage(
    soul_id: str,
    tier: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    estimated_cost_usd: float,
    latency_ms: float,
    fallback_used: bool,
) -> int:
    """One llm_usage row per deliberation (#27 consumes this table)."""
    with database.get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO llm_usage "
            "(soul_id, tier, provider, model, prompt_tokens, "
            "completion_tokens, estimated_cost_usd, latency_ms, "
            "fallback_used, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                soul_id,
                tier,
                provider,
                model,
                int(prompt_tokens),
                int(completion_tokens),
                float(estimated_cost_usd),
                float(latency_ms),
                1 if fallback_used else 0,
                time.time(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _journal(tick_id: int, event_type: str, payload: dict, conn=None) -> int:
    if conn is None:
        with database.get_db() as owned:
            seq = persistence.append_event(owned, tick_id, event_type, payload)
            owned.commit()
            return seq
    return persistence.append_event(conn, tick_id, event_type, payload)


def default_provider_call(
    provider: str,
    model: str,
    prompt: str,
    timeout_s: float,
    key: bytearray,
) -> str:
    """Production HTTP boundary (urllib, stdlib only). Never called in
    tests: every test injects a fake. Raises DeliberationFailure on
    transport errors and non-2xx responses. Key material is decoded
    only here, never logged, never placed in exceptions.
    """
    key_str = bytes(key).decode("utf-8", errors="strict")
    try:
        if provider == "google":
            url = (
                "https://generativelanguage.googleapis.com/v1beta/"
                f"models/{model}:generateContent"
            )
            body = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            }
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": key_str,
            }
        elif provider == "openai":
            url = "https://api.openai.com/v1/chat/completions"
            body = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key_str,
            }
        elif provider == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            body = {
                "model": model,
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
            }
            headers = {
                "Content-Type": "application/json",
                "x-api-key": key_str,
                "anthropic-version": "2023-06-01",
            }
        else:
            raise DeliberationFailure(f"unknown_provider:{provider}")
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except DeliberationFailure:
        raise
    except Exception as exc:
        raise DeliberationFailure(f"provider_call_failed:{type(exc).__name__}") from exc
    finally:
        key_str = ""
    try:
        if provider == "google":
            return payload["candidates"][0]["content"]["parts"][0]["text"]
        if provider == "openai":
            return payload["choices"][0]["message"]["content"]
        return payload["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeliberationFailure("bad_provider_envelope") from exc


def _global_tracker() -> EscalationTracker:
    return tracker()


class Deliberator:
    """Runs one deliberation: route tier, walk the provider chain,
    parse + validate, record metering, journal the trace.

    The heuristic fallback returns status "heuristic" with no intents;
    the pool then runs the #24 reflex think instead, so the soul still
    acts and nothing ever raises out of here except DeliberationCrash
    (defense-in-depth; the pool contains even that).
    """

    def __init__(
        self,
        pool,
        tracker: EscalationTracker | None = None,
        call_provider=None,
        timeout_s: float = PROVIDER_TIMEOUT_S,
    ) -> None:
        self.pool = pool
        self.tracker = tracker if tracker is not None else _global_tracker()
        self._call = call_provider or default_provider_call
        self.timeout_s = timeout_s
        self._flash_failures: dict[str, int] = {}
        self._prefs: dict[str, str] = {}

    def set_provider_preference(self, tamer_id: str, provider: str) -> None:
        if provider not in PROVIDER_ORDER:
            raise ValueError(f"unknown provider: {provider}")
        self._prefs[tamer_id] = provider

    def _route_tier(self, soul_id: str, escalation: str | None) -> str:
        if escalation in _HIGH_STAKES:
            return "pro"
        if (
            escalation == "wallet_delta"
            and self.tracker.last_wallet_delta(soul_id) >= WALLET_DELTA_PRO
        ):
            return "pro"
        if self._flash_failures.get(soul_id, 0) >= FLASH_FAIL_PROMOTION:
            return "pro"
        return "flash"

    def _chain(self, tamer_id: str) -> list[str]:
        try:
            metas = key_vault.list_key_metadata(tamer_id)
        except key_vault.KeyVaultError:
            return []
        have = [
            m["provider"]
            for m in metas
            if m.get("revoked_at") is None and m["provider"] in PROVIDER_ORDER
        ]
        ordered = [p for p in PROVIDER_ORDER if p in have]
        pref = self._prefs.get(tamer_id)
        if pref in ordered:
            ordered.remove(pref)
            ordered.insert(0, pref)
        return ordered

    def _note_validation_failure(self, soul_id: str, tier: str) -> None:
        if tier == "flash":
            self._flash_failures[soul_id] = min(
                self._flash_failures.get(soul_id, 0) + 1, FLASH_FAIL_PROMOTION
            )

    def deliberate(
        self,
        soul_id: str,
        row: dict,
        vision,
        fw_provider,
        now: float,
        escalation: str | None,
        tick_id: int = 0,
    ) -> dict:
        """One deliberation. Never raises for provider/model problems;
        those walk the chain and end at the heuristic fallback."""
        tamer_id = row.get("owner_id") or row.get("custodian_id") or ""
        tier = self._route_tier(soul_id, escalation)
        observations = vision.detail_observations(soul_id)
        self.tracker.check_detail_enters(soul_id, observations, now)
        drive_vec = drives.compute_drives(
            row.get("nature"),
            row.get("satiety"),
            row.get("hydration"),
            row.get("hp"),
            row.get("max_hp"),
            observations,
            row.get("loyalty"),
        )
        # #26: per-query semantic retrieval + once-per-boot restart
        # wake, composed into the LONG-TERM MEMORY prompt section.
        order = self.tracker.pop_order(soul_id)
        sens_list = sensations.recent(soul_id)
        query = " ".join(
            part
            for part in (
                order or "",
                " ".join(s.get("text", "") for s in sens_list[-3:]),
            )
            if part
        ).strip()
        mem_block = memory.memory_block_for_prompt(soul_id, query)
        prompt, report = assemble_prompt(
            soul_id,
            row,
            sensations_list=sens_list,
            observations=observations,
            drive_vec=drive_vec,
            tamer_order=order,
            memory_block=mem_block or None,
        )
        prompt_tokens = report["prompt_tokens"]
        chain = self._chain(tamer_id)
        tried: list[str] = []
        last_error = "no_provider_keys" if not chain else ""
        for provider in chain:
            model = TIER_MODELS[provider][tier]
            try:
                with key_vault.use_key(tamer_id, provider) as key:
                    start = time.monotonic()
                    raw = self._call(provider, model, prompt, self.timeout_s, key)
                    latency_ms = (time.monotonic() - start) * 1000.0
            except key_vault.KeyNotFound:
                continue
            except Exception as exc:
                tried.append(provider)
                last_error = f"{type(exc).__name__}"
                continue
            tried.append(provider)
            try:
                parsed, rationale = parse_output(raw)
                validated: list[tuple[str, dict]] = []
                for action, params in parsed:
                    name, ok = vocab.validate(action)
                    if not ok:
                        raise DeliberationFailure(f"illegal_action:{action}")
                    canonical = agent_pool.validate_agent_payload(name, params)
                    if canonical is None:
                        raise DeliberationFailure(f"bad_params:{name}")
                    validated.append((name, canonical))
                if not validated:
                    raise DeliberationFailure("empty_intents")
            except DeliberationFailure as exc:
                self._note_validation_failure(soul_id, tier)
                last_error = str(exc)[:120]
                continue
            self._flash_failures.pop(soul_id, None)
            completion_tokens = estimate_tokens(raw)
            cost = estimate_cost(model, prompt_tokens, completion_tokens)
            first = chain[0] if chain else provider
            usage_id = record_usage(
                soul_id,
                tier,
                provider,
                model,
                prompt_tokens,
                completion_tokens,
                cost,
                latency_ms,
                fallback_used=provider != first,
            )
            # #27: one idempotent metering event per usage row, one
            # decision trace per deliberation (rationale + intents;
            # intent ids and outcomes join later). Event + trace share
            # one transaction so a crash can never leave an event
            # without its trace.
            seq = _journal(
                tick_id,
                EVENT_LLM_DELIBERATION,
                {
                    "soul_id": soul_id,
                    "tier": tier,
                    "provider": provider,
                    "model": model,
                    "escalation": escalation,
                    "intents": [a for a, _ in validated],
                    "rationale": rationale[:1000],
                    "prompt_tokens": prompt_tokens,
                    "vocab_version": vocab.VOCAB_VERSION,
                },
            )
            with database.get_db() as mconn:
                event_id = metering.record_event_for_usage(usage_id, mconn)
                trace_id = metering.record_decision_trace(
                    mconn,
                    trace_id=f"{metering.TRACE_ID_PREFIX}{usage_id}",
                    soul_id=soul_id,
                    deliberation_id=seq,
                    usage_event_id=event_id,
                    status="deliberated",
                    rationale=rationale,
                    intents=[
                        {"action": action, "params": params}
                        for action, params in validated
                    ],
                )
                mconn.commit()
            self.tracker.mark_deliberated(soul_id, now)
            return {
                "status": "deliberated",
                "tier": tier,
                "provider": provider,
                "model": model,
                "intents": validated,
                "rationale": rationale,
                "fallback_used": provider != first,
                "prompt_tokens": prompt_tokens,
                "trace_id": trace_id,
            }
        reason = (last_error or "chain_exhausted")[:200]
        degraded_seq = _journal(
            tick_id,
            EVENT_LLM_DEGRADED,
            {
                "soul_id": soul_id,
                "tier": tier,
                "escalation": escalation,
                "reason": reason,
                "tried": tried,
                "fallback": "heuristic_reflex",
            },
        )
        usage_id = record_usage(
            soul_id,
            "heuristic",
            "heuristic",
            "heuristic",
            0,
            0,
            0.0,
            0.0,
            fallback_used=True,
        )
        # Event + trace share one transaction (same crash-safety
        # reasoning as the deliberated path above).
        with database.get_db() as mconn:
            event_id = metering.record_event_for_usage(usage_id, mconn)
            trace_id = metering.record_decision_trace(
                mconn,
                trace_id=f"{metering.TRACE_ID_PREFIX}{usage_id}",
                soul_id=soul_id,
                deliberation_id=degraded_seq,
                usage_event_id=event_id,
                status="heuristic",
                rationale=reason,
                intents=[],
            )
            mconn.commit()
        self.tracker.mark_deliberated(soul_id, now)
        return {
            "status": "heuristic",
            "tier": "heuristic",
            "intents": [],
            "fallback_used": True,
            "degraded_reason": reason,
            "trace_id": trace_id,
        }
