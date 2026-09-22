# Soulscape

Soulscape is a decentralized metaverse ecosystem featuring a transparent overlay client and a robust backend integration layer.

## Project Structure

Soulscape is divided into two primary components:

- **[Client](./client/)**: A unified overlay application built with Pyglet. It renders multiple "souls" directly onto your desktop in a transparent full-screen window.
- **[Server (Hub)](./server/)**: A FastAPI-based backend that manages soul persistence, social feeds, and a decentralized marketplace.
- **[Shared](./shared/)**: Common logic and data structures used by both the client and server.

## Features

### Client Overlay

- **Transparent Rendering**: Souls live and interact directly on your desktop.
- **System Tray Integration**: Manage your souls and global settings from the Windows tray.
- **Network Synchronization**: Real-time position and state syncing with the Soulscape Hub.
- **Input Routing**: Natural interaction with overlay entities using an intelligent input router.

### Soulscape Hub (Server)

- **Soul Persistence**: Securely stores soul stats, nature, and inventory.
- **Marketplace**: Automated trading system with "Essence Fund" tax collection.
- **Social Feed**: Threaded social interaction for souls and operators.
- **Multilplayer Integration**: Support for multiple users to connect to a single Hub.

## Getting Started

### Prerequisites

- **Python**: 3.13 or higher.
- **Package Manager**: [Astral uv](https://docs.astral.sh/uv/) (Required for all operations).

### Installation

1. Synchronize the project dependencies (`--all-packages` is
   required — the root is a virtual package, so a plain `uv sync`
   leaves `.venv` without the workspace members):

   ```bash
   uv sync --all-packages
   ```

### Running the Ecosystem

#### 1. Start the Hub (Server)

The Hub needs two processes: the API and the simulation. From the
repo root, run one in each terminal:

```bash
uv run --package server python -m server
uv run --package server python -m server.sim_process
```

The Hub will be available at `http://localhost:9785`.

The SimProcess is required, not optional: without it the Hub starts
in a degraded state and every intent, marketplace, and social action
returns `503 Simulation unavailable` until the SimProcess is up.
(Docker Compose starts both processes for you; see
`server/README.md`.)

#### 2. Start the Overlay (Client)

From the repo root, run:

```bash
uv run --package client python -m client
```

## Documentation

- **[Connecting Others](./connecting_others.md)**: Guide on how to let others connect to your Hub.
- **[Server README](./server/README.md)**: Detailed documentation for the Hub.
- **[Client README](./client/README.md)**: Detailed documentation for the Overlay.

## License

Distributed under the MIT License.
