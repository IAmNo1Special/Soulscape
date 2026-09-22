# AGENTS.md — Soulscape Project Instructions for AI Coding Agents

## Project Overview

Soulscape is a decentralized metaverse ecosystem comprising a transparent desktop overlay client and a FastAPI-based backend hub. The client renders autonomous "soul" entities directly onto the user's desktop using Pyglet; the hub provides persistent storage, social feeds, a marketplace with tax collection, and real-time WebSocket synchronization.

## Repository Structure

```
Soulscape/
├── AGENTS.md                # This file
├── pyproject.toml           # Workspace root config (uv workspace)
├── uv.lock                  # Unified lockfile (root workspace)
├── main.py                  # Empty placeholder (delete; entry points are in server/ and client/)
├── README.md                # Project readme
├── TODO.md                  # Task tracker
├── tech_debt_plan.md        # Technical debt remediation plan
├── connecting_others.md     # Documentation for connecting other projects
├── .gitignore               # Git ignore rules
├── .python-version          # Python version spec
├── server/                  # FastAPI backend hub
│   ├── main.py              # FastAPI app creation, router inclusion, lifespan
│   ├── __main__.py          # Entry point for `uv run --package server python -m server`
│   ├── models.py            # Pydantic request/response models
│   ├── database.py          # SQLite init, schema, connection manager, charge_soul()
│   ├── security.py          # API key auth, UserIdentity, WebSocket token verification
│   ├── managers.py          # ConnectionManager for WebSocket presence tracking
│   ├── routers/             # API routers: souls, marketplace, social, websockets
│   ├── tests/               # Pytest test suite (unit tests + integration)
│   ├── Dockerfile           # Docker image for deployment
│   ├── docker-compose.yml   # Docker Compose for local deployment
│   └── soulscape_hub.db     # SQLite database (generated)
├── client/                  # Pyglet overlay client
│   ├── main.py              # SoulscapeApp — main application class
│   ├── __main__.py          # Entry point for `uv run --package client python -m client`
│   ├── constants.py         # Rendering/physics constants
│   ├── core/                # Core simulation logic
│   │   ├── soul/soul.py     # Soul class (main actor)
│   │   ├── soul/agent.py    # SoulAgent (LLM agent via Google ADK + Gemini)
│   │   ├── soul/physics.py  # SoulPhysics (movement, collision, input)
│   │   ├── biology/         # SoulBiology, stats, species, gender, mechanics
│   │   ├── interactions/    # Inventory, Marketplace, MessageBoard
│   │   ├── commands.py      # Command pattern for actions
│   │   └── stores/          # DataStore abstraction (LocalStore + RemoteStore)
│   ├── shaders/             # GLSL shaders for 3D orbs and auras
│   ├── system/              # Infrastructure: network, persistence, input, tray, window
│   ├── ui/                  # GUI (tkinter/ttkbootstrap) and graphics (Pyglet shaders)
│   ├── tests/               # Pytest test suite (unit + integration)
│   └── utils/               # Helpers, security sanitization, shader utilities
├── shared/                  # Shared models and enums (both client and server)
│   ├── models.py            # Pydantic models (Soul, Tamer, Species, etc.)
│   └── enums.py             # Stat enum
└── gradex_jsons/            # Static game data (abilities, capsules, items, moves, etc.)
```

## Technology Stack

- **Language**: Python 3.13+
- **Build/Package**: Astral uv (`uv sync --all-packages`, `uv run`)
- **Server**: FastAPI, Pydantic v2, Uvicorn, SQLite (WAL mode), WebSockets
- **Client**: Pyglet, Google ADK (Gemini), magetools (ChromaDB), PyAutoGUI, httpx, websockets, Pillow, numpy, ttkbootstrap, pystray
- **Auth**: API key via `X-Hub-Secret` header (`secrets.compare_digest`)
- **Rendering**: Pyglet with GLSL vertex/fragment shaders for 3D orbs and auras
- **Testing**: pytest (run from root with `uv run pytest`)

## Setup & Development Commands

All commands must be run from the project root (`D:\projects\Soulscape`) unless specified otherwise.

### Initial Setup
```bash
uv sync --all-packages        # Install all workspace dependencies (plain `uv sync` omits the members: root is a virtual package)
```

### Running the Hub (Server)
The Hub needs two processes: the API and the simulation. Run one in each terminal:
```bash
uv run --package server python -m server              # API at http://0.0.0.0:9785
uv run --package server python -m server.sim_process  # SimProcess (required; without it /health reports "degraded" and mutations 503)
```

