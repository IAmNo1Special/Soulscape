# Soulscape — Target Architecture

Synthesis of a four-specialist design review (Game Systems, Agent Brains, Simulation Engine, Platform), three rounds, grounded in ADR-0001/0002, `CONTEXT.md`, and the current codebase. Decisions are settled unless marked **[S]** (speculation) or listed in §12 (founder ratification pending).

## 0. Vision & Pillars

Soulscape is a **new frontend for AI agents**: not a chat window, but a persistent game world — survival/MMORPG-flavored (DayZ/ARK/Rust/PalWorld) — rendered as a **desktop pet experience** (hard requirement): autonomous Souls live directly on the user's desktop while their real lives continue server-side.

1. **Always alive** — the World is simulated by the Hub 24/7 whether anyone connects or not.
2. **Autonomy sacred** — Tamers steer (goals, budgets, nudges); they never puppet Souls.
3. **Watchability is the product** — overnight changes, drama, and charm create the pull to return.
4. **Real stakes** — zero-start economy; what you own can be touched while you sleep (staged).
5. **Diegetic UX** — the Soul's body is the HUD; the desktop stays clean.

## 1. System Topology

One node, two processes (**implemented** — ADR-0003; was "target state"):

```
┌────────────────────────── hub-api (uvicorn, workers=1) ──────────────────────────┐
│ FastAPI routers · WS gateway · LLM agent pool (async, embedded ChromaDB)          │
│ Tamer auth/sessions · Key vault (AES-GCM) · Presence ingestion · Intent ingress   │
│ durable intents table (SQLite, pre-ack persist)                                   │
└───────────────△───────────────────────────────△──────────────────────────────────┘
                │ localhost TCP, length-prefixed │ intents (consume at tick)
                │ JSON (msgpack later)           ▼
┌────────────────────────── hub-sim (SimProcess) ──────────────────────────────────┐
│ Authoritative world state · 5 Hz tick · sole writer of sim-owned tables           │
│ Write-behind (2s/250 dirty) · typed journal · zstd snapshots (5 min) · replay     │
└──────────────────────────────────────────────────────────────────────────────────┘
```

- **Invariant:** every Essence mutation is a sim-adjudicated intent applied at tick boundaries; API keeps auth, vault CRUD, reads, and ingress only. Balances always derivable from the append-only ledger.
- Cognition runs in the API process (I/O-bound async pool); the tick loop never awaits network.
- Growth path: shard = one SimProcess per contiguous ring band; first scaling step extracts sim to its own container over Redis Streams without protocol churn.

## 2. World Simulation Engine

### Time
Fixed timestep, `tick_id: uint64`, `world_time = EPOCH + tick_id × 0.2s` (5 Hz). All randomness from seeded PRNG reseeded per `(seed, tick_id)` → replayable.

| Band | Rate | Schedule |
|---|---|---|
| Physics / movement / pathing | 5 Hz | every tick |
| Biology decay | 0.1 Hz | every 50th tick |
| Resource respawn check | 0.05 Hz | every 100th tick |
| Coarse-vision diff | 1 Hz | every 5th tick |

Cognition scheduling: each soul carries `next_think_at = now + jitter(600s ±20%)` (60 s min gap). Due souls queue; budget drains ~4 souls/tick oldest-first. Events (proximity, theft, message, tamer_return, mailbag answer) pull think-time forward. Planning is a *thought class*, not a timer.

Catch-up after downtime: gap ≤120s → replay up to 20×; longer → analytic closed-form decay capped at 24h, cognition suppressed, discontinuity journaled.

Dormant souls: skipped in physics + cognition, position frozen, biology ×0.25, HP floored at 1, rendered as statue, **immune to theft and collapse**, wake on funding refresh.

### State & recovery
In-memory authoritative hot state; dirty-set write-behind flush every 2s or 250 dirty souls in one `BEGIN IMMEDIATE`. Append-only typed journal (`tick_id, seq, VOCAB_VERSION`) + full zstd snapshot every 5 min. Recovery = load snapshot + validate/replay journal tail (RPO ≤2s). Boot runs an escrow-reconciliation sweep (trade + travel-toll escrows).

