# Soulscape Client

The **headless offline simulation**. It renders nothing.

Souls are drawn by a separate Godot 4 overlay project (`soulscape-client`).
This process is the authority offline: it ticks the simulation and writes
`souls.json`, which the Godot client polls. The on-disk contract between the
two is documented in
[`../docs/client-contract.md`](../docs/client-contract.md) — read it before
changing anything a renderer depends on.

## What it does

- Ticks souls at 60 Hz: physics, separation, biology, and a GOAP planner.
- Persists souls owned by this `instance_id` to `souls.json` every 5 s, and
  once more on shutdown. Writes are atomic (`os.replace`), so a reader never
  sees a partial document.
- In `online` mode there is nothing to simulate locally — the Hub owns the
  truth — so owned souls are pushed to the Hub once and the process exits.

No display server, no window, no GPU. It runs fine over SSH.

## Running

From the repo root:

```bash
uv run --package client python -m client
```

## Modes

The mode comes from `settings.json` and is never inferred from the
environment:

- `offline` (default) — souls live in `souls.json`, which you can hand-edit.
  A missing or invalid `mode` falls back here.
- `online` — this process is a one-shot sync to the Hub and exits.

`HUB_URL` (or the `hub_url` setting) only sets the Hub address; it does not
switch modes.

## Configuration

`settings.json` and `.env` live in the app-data directory, not the repo:

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\Soulscape\` |
| Linux / macOS | **`CWD/.soulscape/`** |

The Linux path depends on the directory you launched from, because
`APPDATA` is normally unset there. If the Godot client cannot find your souls,
this is almost always why — see the contract doc.

`.env` holds `HUB_URL` and `HUB_SECRET_KEY` (the value sent as the
`X-Hub-Secret` header).

### Roam bounds

`display_width` / `display_height` in `settings.json` set how large a world
souls roam in, and therefore how world coordinates should be interpreted by
the renderer. They are never written automatically — see "Roam bounds
resolution" in the contract doc for why, and for the
`SOULSCAPE_DISPLAY_WIDTH` / `SOULSCAPE_DISPLAY_HEIGHT` override.

Display size is detected on Windows and macOS. Under Wayland it is not:
XWayland reports a virtual screen spanning every monitor rather than a real
one, so the sim logs a warning and falls back to 1920x1080.

## Project structure

- `sim_process.py` — entry point; the offline tick loop
- `core/` — soul, biology, physics, GOAP brain, interactions, stores
- `system/` — persistence, network, logging, presence, command queue
- `ui/` — tkinter/ttkbootstrap dialogs and logic-only visuals
- `ai/` — LLM agent and magetools, currently parked
- `utils/` — helpers and security sanitization
- `constants.py` — simulation and physics constants

## Testing

```bash
uv run pytest client/tests/
```

## Dependencies

`httpx`, `websockets`, `pillow`, `pyautogui`, `ttkbootstrap`,
`python-dotenv`, `goapauto`, and the workspace-local `shared` package.