### Running the Overlay (Client)
```bash
uv run --package client python -m client  # Starts the Pyglet overlay window
```

### Testing
```bash
uv run pytest                    # Run all tests across server/ and client/
uv run pytest server/tests/      # Run server tests only
uv run pytest client/tests/      # Run client tests only
```

### Linting & Formatting
```bash
ruff check server/ client/       # Linting
ruff format server/ client/      # Formatting
```

### Dependency Management
```bash
uv lock                          # Regenerate uv.lock from pyproject.toml
uv add <package>                 # Add dependency to appropriate workspace member
```

## Conventions

### Code Style
- Follow the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) (enforced for the server)
- Use `ruff` for linting and formatting
- Line length: 80 characters (Black config)
- No comments in source code unless explicitly asked (enforced by project convention)
- Type annotations required for all public functions and class methods

### Naming Conventions
- Python files: `snake_case.py`
- Classes: `PascalCase`
- Functions/methods: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private attributes: `_leading_underscore`

### Architecture Patterns
- **Command Pattern**: All game actions use `Command` ABC in `client/system/command_queue.py` with `execute(context)` method
- **Singleton Pattern**: `Marketplace` and `MessageBoard` in the client use `__new__` for single instances
- **Repository Pattern**: `DataStore` ABC in `client/core/stores/base.py` with `LocalStore` and `RemoteStore` implementations
- **Strategy Pattern**: `Nature.get_modifier()` in `client/core/biology/mechanics.py` returns stat multipliers
- **Observer Pattern**: `ConnectionManager.broadcast()` pushes state changes to all WebSocket clients

### Database Conventions
- SQLite with WAL mode (`PRAGMA journal_mode=WAL`)
- Connections open per-request via `database.get_db()` context manager
- No connection pooling in the current implementation (adequate for current scale)
- Schema defined in `database.init_db()` with `CREATE TABLE IF NOT EXISTS`
- All column names use `snake_case`

### Security Conventions
- API keys authenticate via `X-Hub-Secret` header
- `secrets.compare_digest()` for timing-safe comparison
- Soul secrets stored in DB `secret` column — should be hashed (currently plaintext)
- Operators identified by `HUB_OPERATOR_ID` env var (default: `HUB_OPERATOR`)
- IDOR mitigation: derive `owner_id` from authenticated identity, never trust client-provided owner_id from users

## Key Architecture Details

### Server API Endpoints
| Method | Path | Description |
|--------|------|-------------|
| GET | `/souls` | List souls (filter by owner_id for users; all for operators) |
| POST | `/souls` | Create/update souls for an owner |
| GET | `/marketplace` | List marketplace with essence fund |
| POST | `/marketplace/list` | Add a listing |
| POST | `/marketplace/buy/{listing_id}` | Buy a listing (2% tax to essence_fund) |
| GET | `/social` | Get all posts with replies |
| POST | `/social/post` | Create a post (costs 20 essence) |
| POST | `/social/reply` | Reply to a post (costs 8 essence) |
| POST | `/social/edit/{message_id}` | Edit a post or reply |
| POST | `/social/delete/{message_id}` | Delete a post or reply |
| WS | `/ws/{owner_id}` | WebSocket for real-time presence and soul updates |

### Client Main Loop (`client/main.py`)
1. `SoulscapeApp.__init__()` — loads settings, souls, creates NetworkService, event loop, GUI process
2. `SoulscapeApp.run()` — creates Pyglet window, loads souls, starts network, enters `pyglet.app.run()` at 60 FPS
3. `update_souls(dt)` — called every frame: processes network events, updates physics/biology/AI, broadcasts position updates every ~33ms, saves state every 30s
4. `check_gui_results()` — polls GUI command queue every 100ms for settings/menu interactions

### WebSocket Protocol
- Connect to `ws(s)://{hub_url}/ws/{owner_id}` with `X-Hub-Secret` header
- Server sends: `connected` (online owners), `owner_online`, `owner_offline`, `soul_updated`
- Client sends: `soul_update` with position updates every ~33ms when dirty-flagged
- Ping/pong: client sends `"ping"`, server responds `"pong"`

## Known Issues & Technical Debt