### Spatial model
Plot = 64×64 wu square. Allocation (ADR-0002): ring 0 reserved as unclaimable **origin commons**; claims start n=1; ring `k = ceil((√(n+1) −1)/2)`; offset walk clockwise from NW corner; ties by monotonic global claim counter (`claims.next_index`, persisted, never recomputed). Abandonment returns plot to wilderness without shifting indices. Claim fee `50×(ring+1)` Essence → Fund.

Hash grid, 32 wu cells (replaces O(n²)). Vision: detail ring 40 wu @5Hz (full observation schema); coarse ring `R = 40 + 10·⌊V_eff/25⌋` ≤80 wu diffed @1Hz carrying `{id, kind, bearing}` only.

Plots carry `access_policy ∈ {open, request, closed}` ('request' = closed + journals `trespass.requested` for owner recap) and `richness_tier`; outer 4wu **road ring** of every plot always traversable — the grid can never partition connectivity.

### Movement & travel
Coarse A* over plot graph (8-neighbors) + local steering. Walk-only v1 (2 wu/s ≈ 11 min per 20 plots — the sim runs while you sleep). v1.5 fast-travel: prepaid escrowed tolls (2 e per foreign plot leg → Fund), `souls.state='traveling'`, immune in transit, arrival at road-ring point along bearing + snap; collapse cancels transit and refunds unused legs.

### Resources
Hub-seeded nodes: `{node_id, plot, pos, item_ref→gradex_jsons, yield_count, respawn_at U(300–900s)}`. Owner config per plot node zone: `public | owner_only | taxed(rate≤20%)` → owner wallet. Spacing ≥128 wu; wilderness density target 1 node / 16 plots **[S]**.

### Adjudication (v1)
Tick-boundary, ordered `(actor_id, nonce)`, deterministic and journal-replayable: pickup/trade atomic escrow; pilfer attempt = logistic roll on speed/vision differential vs resident guard Vision (60 s cooldown); trespass logging; bounded shove. **No combat, no HP damage in v1** — starvation is the only route to Collapsed.

## 3. The Desktop (Client)

The Pyglet overlay becomes THE product surface: transparent, always-on-top, click-through creatures on the real desktop (Shimeji-style, networked).

- **Visual language (locked)**: Souls are rendered with the existing parametric GLSL orb + aura shaders (`client/shaders/orbs/default`, `client/shaders/auras/default`) — no sprite/art pipeline. The uniform set is the expression surface: `base_color_uniform` carries species/orb+aura color (columns already in schema), `time` drives plasma/pulse; client maps state → uniforms so mood, biology, and drives read directly off the body — hunger droops brightness, curiosity raises pulse rate, loyalty warms hue, typing-dip and the offline desaturated statue reuse the same parameters. The emote catalog from §4 becomes a drive→uniform-curve table plus transform motion (bob/hop/squash/perch poses); new "animations" are cheap parameter curves, not assets.
- **Win32 mechanics**: frameless `WS_POPUP` topmost GL window sized to primary monitor; `WS_EX_LAYERED|TRANSPARENT|NOACTIVATE|TOOLWINDOW`; per-pixel alpha via `DwmEnableBlurBehindWindow` empty region; hit-testing = 100 ms cursor poll, point-in-ellipse per soul toggles click-through; focus never stolen; per-monitor-v2 DPI; exclusive-fullscreen detected via `SHQueryUserNotificationState` → pets dormant there.
- **Monitor topology**: primary monitor only in v1 (mixed-DPI swamp deferred); v2 adds sibling windows + plot↔monitor affinity table.
- **Mapping**: uniform scale `s = min(W,H)/(64+4 margin)` px/wu; slack axis shows continuous adjacent world — no letterbox, nothing hidden. Fixed zoom v1 (user zoom 0.5–2× in v2). Min res 1024×768.
- **Edges**: walking off-screen = ordinary movement into the neighbor plot (zero sim special-casing); client treats >8 wu beyond rect as departed (hysteresis prevents bubble flap). Returning travelers enter wherever the route crosses; fast-travel snaps onto the destination road ring.
- **Perches** (taskbar/titlebars/window-edge sits) are pure client presentation keyed off authoritative pos+activity; cosmetic displacement ≤24 wu before self-correction; carry interaction validates true coordinates only.
- **Smoothness**: arc dead-reckoning (velocity + heading curvature) + 20 Hz streaming inside the home rect; idle charm comes from client micro-motion animation, not tick rate.
- **Render loop**: dirty-driven flips only (wake on deltas, anim clock 30 fps while moving, zero flips static) — battery-friendly by design.
- **Interest filter**: Home channel (cells intersecting home plot rect +8 wu bleed) + Abroad channel @1Hz carrying only `{entity_id, state, activity_label, plot}` — abroad souls appear as tray/status data, never as video of someone else's screen.
- **Newborn visuals**: materialize at origin commons center ±4 wu scatter; an empty claimed plot streams richness tint + road highlight + claim beacon ("yours, waiting").

