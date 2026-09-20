# ADR-0003: Extract SimProcess — the world survives API restarts

**Status:** Implemented (issue #37).
**Date:** 2026-09-17.
**Supersedes:** the "target state" note in `docs/architecture.md` §1 — the two-process split is now real.

## Context

The tick loop, intent adjudication, and every write to sim-owned tables ran
inside the API process. Any API restart (deploy, crash, OOM) stopped the
world: ticks halted, in-flight intents risked silent loss, and the "always
alive" pillar (§0) depended on one process never dying.

## Decision

Split the hub into two processes on one node:

- **sim** (`server/sim_process.py`) — owns the world. Runs the 5 Hz tick
  loop, adjudicates the durable intent queue, and is the *sole writer* of
  sim-owned tables (full inventory in `server/sim_commands.py`:
  `souls`, `soul_inventory`, `marketplace`, `escrows`, `ledger`,
  `messages`, `tamer_presence`, `intents`, `journal`, `snapshots`,
  `plots`, `resource_nodes`, `soul_home_plots`, `pet_cooldowns`,
  `mailbag`, `quip_budgets`, `metering_events`, `metering_config`,
  `decision_traces`, `expeditions`, `recaps`, `recap_sources`,
  `episodes`, `weekly_digests`, `semantic_memories`, `bridge_events`,
  plus `globals` keys it mutates).
- **api** (`server/main.py`) — stateless. Keeps auth, rate limits, tamer
  sessions, WS tickets, key vault, audit log, and LLM cognition; every
  sim-state mutation goes through the gateway. Ordinary reads use
  short-lived SQLite/WAL connections; world reads (positions, tick
  status) go over IPC.

### Protocol (`server/sim_ipc.py`)

Localhost TCP, default `127.0.0.1:9786`, env `SIM_HOST`/`SIM_PORT`.
Framing: 4-byte big-endian unsigned length + UTF-8 JSON (msgpack
deferred), 16 MiB max frame. Message types: `ping`, `intent_submit`,
`intent_status`, `command`, `query`, `shutdown`. Server side: one
`SimDispatcher` over the tick's step lock, bounded
`ThreadPoolExecutor(max_workers=8)` accept pool. Refusals carry
machine-readable `reason` + `detail` (and optional `ws_code`).

### Gateway (`server/sim_gateway.py`)

`SimGateway` is the API's only path to sim-owned state: IPC backend in
production (`SOULSCAPE_SIM_MODE` unset), in-process backend
(`SOULSCAPE_SIM_MODE=inprocess`) for tests — same dispatcher, same
messages, no sockets.

### Read-path split

- Hot world reads (`positions`, `tick_status`) → IPC queries, served
  under the tick's step lock for a consistent snapshot.
- Everything else → direct SQLite/WAL reads from the API (short-lived
  connections). WAL gives concurrent readers against the sim's writes.
- API-owned tables (`tamers`, `tamer_sessions`, `ws_sessions`,
  `ws_tickets`, `llm_keys`, `audit_log`, `rate_limits`,
  `bridge_tokens`) stay API-local.

### Failure behavior — fail loud, never lose

- Sim unreachable → mutations return clear **503** (`SimUnreachable`);
  no silent intent loss, no partial writes.
- **Commit-before-ack**: intents are durably persisted before the ACK;
  sim crash between commit and ACK is recovered at boot by re-pumping
  pending intents (issue #16 recovery suite still green).
- Retries reuse the same nonce / `Idempotency-Key`; duplicate submits
  return the original record.

### Deployment

Root `docker-compose.yml`: `sim` + `api` services, one persistent
`SOULSCAPE_DB_PATH` on the shared `soulscape-data` volume, `api`
`depends_on` sim `service_healthy`. Health: sim answers IPC `ping`
with `tick_running`; api `/health` reports DB state, sim reachability,
latency, and tick id. `server/Dockerfile` copies the full `server/`
and `shared/` trees (an earlier revision copied an obsolete subset).

### Verification

- Two-process integration test: intent submitted immediately before
  `SIGKILL`ing the API; sim tick kept advancing with the API dead; API
  restarted; intent durably adjudicated, never lost
  (`test_sim_survives_api_restart.py`).
- Tick p99 under concurrent IPC load: 500 souls, 4 simulated API
  clients (5 Hz position reads + ~1 intent/s each): p50 15.6 ms,
  p99 49.7 ms vs 200 ms budget (< 50%).
- Offline client mode untouched; all HTTP endpoints unchanged.

## Consequences

- The API is horizontally disposable; only the sim is stateful.
- IPC adds ~1 ms per call; tick step lock serializes commands/queries
  with stepping — measured headroom is comfortable (see above).
- Debugging now spans two processes; `/debug/tick` reports the sim's
  snapshot.

## Scaling

Single node, two processes is the deployment until the tick itself
becomes the bottleneck. The trigger is explicit and unchanged:
**revisit sharding on sustained ticks-behind or third-Tamer onboarding.**
