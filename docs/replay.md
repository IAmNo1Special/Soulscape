# Deterministic replay (issue #38)

Forensic re-adjudication for dispute investigations. Re-runs a journal
window's intents through the **real** tick-pump adjudication
(`WorldTick._adjudicate_one` — the same function the live pump calls),
in journal order, against a temp DB rebuilt from a snapshot. Diffs
recorded vs replayed outcomes per intent.

## CLI

```bash
uv run --package server python -m server.replay --snapshot <id|path> \
    --journal-tail <lo-hi|last:N|since:SEQ> \
    [--seed S] [--diff-against recorded] [--json] [--db PATH]
```

- `--snapshot`: snapshot id from the live DB, or a path to a snapshot
  JSON file (zlib blob or raw JSON). Must be v2 (full table dump);
  v1 kinematics-only snapshots are refused.
- `--journal-tail`: which journal seqs to replay. Default: everything
  after the snapshot's `journal_seq`.
- `--seed`: scenario seed override. Default: the snapshot header's
  `scenario_seed`, else the latest `run_start` journal event, else
  `SOULSCAPE_SCENARIO_SEED`.
- `--diff-against recorded`: reserved; the default mode already diffs
  against recorded outcomes.
- `--json`: machine-readable report on stdout.

Exit **0** = replay identical. Exit **1** = divergent (or usage
failure). Human output lists per-intent OK/DIFF lines plus an outcome
hash; JSON includes per-intent records, field-level diffs, operator
interventions, and live-DB untouched evidence.

An explicit `--journal-tail lo-hi` starting after the snapshot replays
the skipped post-snapshot intents as a **warm-up prefix** (for state,
not diffed) so the window still starts from the true pre-window
state. If the warm-up itself diverges, the report flags
`prefix_divergent` and the run fails — the window replay cannot be
trusted.

## Snapshots (v2)

`persistence.take_snapshot` writes format v2: header
(`scenario_seed`, `derivation`, tick id, journal seq) plus a full dump
of every adjudication-relevant table (`SNAPSHOT_TABLES`). The sim also
journals a `run_start` header at boot (seed, derivation contract,
tick_dt; idempotent). The replay resolves its seed from the snapshot
header and warns if the snapshot's derivation differs from the running
code's — seeded draws would then diverge by construction.

## Seed / sub-seed contract

One scenario seed (`SOULSCAPE_SCENARIO_SEED`, or explicit per-run).
Per-intent RNG: `derive_rng(seed, scope, intent_id)` =
`random.Random(int.from_bytes(sha256(seed NUL scope NUL intent_id)))`.
Contract version: `sha256-nuljoin-int-v1` (in `DERIVATION_VERSION`,
snapshot header, `run_start`). Per-intent streams keep draws
independent of pump order. Wall clock and minted ids are
record-and-replay: the frozen clock is the outcome event's journal
`created_at`; minted ids (`listing_id`, `message_id`) are restored
from the recorded result (`tick_gen_id`).

## What the window covers

An intent is replayed when its adjudication outcome appears in the
selected journal range as one of `ADJUDICATION_OUTCOME_TYPES`:
`intent_adjudicated`, `intent_rejected`, `soul_fed` (biology),
`metering_debit_settled`, `tool.event` (bridge; carries `intent_id`
since #38). Operator interventions (`operator.*` journal rows, e.g.
`operator.plot_policy`) are **shown, not re-applied** — they explain
"why did X happen".

Typed `operator.*` events exist only for direct operator DB writes
that bypass intent adjudication (`plot_policy`, `pricing_update`,
`metering_settle`). Operator actions that flow through intents
(marketplace list-override, cancel) need no separate event: the
intent itself is journaled and replayed.

## Dispute workflow (metering, #27)

`agents/metering.soul_line_items()` attaches a `replay` handoff to each
settled batch: the settlement intent id, the exact journal seq range,
the newest snapshot preceding the window, and a ready CLI invocation.

## Nondeterminism audit (adjudication path)

| Source | Control |
|---|---|
| RNG (carry escape rolls) | `tick_rng` seeded per intent; unseeded fallback = legacy behavior |
| Wall clock (ledger/message/listing/claim/settlement timestamps, `respawns_at`, cooldowns, carry sessions, bridge 1h window, dormancy payloads) | `tick_now` — frozen journal time in replay |
| Minted ids (`listing_id`, `message_id`) | `tick_gen_id` record-and-replay |
| Arrival order | `pending_intents()` is `ORDER BY created_at, rowid`; replay uses journal order |

Out of scope by design: the full tick loop (agent pool, LLM quips,
sweeps), expedition/agent-pool randomness (never inside adjudication),
and external/LLM outcomes — agent **decisions** are replayed through
the recorded intents; LLM **deliberation content** is not
reproducible and is never treated as an adjudication input.

## Honest limitations

- The live DB is opened read-only; sha256+mtime before/after are
  reported. Sidecar WAL/SHM files are not hashed (the read-only open
  cannot write them).
- Intents with no journal outcome event (presence intents, rejected
  bridge events) are not replayed.
- A window whose pre-state depends on operator/sim-command actions
  inside the window (soul births, manual batch creation) replays the
  adjudications against the overlaid live rows for those — the CLI
  documents the window it ran, it does not claim to reproduce
  operator actions.
- Post-commit side effects (memory episodes, sensations, bubbles)
  run during replay but land in the discarded temp DB / dying
  process; they never touch the live DB.
- Carry windows must start at (or before) the grab: mid-carry
  in-memory session state cannot be rebuilt and will diverge honestly.
