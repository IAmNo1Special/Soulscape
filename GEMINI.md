# Soulscape Project Overview

This document provides a high-level overview of the Soulscape project, its architecture, and development conventions.

## 1. Project Purpose

Soulscape is a desktop-integrated creature simulation featuring autonomous "souls" with LLM-driven behaviors. These souls live on your desktop in a transparent, always-on-top window, and their actions are decided by a Gemini large language model. The project has both a client (the desktop application) and a server ("The Hub") component for persistence and multi-user interactions.

## 2. Architecture

The project is a Python application with the following key components:

*   **Desktop Client (`src/soulscape/main.py`):** The main application that runs on the user's desktop. It's built using the `pyglet` library to create the transparent overlay window. It manages the rendering of souls, user input, and communication with the Hub.
*   **Souls (`src/soulscape/core/soul/soul.py`):** The core entities of the simulation. Each soul has a distinct appearance, biology, and physics.
*   **Soul Agent (`src/soulscape/core/soul/agent.py`):** The "brain" of each soul. It uses the Google Agent Development Kit (ADK) with a Gemini model to decide the soul's actions. The agent can perceive its environment (via screenshots), and act using a set of tools (called "spells").
*   **The Hub (`hub/` - mentioned in `README.md`):** A central server that provides services like a marketplace, social message board, and persistence for the souls. This allows for multi-user interactions and for souls to exist across different computers.
*   **Magetools (`magetools.yaml`):** A custom tool that seems to provide the souls with a set of "spells" or abilities that the LLM can use. This includes things like movement and social interactions.

## 3. Technologies Used

*   **Programming Language:** Python 3.11+
*   **Graphics:** `pyglet` for creating the transparent, always-on-top window and rendering.
*   **AI:** `google-genai` and `google-adk` for the LLM-powered agent.
*   **GUI:** `ttkbootstrap` and `pystray` for the settings windows and system tray icon.
*   **Dependencies:** Managed with `uv`. See `pyproject.toml` for a full list.

## 4. Building and Running

### Running the Desktop Client

The desktop client can be run directly using `uv`:

```bash
uv run python src/soulscape/main.py
```

### Running the Hub

The Hub can be run using Docker Compose or manually with `uv`.

**Using Docker (Recommended):**

```bash
docker compose -f hub/docker-compose.yml up --build -d
```

**Manually:**

```bash
uv run python hub/main.py
```

## 5. Development Conventions

*   **Package Management:** The project uses `uv` for package management. To install dependencies, use `uv sync`.
*   **Code Style:** The project uses `black` for code formatting and `isort` for import sorting. Configuration for these tools can be found in `pyproject.toml`.
*   **Linting:** `ruff` is used for linting.
*   **AI Agent Development:** The soul agents are built using the Google Agent Development Kit (ADK). The available tools for the agents seem to be defined through the `magetools` system.