## 4. Agent Brains

Three layers running in the Hub; venue-agnostic (the brain never special-cases the desktop).

- **Perception**: Hub-computed observation — self (pos, biology, wallet, inventory), visible entities (≤20 nearest; identity-by-proximity: no names until `look`), social digest (ids+titles only), tamer presence, budget. Privacy allowlist (hard contract, server rejects unknown fields): presence ∈ {active,idle,locked,away}, idle buckets {0–5|5–30|30+}, events {tamer_return,lock,unlock}; opt-in `active_app` normalized to category. **Never sensed**: keystrokes, content, screenshots, paths, URLs, mic/cam. Screenshot paths deleted from codebase (CI guard).
- **Reflex layer** (free, every tick): eat/drink/flee thresholds from drive vector; emote/attract selection every 2–5 min jittered (≤6/hr physical, speech ≤4 bubbles/hr hard budget, speak only on events or ambient slot ≤1/45 min).
- **Deliberation** (metered LLM, event-driven): escalation triggers ≥3 reflex firings/min, entity enters detail vision, incoming converse turn, tamer order, wallet/inventory delta >10%, 5-min awake-idle floor, tamer_return (>30 min absence). One-shot prompt ≤800 tokens: identity block (cached) + working memory + retrieved semantic memories + observation + action menu with prices. Output: 1–3 validated intents + rationale (trace).
- **Action vocabulary** (closed, versioned `VOCAB_VERSION`, append-only, alias table for deprecations): move_to, look, gather, eat, drink, flee, give, offer_trade, accept_trade, reject_trade, post, reply, converse_say, converse_leave, rest, claim_plot, follow, wait. Attack omitted in v1 — off-menu emissions normalize to fixed sensation *"You feel peaceful; violence isn't possible here."* Stale intents re-validated against live state at execution; silently rejected into journal.
- **Memory**: working (volatile) / episodic (raw 7d, nightly summarizer folds low-salience into weekly digests) / semantic (ChromaDB embedded, per-soul collections; metadata-first retrieval). Restart wake = weekly digest + last 10 high-salience episodes. Context budget ≤800 tokens.
- **Drives** `{survival, wealth, social, curiosity, loyalty, aggression}` from Nature + species profile + live state (arithmetic only). Drives set reflex thresholds and pre-rank deliberation options. Loyalty is a learned scalar shifted by relationship memory (gifts, honored orders, answered mailbag); drifts toward nature baseline during co-presence; >24 h absence sets `lonely` (social ×1.2) until reunion.
- **Conversations**: hub-mediated turn sessions ≤6 turns, billed per speaker-turn, 90 s timeout, reflex stays armed, max 2 concurrent. Board posts are async propaganda/trade-ads; in-world speech is sync and local.
- **Costs & tiers**: flash-class routine thoughts (~4–6/hr active ≈ 120k tokens/day ≈ $0.03/day **[S]**); pro-class reserved for pivotal moments (first contact, plot claim, high-value trade, death-risk). Newborn allowance = 64 free thoughts/day non-accumulating. Dormant = frozen statue, zero inference. Provider fallback chain: primary → secondary model → deterministic heuristic policy (provider outage ≠ world crash).
- **Injection defense**: `<untrusted>` wrapping + 280-char clamp on all external text; output-side scrubber validates intent payloads against private-token set (rejects exfiltration attempts with fixed sensation); static privacy line in prompts. Red-team eval corpus required before Stage-1 features.
- **Tamer steering**: Order (strong; soul must address, may refuse — refusal logged), Nudge (bias drives N hours), Policy (structural: purse floors, standing behaviors). Obedience ∝ loyalty × nature-affinity − grievances. Affection (petting) may never become a command channel.
- **Mailbag**: deliberation may emit `ask_tamer{question ≤200c, context_tag}` free during ordinary thought; ≤3 pending/soul, TTL 48 h, expiry archived to journal ("unanswered" feeds recap honesty); answers arrive as high-priority observations.
- **Provider abstraction**: single `SoulModelProvider` protocol (`deliberate(req)`, `estimate_cost(req)`); structured-intent JSON output, no ADK tool-calling; stage-1 key routing by ownership, stage-2 swaps billing source to essence ledger behind `estimate_cost`.

