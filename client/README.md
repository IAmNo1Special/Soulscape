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

1. Navigate to the client directory from the root of the Soulscape project:

   ```bash
   cd client
   ```

1. Synchronize dependencies:

   ```bash
   uv sync
   ```

### Running the Client

To start the overlay client:

```bash
uv run main.py
```

The client will launch a transparent full-screen window overlaying your desktop. You can manage the application through the Windows system tray.

## Configuration

The client uses environment variables for configuration. Create a `.env` file in the client directory:

```env
# Hub connection settings
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
