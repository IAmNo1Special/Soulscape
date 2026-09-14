# Soulscape Technical Debt Remediation Plan

## Summary of Issues Addressed

This plan addresses 25+ Technical Debt, Risk, and Quirk items identified during the production-grade codebase deep dive, refined through iterative sub-agent review (security analyst, performance engineer, architecture reviewer).

---

## Phase 1: Critical Bug Fixes & Security (P0)

### 1.1 Fix `Evolution.evolves_to` Forward Reference Runtime Crash
**File:** `shared/models.py:48`
**Problem:** `Evolution.evolves_to: Species` references `Species` before it's defined at line 56. This causes a `NameError` at import time — a runtime crash, not just a type annotation issue. Pydantic v2 evaluates field annotations eagerly.
**Fix:**
- Change to `evolves_to: "Species"` (string forward reference)
- Verify the fix resolves the import crash
- P0: This is a **blocking import crash**, not cosmetic

### 1.2 Fix Multi-Step Transaction Race Conditions (Corrected Diagnosis)
**Files:** `server/database.py`, `server/routers/marketplace.py`, `server/routers/social.py`
**Problem (Corrected):** `charge_soul()` is already atomic — the `UPDATE ... WHERE ... AND essence >= ?` + `rowcount` check is the correct pattern. The real race condition risk is in **multi-step transactions** (`buy_item()`, `create_post()+charge`, `reply_to_post()+charge`) which perform multiple SQL operations in a single `get_db()` context but lack an explicit `BEGIN IMMEDIATE` transaction start. Under concurrent writes, SQLite's deferred transaction mode allows other connections to read stale data between steps.
**Fix:**
- Add `conn.execute("BEGIN IMMEDIATE")` at the start of all multi-statement write operations in `marketplace.py` and `social.py`
- Add `PRAGMA busy_timeout=5000` to `_get_connection()` in `database.py` (see 3.1 for full context)

### 1.3 Fix HTTPS Enforcement — URL Parsing Vulnerability
**File:** `client/system/network/client.py:36-49`
**Problem (Corrected):** The existing `"test-hub" in self.base_url` substring check is a URL parsing vulnerability — `http://test-hub.evil.com` would pass as "local". The plan must use proper URL parsing.
**Fix:**
- Use `urllib.parse.urlparse(base_url).hostname` instead of substring matching
- Allow HTTP only for exact hostnames: `localhost`, `127.0.0.1`, `::1` (IPv6 loopback)
- Remove `test-hub` from the allowlist entirely — it is not a loopback address
- Add an explicit `verify=True` parameter to `httpx.AsyncClient` (already default, but make it explicit)
- Add a startup warning if `HUB_SECRET_KEY` is not set (already partially done in `server/main.py:25-28`)

### 1.4 Fix SQL Empty IN Clause + Replace SELECT * with Explicit Columns
**File:** `server/routers/souls.py:55-58`, `44`
**Problem (Corrected):** The f-string IN clause is not SQL injection vulnerable (placeholders are parameterized). However:
- The empty `soul_ids` list edge case generates invalid SQL (`WHERE soul_id IN ()`)
- `souls.py:44` (`cursor.execute("SELECT * FROM souls")`) uses SELECT * which returns all columns including `secret` — the secret is correctly stripped by `s.pop("secret", None)` at line 48 before returning, but SELECT * is still a code quality risk (future schema changes could leak new sensitive columns)
**Fix:**
- Add guard clause: `if not soul_ids: return souls` before the inventory query
- Replace `SELECT *` with explicit column lists (excluding `secret`) in all router queries
- Add a comment documenting the operator exception for `get_souls()` without `owner_id`

### 1.5 Add WebSocket Message Spoofing Protection with DoS Mitigation
**File:** `server/routers/websockets.py:89-98`
**Problem:** Once authenticated, a WebSocket client can inject fake soul updates for other owners by sending a `soul_update` message with arbitrary `owner_id` and `soul_id` values. The server broadcasts this to all connections for that owner without validating that the soul IDs belong to the authenticated user.
**Fix:**
- After parsing `soul_update` messages, validate that all `soul_id` values belong to the authenticated `owner_id`
- **DoS Mitigation:** Cache the set of owned soul_ids per WebSocket connection on connect (fetched once from DB). Subsequent messages validate against the cached set instead of querying the DB per message. This prevents a malicious client from spamming DB queries per message.
- Reject messages with unauthorized soul IDs with WS close code 1008
- Log spoofing attempts for security audit
- Enforce a per-message rate limit (e.g., max 10 soul_update messages per second per connection) as part of the rate limiting in 1.6