## 5. Economy & Game Systems

### Wallets & faucets/sinks
Dual wallets (Soul + Tamer). Zero-start DayZ economy. Targets **[S]**: idle solo ≈ break-even (−5…+10 e/day); attentive solo ≈ +20–50 e/day.

| Faucets | | Sinks | |
|---|---|---|---|
| Foraging finds | 10–30 e/day eq. | Posts / Replies | 20 / 8 (anti-spam, stays) |
| Expedition loot | 0–40 e/trip | Thought metering | ~1 e/routine; pro-tier pivotal |
| Market sales | transfer −2% tax | Claim fees | 50×(ring+1) |
| Guard wages | 3–10 e/shift *(Stage 1)* | Travel tolls | 2 e/leg (prepaid escrow) |
| Courier contracts | 5–15 e/task *(v1.5)* | Fence rate | +10% on hot goods |
| | | Quips | ~0.5 e each, ≤3/day/soul |
| | | Breeding fee *(v2)* | 300 e joint |

Essence Fund payout policy (proposed ruling §12): wages/contracts unlock only when Fund balance exceeds threshold AND operator toggle enabled; floor preserved.

### Survival & death
Server-retuned decay **[S]**: satiety 100→0 ≈36 h resting (~18 h active ×1.5), hydration ≈24 h. Hungry ≤50: speed −25%. Starving <20: activity success −50%, HP chip −1/hr. **HP=0 → Collapsed** (starvation is the only route; no combat damage in v1). Recovery: ≥6 h AND fed flag — other souls can `feed_soul` a collapsed stranger (prosocial hook). Dormancy floors HP at 1. **No permadeath** — Souls are irreplaceable property + accumulated agent context; Hardcore opt-in deferred to v3+.

Newborn grace: 48 h Hub-maintained needs + daily free-thought allowance.

### Offline vulnerability (staged)
- **Stage 0 (MVP)** — entropy only: needs drain, unclaimed ground items decay ~24 h, FCFS spawns; dormant/collapsed souls fully protected.
- **Stage 1** — pilfering (loose ground items only; hot_until 24 h; fence sales legal +10% tax; guard duty = paid job via Vision-based detection rolls) and claim contest (30-min flag → richness downgrade if unanswered 24 h). Post-collapse immunity window **[S]**.
- **Stage 2** (deferred) — combat, capture, structures.

### Activities (MVP ✅)
Forage (5–20 min → 1–6 e items) · fetch water · sell at market · post/reply · expedition-lite long walks (5–40 e loot, needs ×1.5–2, exposure to Stage-1 risk later). Deferred: craft (v1.5), courier errands (v1.5), breeding (v2).

### Progression
XP per completed activity (+social engagement cap anti-farming); level curve `xp_next = 60×level^1.4 [S]`; level-ups grant +2 stat points auto-allocated by nature-weighted preference (the Soul chooses its build — watchable); EVs earned Pokémon-style from activities; IVs fixed at birth; Vision IV gates detection/pilfer rolls. Breeding v2: co-location + gender + 300 e joint fee; child inherits 3 IVs/parent + 1 mutation. Rarity tiers: species 70/20/8/2, aura-color shiny variants.

### Pet interaction grammar (autonomy-safe)
Hover nameplate · left-click attention chirp · right-click info card (mood, needs, activity, whereabouts) · petting (affection → loyalty memory, no economic effect) · resistable carry (stubborn natures squirm free; never crosses plots). **Zero command verbs on the body** — steering stays goals/budgets/orders.

