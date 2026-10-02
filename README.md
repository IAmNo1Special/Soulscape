# Soulscape

Soulscape is a decentralized metaverse ecosystem: a Godot-rendered desktop
overlay, a headless Python simulation for offline play, and a FastAPI backend
hub. The hub is the single source of truth online; local disk
(`souls.json`) is the authority offline.

## Project structure

- **[client/](client/)** — the **headless** offline simulation. Ticks souls at
  60 Hz and writes `souls.json`. Renders nothing.
- **[server/](server/)** — the Hub: a FastAPI backend for soul persistence,
  social feeds, and a marketplace with tax collection.
- **[shared/](shared/)** — models and enums used by both.
- **Rendering** lives in a separate Godot 4 project,
  [`soulscape-client`](https://github.com/IAmNo1Special/soulscape-client),
  which polls `souls.json` and draws the souls in a transparent, always-on-top,
  click-through window.

The file boundary between the simulation and the Godot client is specified in
[`docs/client-contract.md`](docs/client-contract.md).

## Features

### Overlay (Godot)

- **Desktop pet**: a borderless, transparent, always-on-top window covering
  the monitor, passing clicks through to whatever is underneath.
- **Live soul state**: position, health, satiety, hydration and colours,
  interpolated between the sim's writes.
- **Hub viewport**: in online mode the overlay renders the Hub's WebSocket
  stream instead of the local file.

### Soulscape Hub (server)

- **Soul Persistence**: securely stores soul stats, nature, and inventory.
- **Marketplace**: automated trading with "Essence Fund" tax collection.
- **Social Feed**: threaded social interaction for souls and operators.
- **Multiplayer Integration**: multiple users can connect to a single Hub.

## Getting started

### Prerequisites

- **Python**: 3.13 or higher.
- **Package Manager**: [Astral uv](https://docs.astral.sh/uv/) (required for
  all operations).

### Installation

Synchronize the project dependencies (`--all-packages` is required — the root
is a virtual package, so a plain `uv sync` leaves `.venv` without the
workspace members):

```bash
uv sync --all-packages
```

### Running the ecosystem

#### 1. Start the Hub (server)

The Hub needs two processes: the API and the simulation. From the repo root,
run one in each terminal:

```bash
uv run --package server python -m server
uv run --package server python -m server.sim_process
```

The Hub will be available at `http://localhost:9785`.

The SimProcess is required, not optional: without it the Hub starts in a
degraded state and every intent, marketplace, and social action returns
`503 Simulation unavailable` until the SimProcess is up. (Docker Compose
starts both processes for you; see [server/README.md](server/README.md).)

#### 2. Start the offline simulation (client)

From the repo root:

```bash
uv run --package client python -m client
```

This writes `souls.json` into the app-data directory and keeps running. It
opens no window.

#### 3. Start the overlay

Clone and run the Godot project separately:

```bash
godot --path soulscape-client
```

Point it at the simulation's data directory if they do not already agree —
on Linux the sim writes to `CWD/.soulscape` while the overlay looks in
`~/.soulscape`. See the contract doc.

## Documentation

- **[Client contract](docs/client-contract.md)**: the `souls.json` /
  `settings.json` schema the overlay reads.
- **[Connecting Others](connecting_others.md)**: how to let others connect to
  your Hub.
- **[Client README](client/README.md)**: the offline simulation.
- **[Server README](server/README.md)**: the Hub.