### P0 — Blocking Bugs
1. **`Evolution.evolves_to` forward reference crash** (`shared/models.py:48`): `NameError` at import time. Fix: change to `evolves_to: "Species"` (string forward reference).
2. **Multi-step transaction race conditions**: `buy_item()`, `create_post()+charge`, `reply_to_post()+charge` lack `BEGIN IMMEDIATE` transaction start. Fix: add explicit transaction boundaries.

### P1 — Architecture & Security
3. **No rate limiting** on any API or WebSocket endpoint
4. **WebSocket message spoofing**: no per-message validation of soul ownership — `soul_update` messages are broadcast without verifying `soul_id` values belong to the authenticated owner
6. **No operator action audit logging** — compromised operator key provides unlimited access with no trail
7. **Client WebSocket protocol detection uses substring matching** (`"test-hub" in url` at `client/system/network/client.py:37`) — URL parsing vulnerability; use `urllib.parse.urlparse()` instead
8. **WebSockets auth tokens never expire** — compromised soul secrets grant indefinite access
9. **Soul secrets stored in plaintext** in the database — should be hashed
10. **No minimum secret strength validation** — users can set weak, guessable secrets
11. **Missing security headers** (CORS, X-Content-Type-Options, HSTS, etc.)
12. **No input sanitization for social content** — potential XSS if content is rendered in HTML context

### P2 — Performance
13. **Client sends position updates every 33ms (30Hz)** — consider reducing to 5Hz (200ms)
14. **`ConnectionManager.broadcast()` is sequential** — should use `asyncio.gather()` with timeouts
15. **O(n²) collision detection per frame** — acceptable for <20 souls; needs spatial hashing for scale
16. **Wasted `pyautogui.screenshot()` thread** in `SoulAgent` — the screenshot is computed but immediately discarded (`img_bytes = None`)
17. **No DB indexes** beyond primary keys — full table scans on `owner_id`, `secret` queries
18. **No connection pooling** — new `sqlite3.Connection` per request (mitigated by WAL mode + busy_timeout)

### P3 — Robustness
19. **JSON bomb vulnerability** — `json.loads()` on `MarketListing.item` has no size/nesting limits
20. **`decision_interval` hardcoded to 3600s** — too infrequent for responsive simulation
21. **Thread pool exhaustion** — unbounded daemon threads spawned for agent decisions

### P4 — Maintenance
22. **Root `main.py` is 0 bytes** — delete the empty file
23. **Pydantic version mismatch** — `server/pyproject.toml` pins `pydantic>=2.12.5` while `client/pyproject.toml` and root `pyproject.toml` have no pydantic dependency; the lockfile may resolve different versions across workspace members
24. **`server/.venv` and `client/.venv`** isolated from workspace — unified via `uv sync --all-packages` from root
25. **`shared/` not a workspace member** — makes cross-package imports fragile
26. **`SoulResponse` missing 21 stat columns** — all stat data silently dropped when returned via FastAPI response_model
27. **`Stat` enum duplicated** in `shared/enums.py` and `client/core/biology/stats.py`
28. **No integration tests** — only unit tests exist; no end-to-end flow coverage
29. **WebSocket protocol is undocumented** — no protocol spec for new client implementations

## Testing Conventions
- Tests in `server/tests/` and `client/tests/` directories
- Use `pytest` with `pytest-asyncio` for async tests
- Test isolation: use in-memory SQLite (`:memory:`) for server tests
- Mock external services (Gemini API, WebSocket hub) in unit tests
- Integration tests should cover full CRUD flows: create soul → list item → buy item → post → reply

## Important Notes for AI Agents
- **Do not modify `server/.venv/` or `client/.venv/`** — these should be deleted; use `uv sync --all-packages` from root for a unified environment
- **Do not use `test-hub` as a localhost check** — use proper URL parsing via `urllib.parse.urlparse()`
- **Do not spawn unbounded threads** — use `concurrent.futures.ThreadPoolExecutor(max_workers=N)` instead
- **Do not send position updates more than 5Hz** — the 33ms broadcast interval should be increased to 200ms
- **Soul secrets are sensitive** — never log them, never include them in client-side broadcast data (use `include_secret=False` for network payloads)
- **SQLite WAL mode** is enabled — concurrent reads are safe but writes still serialize
- **The `shared/` package** is a uv workspace member (src layout, installed editable via `uv sync --all-packages` from the root) — always run through `uv run` (which syncs it), never rely on a stale `.venv`

## Agent skills

### Issue tracker

Issues live in GitHub Issues on github.com/IAmNo1Special/Soulscape (gh CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles using default label strings (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.