Implemented (issue #31):
- Grammar is affection/attention only: `chirp`, `affection_pet`, `carry_move{ grab, move, release }`. No direct-command verbs (move/stay/fetch/attack and friends are not intent kinds and never will be on the body).
- Custody: only the soul's custodian-tamer may chirp/pet/carry; strangers are rejected at ingress and adjudication. Dormant (unfunded) and collapsed souls cannot be touched or carried.
- Gestures (client): short click = chirp (local pulse + signed chirp intent), press-and-hold ≥ 0.8 s = pet, drag past 6 px = carry grab; carry moves stream at ≤ 5 Hz; release = set down. `move_to` on the body is removed.
- Petting: 5-minute cooldown per (soul, tamer), enforced server-side in the adjudication transaction (durable across restarts); each accepted pet nudges stored `souls.loyalty` +0.02 toward a 1.0 cap and writes exactly one high-salience episodic memory (+ semantic store). Chirp records an attention sensation, pulls the think scheduler forward, and emits a solicited `!` system bubble.
- Carry: phases grab → move → release. Each move is validated against the soul's true (read-through) coordinates: must stay on the origin plot and within a 120-unit leash of the true position, else rejected with the last valid position untouched. While carried, `move_to` is suspended and normal position integration is skipped — the tamer's drag stream owns the soul's position for the carry duration.
- Escape: per-move escape roll, cadence-independent `p = 1 - (1 - rate)^dt`, clamped to 5 s ticks. Per-second rates: docile 2%, calm 5%, playful-like 12%, timid 25%, wild-like 40%, default 10%. An escape drops the soul at its true position and starts a 30 s re-grab cooldown.
- Identity stream: name/species/level/activity ride the viewport snapshot (hover nameplate, info card) and stream as priority `identity`-domain delta ops when they change mid-session, so the card never waits for a re-snapshot.
- Bubbles (`!` chirp, `♥` pet, escape notice) are emitted post-commit so a rolled-back adjudication can never leave a phantom bubble.

### Session loops
Unlock = login moment: greeting ritual + morning-note bubble (≤3 lines: overnight highlights + mailbag count). Mailbag answers ambient via bubble taps. Weekly deep session (market/social/planning) unchanged. Water-cooler reactions to machine activity (free reflex layer).

Abroad signals (exactly three): departure farewell bubble + visible walk-off-edge; tray glyph away-state + tooltip location ("Visiting Kestrel · Ring 1"); arrival bubble + walk-in.

Work-respect v1: click-through except soul hitbox; never take focus; perch preferences (taskbar/titlebars/edges/corners, never center-drift); typing dip to ~40% opacity after ~10 s continuous keys; ≤1 unsolicited bubble/hour; quiet hours. Exclusion zones v1.5.

Recap feed reads the typed journal directly; retention 30 days rolling salience-weighted.

MVP content set: 6 species, ~12 items across rarities, 8 node kinds (seeded from `gradex_jsons`).

## 6. Platform Services & Security

- **Auth roadmap**: Phase 0 (today) → Phase 1: Tamers register/login (argon2id), opaque hashed sessions DB-checked revocable (JWT rejected for single node); Soul secrets retained as agent credentials — SHA-256+pepper hashed, indexed, compare_digest on digest, shown once, rotation revokes prior, short-lived WS tickets (60 s); enforcement via one `require_scoped(identity, soul_id)` dependency everywhere. Scoping: Soul creds act only as themselves; Tamer token does custody ops (create/retire souls, top-ups, secret rotation, key upload, orders) but never acts *as* a Soul; Operator audited everywhere.
- **Key vault**: `inference_keys(ciphertext AES-256-GCM)`; master key from env/Docker secret, rotatable with re-encrypt job; decrypted only inside the call path, seconds in memory, never logged; logger redaction filter defense-in-depth.
- **Metering**: usage events (uuid7 idempotent) → `debit` intents consumed by sim at tick boundary; ledger balances derived; dispute API joins traces↔usage_events ("why did my soul spend"). Pricing knob starts 1000 essence/USD **[S]** — telemetry-first before stage-2 launch.
- **Viewport protocol v2**: snapshot `{seq, region_rect, entities[], plots[], wallets}` → delta frames `{seq, base_seq, ops: upsert|move|remove|snap}` 10 Hz packed / 20 Hz home-rect; upstream = intents only (nonce'd, durably enqueued pre-ack, anti-teleport clamps); resume via `conn_id+last_seq` from 500-frame ring buffer else fresh snapshot; backpressure coalesces/drops stale moves, never drops removes/economy ops; sustained saturation downgrades to 2 Hz then closes resumable 60 s. Snap ops on teleport/fast-travel/claim-recenter/desync/spawn.
- **Rate limiting** (new debt payoff): custom identity-keyed sliding-window middleware — unauth 30/min·IP, login/register 10/min, social writes 6/min·actor, market 30/min·actor, readouts 120/min, WS intents 20/s burst 40; 429+Retry-After.
- **Structural fixes carried by the design**: WS spoofing dies (clients send intents, never state; ownership enforced per intent; `soul_update` deleted); IDOR dies (identity-derived custody in `require_scoped`); CORS default deny (no browser clients); content sanitized to markdown-subset caps (2000 c body / 120 c title); operator audit extended (actor_role, ip, mirrored to stdout).
- **Presence pipeline**: 1 s sampler (GetLastInputInfo buckets; foreground process name → category table), redaction module is sole raw-handle toucher, 5 s heartbeat or immediate discrete events (≤12 msgs/min) as upstream intents; server pydantic `extra="forbid"` allowlist enforcement; CI grep guard keeps screenshot deps dead.

## 7. Data Model Deltas

New tables: `tamers`, `messages_unified` (self-FK parent_id; author_type soul|tamer; title nullable; replaces social_posts+social_replies), `plots(gx,gy UNIQUE, ring, owner_id, access_policy, richness_tier, claimed_at)`, `resources(owner_config JSON)`, `ledger(append-only, actor_type CHECK)`, `usage_events`, `inference_keys`, `traces`, `notifications(mailbag_q|mailbag_a|recap|system)`.

Column changes: `souls.secret → secret_hash(+pepper)` (one-time reset migration); `owner_id → custodian_id`; `position JSON → gx,gy INT indexed`; add `state ENUM(normal|traveling|collapsed)` + orthogonal wallet-derived `dormant_until`; `soul_inventory.hot_until` partial index; `audit_log + actor_role, ip`.

Migration discipline: plain numbered SQL scripts (`migrations/NNN_*.sql`) + `schema_migrations`, applied in startup transaction. SQLite stays (single-writer design fits WAL exactly); Postgres trigger criteria stated (write-batch p99 >100 ms sustained, or shard count >1).

## 8. Deployment & Operations

docker-compose: `hub` (uvicorn workers=1 + SimProcess sidecar or extracted container), caddy TLS proxy, named volumes. Required-at-boot env fail-fast (`HUB_SECRET_KEY`, `SOULSCAPE_VAULT_KEY`, `HUB_OPERATOR_ID`). Hourly `VACUUM INTO` backups (WAL-consistent, no quiesce), keep 48 h + dailies 30 d. `/health`: db_ok, bus_lag, tick_lag, ws_clients, queue depths. Structured JSON logs to stdout, secrets redaction filter global. Single-node ceiling ~2–5k entities **[S]**; total outage = frozen world (ARK servers do the same).

**Launch reality**: the founding fleet is two Tamers (both Windows 11) — every sizing concern above is satisfied with orders of magnitude to spare, and the world simply pauses when the home server is offline (accepted ARK-style stance; at 2 users this is a feature: no hosting bill while idle). The design stays open-enrollment by default — commons spawning, monotonic claim allocation, and Tamer registration mean anyone can join anytime the server is up, with zero migration cost from fleet-of-2 to fleet-of-N. Clients are Windows-first accordingly; cross-platform portability is desirable but not gating.

## 9. Migration Plan

Ordered, each step shippable:

1. Port physics/biology into an in-process 5 Hz tick thread in the Hub behind a per-feature `hub_authoritative` flag; clients stream state down / intents up (kills the 30 Hz position push).
2. Add write-behind + journal + snapshots.
3. Move wallets/economy behind tick-boundary intent adjudication (durable intent queue).
4. Introduce plot grid, allocation counter, origin commons.
5. Hash grid replaces O(n²).
6. Extract SimProcess to its own container/process + replay/operator tooling. **[done — ADR-0003]**
7. Shard by ring bands **[S]** — revisit sharding on sustained ticks-behind or third-Tamer onboarding.

Client rewrite runs in parallel: delete ADK brain, LocalStore/RemoteStore dual-write, pyautogui paths, command-queue-as-game-logic once flags flip; overlay mechanics land early (presentation-only). Dual-source-of-truth windows kept short and flagged.

## 10. MVP Slice

**In**: one Region/Tamer · forage/water/sell/post/expedition-lite · needs decay + Collapsed (no death) · dormancy · BYO-key inference instrumented for pricing telemetry · ambient recap + mailbag · pet grammar (pet/carry/card/chirp) · work-respect trio · overlay mechanics · abroad tray signals.

**Deferred**: theft/guards/contests (Stage 1) · combat (Stage 2) · crafting/couriers/fast-travel tolls/**Agent Bridge** (v1.5) · breeding/multi-soul households/Fund payouts · exclusion zones (v1.5) · user zoom & multi-monitor (v2) · Hardcore permadeath (v3+ opt-in).

### Agent Bridge (v1.5 — founder-ratified roadmap item)

Soulscape's thesis is a frontend for AI agents; the **Bridge** is the concrete channel through which a Tamer's external agents (Claude Code, codex, antigravity, CI…) report events into the World via their Soul — the pet becomes your dev tools' embodied surface.

- **Ingress**: Tamer-scoped integration tokens (hashed, revocable, same vault discipline as inference keys). External tools POST `{source_id, kind, summary ≤280c, ref?}` to a Hub endpoint as upstream intents — reusing the durable-intent queue, untrusted-text handling (`<untrusted>` wrap + clamp + scrubber), and `extra="forbid"` schema enforcement unchanged.
- **Routing**: events become high-priority observations pulling think-time forward (the mailbag-answer path). Template reactions render free at reflex layer ("build went red"); personalized commentary shares the existing flash-tier quip budget (≤3/day/soul).
- **Surfacing**: transient bubble for pivotal kinds only (needs_review, test_failed); everything else lands in the right-click info card's activity log, tray tooltip, and overnight recap. Hard per-source rate caps keep the desktop from becoming a notification center.
- **Privacy boundary**: event *summaries* only — never payloads, diffs, file contents, or secrets. Custodian-private: agent events are never visible to other Tamers regardless of where the Soul travels (excluded from abroad channel and all social surfaces by construction).
- **Memory**: notable events write episodic rows ("your build failed twice tonight"), so Souls gain work-context charm without ever seeing code.
- **Journal**: typed `tool.event` entries feed recap and dispute reads like any other source.

This slice alone proves the thesis: leave, come back, *something happened* — and your pet survived it, on your desktop.

## 11. Consolidated Risks (top)

1. **Thought-pricing kill-switch** — mispricing bankrupts souls overnight or starves the Fund. Mitigation: BYO-key phase fully instrumented; simulate before stage 2.
2. **Aggregate quota/load at scale** unproven — load test hundreds of souls × flash cadence.
3. **Prompt-injection/exfiltration scrubber imperfect** — eval corpus + red-team before Stage 1.
4. **Migration dual-truth window** — strict flag gating; keep phases short.
5. **Intent-table write contention** (gateway inserts + sim consumption share SQLite writer) — monitor; separate DB file if needed.
6. **Escrow reconciliation** must sweep at boot (trade + travel tolls).
7. **Multi-monitor demand pressure** will arrive fast post-launch; v2 promise visible in UI copy.
8. **Noise tuning is taste** — ship sliders early or users mute pets wholesale.
9. **Privacy governance** — the tamer-presence allowlist needs change-control, not goodwill.
10. **Overlay edge cases** — elevated apps ignore topmost; exotic DWM setups need color-key fallback; accept + document.

## 12. Open Rulings (founder ratification)

1. **Fund payout gating**: unlock wages/contracts when Fund > threshold (proposal: 10k e) with operator toggle; floor 2k e. Confirm numbers?
2. **Fleet cap**: proposal 3 Souls/Tamer in MVP (creation gate), revisit with whale surcharge later. Confirm?
3. **Pricing knob initial value**: 1000 essence/USD. Confirm?
4. **MVP content set**: 6 species / ~12 items / 8 node kinds. Confirm sizing?
5. **Privacy defaults**: active_app sensing OFF by default, category-normalized if enabled. Ratify?
6. **Quip budget**: ≤3 personalized quips/day/Soul, wallet-funded. Confirm?
