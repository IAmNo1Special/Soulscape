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

1. Synchronize the project dependencies:

   ```bash
   uv sync
   ```

### Running the Ecosystem

#### 1. Start the Hub (Server)

Navigate to the `server` directory and run:

```bash
cd server
uv run main.py
```

The Hub will be available at `http://localhost:9785`.

#### 2. Start the Overlay (Client)

Navigate to the `client` directory and run:

```bash
cd client
uv run main.py
```

## Documentation

- **[Connecting Others](./connecting_others.md)**: Guide on how to let others connect to your Hub.
- **[Server README](./server/README.md)**: Detailed documentation for the Hub.
- **[Client README](./client/README.md)**: Detailed documentation for the Overlay.

## License

Distributed under the MIT License.
