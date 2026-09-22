# Soulscape Client

A desktop-integrated overlay application featuring autonomous souls with LLM-driven behaviors. The client renders multiple "souls" directly onto your desktop in a transparent full-screen window.

## Features

- **Transparent Rendering**: Souls live and interact directly on your desktop in a transparent overlay window.
- **System Tray Integration**: Manage your souls and global settings from the Windows system tray.
- **Network Synchronization**: Real-time position and state syncing with the Soulscape Hub.
- **Input Routing**: Natural interaction with overlay entities using an intelligent input router.
- **LLM-Driven Behaviors**: Souls exhibit autonomous behaviors powered by language models.
- **Pyglet-Based Rendering**: High-performance OpenGL rendering for smooth animations.

## Getting Started

### Prerequisites

- **Python**: 3.13 or higher.
- **Package Manager**: [Astral uv](https://docs.astral.sh/uv/) (Required for all operations).
- **Windows**: The client is designed for Windows systems with system tray support.

### Installation

1. Synchronize dependencies from the repo root (`--all-packages`
   is required because the root is a virtual package — a plain
   `uv sync` leaves `.venv` without the members, failing with
   `ModuleNotFoundError`, e.g. `No module named 'pyglet'` / `'shared.env'`):

   ```bash
   uv sync --all-packages
   ```

### Running the Client

To start the overlay client, from the repo root run:

```bash
uv run --package client python -m client
```

The client will launch a transparent full-screen window overlaying your desktop. You can manage the application through the Windows system tray.

## Configuration

The client runs in one of two explicit modes, chosen by the `mode`
setting in `settings.json` (stored under the app-data directory;
see `client/system/persistence.py`). The mode is never inferred from
environment variables:

- `offline` (default) — the local game: souls live in `souls.json`,
  which you can hand-edit. A missing or invalid `mode` falls back
  here.
- `online` — the client is a viewport over the Hub; it does not run
  the local simulation.

To go online, set `"mode": "online"` in `settings.json` and point the
client at your Hub. `HUB_URL` (or the `hub_url` setting) only sets the
Hub address — it does not switch modes. The client loads a `.env`
file from the client directory:

```env
# Hub connection settings (address only; mode comes from settings.json)
HUB_URL=http://localhost:9785
HUB_API_KEY=your_api_key_here

# Client settings
WINDOW_WIDTH=1920
WINDOW_HEIGHT=1080
TRANSPARENCY=0.9
```

## Project Structure

- **core/**: Core simulation logic and soul behavior systems
- **shaders/**: OpenGL shaders for rendering effects
- **system/**: System-level integrations (tray, input routing)
- **ui/**: User interface components and overlays
- **utils/**: Utility functions and helpers
- **main.py**: Entry point for the application
- **constants.py**: Application-wide constants and configuration

## Usage

### System Tray

Right-click the system tray icon to:

- Show/Hide the overlay window
- Access settings
- View soul status
- Exit the application

### Soul Interaction

- Souls move autonomously based on their LLM-driven behaviors
- Click on souls to interact with them
- Use the input router to direct keyboard/mouse input to specific souls

### Network Sync

The client automatically syncs soul state with the Hub when connected. Ensure the Hub is running before starting the client for full functionality.

## Testing

The project uses `pytest` for comprehensive testing.

To run the full test suite:

```bash
uv run pytest
```

## Dependencies

Key dependencies include:

- **Pyglet**: OpenGL rendering and window management
- **Google ADK & GenAI**: LLM integration for soul behaviors
- **PyAutoGUI**: Input automation and routing
- **Pystray**: System tray integration
- **WebSockets**: Real-time communication with the Hub

## License

Distributed under the MIT License. See `LICENSE` for more information.

## Contact

Project Link: [https://github.com/IAmNo1Special/Soulscape](https://github.com/IAmNo1Special/Soulscape)