### 1.6 Add Rate Limiting on API Endpoints
**Problem:** No rate limiting exists on any endpoint. An attacker can spam `buy` requests, open thousands of WebSocket connections, flood social endpoints, or brute-force soul secret authentication indefinitely.
**Fix:**
- Add rate limiting middleware (e.g., `slowapi` for FastAPI) with per-identity limits:
  - 10 requests/second for API endpoints (burst tolerance: allow short spikes up to 20 req/s)
  - 5 connection attempts/minute for WebSocket connections (sliding window per IP + identity)
- Apply stricter limits to auth-sensitive endpoints (`/souls`, `/marketplace/buy`, `/social/post`)
- **Lockout guard:** Use a sliding window algorithm, not a fixed window, to prevent burst attacks at window boundaries
- **NAT/firewall consideration:** Track both IP-based and identity-based limits. IP limits apply to unauthenticated requests; identity limits apply to authenticated requests. An authentication failure count tracked per IP should not lock out other users behind the same NAT
- Exempt the `/health` endpoint from rate limiting

### 1.7 Add Operator Action Audit Logging with Append-Only Protection
**Problem:** The operator role can override any field on any endpoint. A compromised operator key gives unrestricted control with no audit trail beyond basic request logging. Audit logs could also be tampered with by an attacker with DB access.
**Fix:**
- Add structured security audit logging for all operator actions (what was changed, by which operator key, at what time, which soul_ids were affected)
- Write audit logs to a dedicated `audit_log` table with an append-only design (no UPDATE or DELETE permissions on the table for application users)
- Log all authentication failures with timestamps (but not secrets) to a separate `auth_log` table
- Use database-level triggers or application-level constraints to prevent modification of existing audit entries
- The `audit_log` table should only have INSERT permission for the application DB user

### 1.8 Enforce WebSocket Token Lifetime + Add Secret Index + Secure Revocation
**Files:** `server/security.py:81-116`, `server/database.py`
**Problem:** WebSocket auth tokens (soul secrets) never expire or get revoked. A compromised token grants indefinite access. Auth lookups also do full table scans.
**Fix:**
- Add a `token_expiry` column (REAL, Unix timestamp) and an `is_revoked` boolean column to the `souls` table
- Validate token expiry in both `verify_ws_token()` and `get_api_key()`
- Define a default token lifetime (e.g., 30 days)
- Add a `revoke_token()` utility accessible only to operator identity (validated via `X-Hub-Secret` header matching `HUB_SECRET_KEY`)
- Add `CREATE INDEX IF NOT EXISTS idx_souls_secret ON souls(secret)` for auth lookups
- Add periodic cleanup of expired tokens in the lifespan startup

### 1.9 Add Minimum Secret Strength Validation
**Problem:** Soul secrets are user-provided at `souls.py:137` (`s.get("secret") or existing_secrets.get(soul_id)`). Users can set weak, guessable secrets. No validation of secret entropy exists at any creation or update path.
**Fix:**
- At soul creation and secret update, validate that secrets meet a minimum length (e.g., 32 characters) and entropy threshold
- Generate cryptographically secure secrets using `secrets.token_hex(32)` as the default when none is provided, rather than relying on user-provided values
- Reject secrets that are common words, empty, or shorter than 16 characters
- Store secrets as hashed values (bcrypt/argon2) in the DB rather than plaintext — the current schema stores them in plaintext which is a critical vulnerability if the DB is compromised

### 1.10 Add Security Headers Middleware
**Problem:** The FastAPI application has no CORS configuration, no `X-Content-Type-Options`, no `X-Frame-Options`, no `Strict-Transport-Security` header, and no response size limits. WebSocket endpoints are similarly unprotected.
**Fix:**
- Add `starlette.middleware.cors.CORSMiddleware` with explicit `allow_origins` (not `allow_origins=["*"]`)
- Add a middleware that injects security headers on all responses:
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Strict-Transport-Security: max-age=31536000; includeSubDomains` (when HTTPS is enforced)
  - `Referrer-Policy: strict-origin-when-cross-origin`
- Configure `uvicorn` with a `limit_max_requests` and `limit_request_line` to prevent resource exhaustion

### 1.11 Add Input Sanitization for Social Content
**Problem:** Social posts (`create_post`, `reply_to_post`) and edits (`edit_message`) accept arbitrary string content with no sanitization. If this content is ever rendered in a web frontend or desktop UI with HTML rendering, it enables XSS/stored injection attacks.
**Fix:**
- Add HTML/script tag stripping or HTML entity encoding for social content at the API layer, following the pattern in `client/utils/security.py:sanitize_content()`
- Validate that content does not contain executable payloads (script tags, event handlers, embedded objects)
- Apply the same sanitization on the client side when rendering social messages from the hub

---

## Phase 2: Architecture & Code Quality (P1)

### 2.1 Deduplicate `Stat` Enum (With Workspace Fix)
**Files:** `shared/enums.py:4` and `client/core/biology/stats.py:17`
**Problem:** The `Stat(str, Enum)` is defined independently in both `shared/` and `client/core/biology/`. The shared version is unused by the client's simulation code.
**Fix:**
- **Prerequisite:** Add `shared/` as a workspace member in `pyproject.toml` (`[tool.uv.workspace]` members) and ensure it's installed in both venvs via `uv sync` from the workspace root
- Remove the duplicate `Stat` enum from `client/core/biology/stats.py`
- Import `Stat` from `shared.enums` in `client/core/biology/stats.py`
- Do NOT modify `client/core/biology/mechanics.py` — it already uses string comparison at runtime (line 54) and imports `Stat` only in `TYPE_CHECKING`. No runtime change needed.
- If `shared/` cannot be added to the workspace immediately, harmonize the values and add a TODO comment referencing the canonical location

### 2.2 Unify ID Types (soul_id)
**Files:** `shared/models.py:66` (PositiveInt), `server/models.py:73` (str), `client/core/biology/biology.py:46` (UUID hex string)
**Problem:** The shared `Soul` model uses `PositiveInt` for `soul_id`, the server uses `str`, and the client uses UUID hex strings. These are incompatible.
**Fix:**
- Update `shared/models.py` to use `str` for `soul_id` in `Soul` and `Tamer`
- Align `SoulResponse.soul_id` in server with `str` type
- Document the type contract: `soul_id` is a string (UUID hex or custom ID)
- Fix `Evolution.evolves_to: "Species"` as part of this fix (see 1.1)

### 2.3 Keep `message_id` in URL Path for `edit_message`
**Files:** `server/models.py:50-53`, `server/routers/social.py:180-216`
**Decision:** The current design is architecturally sound — path params identify the resource, body params describe the mutation. No model change needed.
**Fix:**
- Keep `message_id` in the URL path (RESTful convention)
- Add server-side validation that `message_id` from the path matches any `message_id` in the body (if present, for future extensibility)

### 2.4 Add `/health` Endpoint
**File:** `server/main.py` or `server/routers/`
**Problem:** No health check endpoint exists (already noted in TODO.md).
**Fix:**
- Add `@router.get("/health", dependencies=[])` to `server/main.py`
- Returns `{"status": "online", "db_ok": bool}` after a quick DB ping
- No auth required (health checks should be public)

### 2.5 Delete Root `main.py` (0 bytes)
**File:** `D:\projects\Soulscape\main.py`
**Problem:** The root `main.py` is empty and creates confusion about the actual entry points.
**Fix:**
- Delete the empty file
- The actual entry points are `server/main.py` and `client/main.py`, both invoked via their respective `uv run main.py` commands

### 2.6 Add Database Indexes
**File:** `server/database.py` (schema definitions in `init_db()`)
**Problem:** The `souls` table has no explicit indexes beyond the primary key. Queries like `WHERE owner_id = ?` do full table scans.
**Fix:**
- Add `CREATE INDEX IF NOT EXISTS idx_souls_owner_id ON souls(owner_id)`
- Add `CREATE INDEX IF NOT EXISTS idx_souls_secret ON souls(secret)` for auth lookups
- Add `CREATE INDEX IF NOT EXISTS idx_soul_inventory_soul_id ON soul_inventory(soul_id)`
- Add `CREATE INDEX IF NOT EXISTS idx_marketplace_listing_id ON marketplace(listing_id)`
- Add `CREATE INDEX IF NOT EXISTS idx_social_posts_message_id ON social_posts(message_id)`

### 2.7 Fix Pydantic Version Fragmentation
**Problem:** `server/pyproject.toml` pins `pydantic>=2.12.5` while `client/pyproject.toml` and root `pyproject.toml` have no pydantic dependency. The separate `.venv` directories (see 5.1) may have different resolved versions, causing silent incompatibility risks.
**Fix:**
- Remove `server/.venv` and `client/.venv` (see 5.1)
- Run `uv sync` from workspace root to unify all dependencies
- Run `uv lock` to regenerate `uv.lock` with consistent versions
- Verify all tests pass after unification

### 2.8 Fix `SoulResponse` Stat Data Completeness
**File:** `server/models.py`, `server/routers/souls.py`
**Problem:** The DB `souls` table has 21 stat columns (`stat_hp_base` through `stat_vis_ev`), but `SoulResponse` has zero corresponding fields. All stat data is silently dropped when the server returns soul data via FastAPI's `response_model`.
**Fix:**
- Add all stat fields to `SoulResponse`: `stat_hp_base`, `stat_atk_base`, `stat_def_base`, `stat_spa_base`, `stat_spd_base`, `stat_spe_base`, `stat_vis_base`, `stat_hp_iv`, `stat_atk_iv`, `stat_def_iv`, `stat_spa_iv`, `stat_spd_iv`, `stat_spe_iv`, `stat_vis_iv`, `stat_hp_ev`, `stat_atk_ev`, `stat_def_ev`, `stat_spa_ev`, `stat_spd_ev`, `stat_spe_ev`, `stat_vis_ev`, `nature`
- OR create a nested `SoulStatsResponse` model and include it in `SoulResponse`

---

## Phase 3: Performance Optimization (P2)

### 3.1 Add `PRAGMA busy_timeout` to DB Connection
**File:** `server/database.py:20-26`
**Problem:** `_get_connection()` creates a new `sqlite3.Connection` for every request with no busy timeout. Under contention, SQLite returns `OperationalError: database is locked` immediately instead of waiting.
**Fix:**
- Add `conn.execute("PRAGMA busy_timeout=5000")` to `_get_connection()`
- This is the single most impactful SQLite optimization for this workload
- WAL mode is already enabled (correct)
- **Do NOT** use `check_same_thread=False` for connection pooling — SQLite is not thread-safe for concurrent writes and this disables SQLite's thread safety guard, risking database corruption
- **Do NOT** suggest `aiosqlite` — it is not a current dependency and would require rewriting all router handlers to be async

### 3.2 Fix `ConnectionManager.broadcast()` Concurrency
**File:** `server/managers.py:39-47`
**Problem:** `broadcast()` iterates all active WebSocket connections sequentially. A single slow or disconnected client blocks the entire broadcast loop. Each incoming `soul_update` WebSocket message triggers a broadcast to all other connected owners, so broadcast frequency depends on the number of connected clients — under load, sequential sends can accumulate significant stall time.
**Fix:**
- Use `asyncio.gather()` with `return_exceptions=True` to send to all clients concurrently
- Add per-client send timeout (e.g., 5 seconds) using `asyncio.wait_for()`
- Gracefully handle timeout by removing the dead connection from `active_connections`
- Add an `asyncio.Semaphore(10)` to cap concurrent sends and prevent overwhelming the event loop
- Pre-serialize JSON once per broadcast using `json.dumps()` before sending to avoid redundant serialization per client

### 3.3 Optimize O(n²) Collision Detection (Scale-Justified)
**File:** `client/core/soul/physics.py:338-402`
**Problem:** `_apply_separation()` checks every soul against every other soul every frame. The code already has a bounds-check early exit (lines 361-364) that eliminates distant pairs.
**Fix:**
- **Only implement if N > 20 souls** — the existing early-exit optimization makes brute force acceptable for typical loads
- If needed, implement spatial hashing with cell size = `separation_radius`
  - Each soul belongs to cell `(floor(x/cell_size), floor(y/cell_size))`
  - Only check souls in the same cell and the 8 adjacent cells (3x3 neighborhood)
  - Track dirty souls (moved since last frame) and only rebuild their cell assignments each frame
  - As a simpler alternative, sort souls by x-coordinate and do a linear sweep, checking only neighbors within `separation_radius` in x
- Document the decision to keep or change the algorithm with the expected soul count

### 3.4 Remove Wasted Screenshot Thread (Consolidated with 4.5)
**Files:** `client/core/soul/agent.py:609-624`, `client/core/soul/agent.py:424`
**Problem:** `_start_agent_thread()` spawns a thread that captures a screenshot via `pyautogui.screenshot()` (line 613), but `_run_turn_async()` immediately sets `img_bytes = None` on line 424, discarding the screenshot. The `pyautogui` import (line 14) and the entire screenshot thread are wasted work that can conflict with Pyglet's OpenGL context on the main thread.
**Fix:**
- Remove the screenshot capture code entirely from `_start_agent_thread()`
- Remove the `pyautogui` import from `agent.py` (unless used elsewhere — check)
- Simplify `_start_agent_thread()` to call `_run_agent_step()` directly without a thread for screenshotting
- If screenshots are desired for future use, track as a separate TODO and do not remove the infrastructure

### 3.5 Add Caching for Read-Heavy Endpoints (Scoped Carefully)
**Files:** `server/routers/souls.py`, `server/routers/marketplace.py`
**Problem:** Every HTTP request hits SQLite directly. Read-heavy endpoints like `get_souls()` and `get_marketplace()` would benefit from a short-lived cache, but cache invalidation for marketplace purchases is tricky (stale listings could cause double-purchase attempts).
**Fix:**
- Add `cachetools` to `pyproject.toml` dependencies
- Add an in-memory TTL cache for `get_marketplace()` using `cachetools.TTLCache(maxsize=100, ttl=10)` — marketplace listings change infrequently
- For `get_souls()`, first optimize the query (explicit columns, skip `secret`) — WAL mode makes this fast enough
- Do NOT cache marketplace individual listings — the delete-on-buy pattern makes stale cache dangerous
- Add JSON parsing caching for `json.loads()` on `position`/`hometown`/`item` fields — parse once and cache the parsed result alongside the raw DB row
- No stampede protection needed for a small deployment, but add a `cachetools` LRU cache for parsed JSON results if profiling shows it's a bottleneck
- Use `concurrent.futures.ThreadPoolExecutor(max_workers=4)` instead of spawning unbounded daemon threads for agent decisions. This prevents thread pool exhaustion when multiple souls have active agents simultaneously.

### 3.6 Address Client Position Broadcast Frequency
**File:** `client/main.py:455`
**Problem:** The client sends position updates to the Hub every ~33ms (30Hz) for all local souls. With N local souls, each broadcast is O(N) in serialization and network send. At scale, this can saturate network bandwidth or the Hub's WebSocket receive buffer.
**Fix:**
- Increase the broadcast interval from 33ms to 200ms (5Hz) — position changes are visually smooth at 5Hz for other viewers
- Ensure the dirty-flag check (position changed >1px since last broadcast) is still applied
- Consider delta compression: only send the delta from the last acknowledged position rather than absolute coordinates
- Document the tradeoff: higher frequency = more responsive sync but higher bandwidth; lower frequency = less bandwidth but slightly delayed remote soul positions

---

## Phase 4: Robustness & Error Handling (P3)

### 4.1 Add JSON Bomb Protection for `MarketListing.item`
**Files:** `server/models.py:15`, `server/routers/marketplace.py:38`
**Problem:** `json.loads()` on `listing["item"]` at `marketplace.py:38` and on `MarketListing.item` fields has no size or nesting depth limit. A malicious or malformed JSON payload could cause denial-of-service via deeply nested objects or huge strings.
**Fix:**
- Add a `json.loads()` guard with `max_depth` and `max_size` limits (e.g., max 100KB, max nesting depth 10)
- Validate `MarketListing.item` with a Pydantic model that enforces reasonable size constraints

### 4.2 Move `InventoryCommand` Capacity Check into `Inventory.add_item()`
**File:** `client/core/commands.py:44-56`, `client/core/interactions/inventory.py:187`
**Problem:** The capacity check `len(soul.inventory.items) < capacity` in `commands.py` is not atomic — between the check and the `add_item()` call, another operation could modify the inventory.
**Fix:**
- Move the capacity check into `Inventory.add_item()` itself to make it atomic at the method level
- Return `False` if inventory is full (already does this — the command just adds redundant pre-check)
- Keep the command's pre-check as an optimization to avoid unnecessary inventory creation, but the real guard must be in `add_item()`

### 4.3 Make `decision_interval` Configurable
**File:** `client/core/soul/agent.py:53`
**Problem:** `decision_interval = 3600.0` (1 hour) is too infrequent for a responsive simulation. Reducing it would increase server load significantly.
**Fix:**
- Make `decision_interval` configurable via environment variable or settings: `SOUL_DECISION_INTERVAL` (default 3600)
- Add a `decision_frequency` parameter to `SoulAgent.__init__()`
- If reducing the default, add server-side rate limiting to prevent agent decision storms when many souls become active simultaneously
- Remove the wasted screenshot thread (see 3.4) to offset any performance cost from more frequent decisions

### 4.4 Fix `SoulResponse` Stat Field Mapping (Replaces Previous 4.2)
**File:** `server/models.py`, `server/routers/souls.py`
**Problem:** `SoulResponse.model_config = ConfigDict(from_attributes=True)` works fine for `dict(row)` inputs, but the real fragility is data completeness — 21 stat columns are silently dropped because `SoulResponse` has no stat fields. This is a data bug, not a Pydantic fragility issue.
**Fix:**
- See 2.8 (add all stat fields to SoulResponse)
- The `from_attributes=True` actually works correctly for the current DB schema

### 4.5 Consolidate `pyautogui` Thread Fix (Merged with 3.4)
**File:** `client/core/soul/agent.py`
**Decision:** This item is consolidated with 3.4. The `pyautogui` screenshot thread is removed entirely along with the thread-spawning code in `_start_agent_thread()`. The `pyautogui` import can be removed from `agent.py` unless it's used elsewhere in the file.

---

## Phase 5: Maintenance & Developer Experience (P4)

### 5.1 Unify Virtual Environments
**Problem:** `server/.venv` and `client/.venv` exist as isolated environments with duplicate dependencies, separate from the root `uv.lock` and workspace. This causes version fragmentation and makes dependency management confusing.
**Fix:**
- Delete `server/.venv/` and `client/.venv/` entirely
- Run `uv sync` from the workspace root (`D:\projects\Soulscape`) to create a single unified environment
- Run `uv lock --frozen` to regenerate `uv.lock` with consistent versions across all workspace members
- Verify all tests pass: `uv run pytest` from workspace root
- Update `.gitignore` to ignore `.venv/` at any depth (already partially covered)

### 5.2 Make `shared/` a Proper Workspace Member
**Problem:** `pyproject.toml` lists only `server/` and `client/` as workspace members. The `shared/` package has no dependency group entry and no proper package configuration, making cross-imports fragile.
**Fix:**
- Add `shared/` to workspace members in `[tool.uv.workspace]`
- Add `shared/` to `[tool.uv.sources]` if it needs its own package config
- OR ensure `shared/` is importable from both client and server without being installed as a separate package (it works as-is since it's in the Python path when run from the workspace root)
- Document the `shared/` package contract in a `shared/README.md`

### 5.3 Add Integration Tests for End-to-End Flows
**Problem:** The test suite has unit tests for auth, souls, marketplace, and social, but no integration test covering the full user journey.
**Fix:**
- Add an integration test in `server/tests/` (`test_integration.py`) that:
  1. Creates a soul via POST `/souls`
  2. Lists an item via POST `/marketplace/list`
  3. Buys the item via POST `/marketplace/buy/{id}`
  4. Creates a post via POST `/social/post`
  5. Replies to the post via POST `/social/reply`
- Verify essence balance after purchase, tax collection in globals table, inventory changes
- Use FastAPI's `TestClient` (matching existing `conftest.py` pattern) for synchronous integration tests; use `httpx.AsyncClient` for async WebSocket tests
- Use an in-memory SQLite (`:memory:`) or a temporary file for test DB isolation

### 5.4 Document the WebSocket Protocol
**File:** `server/README.md` (extend existing) or new `WEBSOCKET_PROTOCOL.md`
**Problem:** The WebSocket message format is undocumented, making it hard for new clients to connect.
**Fix:**
- Add to `server/README.md` or create `WEBSOCKET_PROTOCOL.md`:
  - Connection URL format: `ws(s)://{hub_url}/ws/{owner_id}?token={api_key}` or header `X-Hub-Secret`
  - Message types and expected formats:
    - `connected`: `{ "type": "connected", "online_owners": [...] }`
    - `owner_online`: `{ "type": "owner_online", "owner_id": "..." }`
    - `owner_offline`: `{ "type": "owner_offline", "owner_id": "..." }`
    - `soul_updated`: `{ "type": "soul_updated", "owner_id": "...", "souls": [...] }`
  - Ping/pong behavior: send `"ping"` text, receive `"pong"` text
  - Auth: via `?token=` query param or `X-Hub-Secret` header

### 5.5 Run Linting & Formatting Pass
**Problem:** The project uses `ruff` (defined in `pyproject.toml`) but the remediation plan doesn't include running it. New code introduced by fixes must follow project conventions.
**Fix:**
- Run `ruff check` and `ruff format` across both `server/` and `client/` directories after all changes
- Fix any lints introduced by the remediation changes
- Add a pre-commit hook or CI step to run `ruff` automatically