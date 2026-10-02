# Offline client contract (`souls.json` / `settings.json`)

The file boundary between the **headless Python sim** (writer) and the **Godot
overlay client** (reader, the separate `soulscape-client` project). Offline mode
only — online mode never touches these files.

Nothing in the repo documented this before; the schema was implicit in
`Soul.to_dict()` and `SoulBiology.to_flat_dict()`. Everything below was
verified against a live sim run on 2026-10-02.

## Where the files live

Resolved by `client/utils/helpers.py:80-90`:

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\Soulscape\` |
| Linux / macOS | **`CWD/.soulscape/`** |

The Linux fallback is `Path.cwd() / ".soulscape"`, **not** `~/.soulscape`,
because `APPDATA` is normally unset there. The data directory therefore depends
on the directory the sim was launched from.

> **Trap.** The Godot client resolves `~/.soulscape` on Linux
> (`file_soul_source.gd`), so the two disagree by default. Run the sim from
> `$HOME`, or point the client at the sim with `SOULSCAPE_DATA_DIR`. Making the
> sim use `~/.soulscape` unconditionally is the real fix and is still open.

Files in the directory, and which to read:

| File | Read by |
|---|---|
| `souls.json` | the client — the only file it parses |
| `settings.json` | the client |
| `.env` | the client (`DotEnv`) |
| `souls.json.bak` | backup copy, ignore |
| `souls.json.corrupt-<stamp>` | quarantined corrupt input, ignore |
| `tmp*` | transient, ignore |
| `logs/` | sim logging |

Match the exact filename; do not glob `souls.json*`.

## `settings.json`

Written by `save_settings()` (`client/system/persistence.py:252-264`) and also
by `load_settings()`, which seeds and backfills defaults (`:267-319`).

| Key | Type | Default | Notes |
|---|---|---|---|
| `mode` | str | `"offline"` | `offline` or `online`; invalid values are rewritten to `offline` (`:302-305`) |
| `instance_id` | str | fresh `uuid4().hex` | Only souls with this `owner_id` are persisted (`sim_process.py:104`) |
| `hub_url` | str | `http://localhost:9785` | Address only — it never switches mode |
| `opacity` | int | `100` | |
| `spawn_hotkey` | str | `ctrl+shift+s` | |
| `display_width` | int | — | **Never written by any Python code.** Read at `sim_process.py:40`; falls back to 1920 (`sim_process.py:34-35`). Producer unassigned. |
| `display_height` | int | — | as above |

Unknown keys are preserved across rewrites (`:307-311`), so hand-added
`display_width` / `display_height` survive.

## `souls.json`

A JSON **array** of flat entry dicts. Written by `save_souls()`
(`persistence.py:145-179`) via `_atomic_write_json()` (`:60-79`), which uses
`tempfile.mkstemp` in the same directory plus `os.replace` (`:71`) — so readers
always see a whole old or new document, never a partial one.

Persisted **every 5 s** (`SAVE_INTERVAL_SECONDS`, `sim_process.py:32`) and once
more on shutdown, including on Ctrl-C (`sim_process.py:150-151`). Consumers must
interpolate between writes or motion will step every 5 s.

### Entry schema

~45 keys. The ones that matter to a renderer:

| Key | Type | Notes |
|---|---|---|
| `soul_id` | str | uuid |
| `owner_id` | str \| null | matches `settings.instance_id` |
| `position` | `[float, float]` | **The only position convention on disk.** Pixel coords |
| `hp` | int | current |
| `max_health` | int | effective pool — note the name |
| `satiety` | float | |
| `hydration` | float | |
| `orb_color` | `[float, float, float]` | 0-1 RGB |
| `aura_color` | `[float, float, float]` | 0-1 RGB |
| `aura_visible` | bool | |
| `activity` | str | e.g. `resting` |
| `level` / `xp` | int | |
| `essence` | float | |
| `inventory` | dict | `{capacity, items[]}` |

Identity and biology: `name`, `first_name`, `family_name`, `species`,
`gender`, `hometown`, `birth_date`, `mother_id`, `father_id`, plus 21 stat keys
— `stat_{hp,atk,def,spa,spd,spe,vis}_{base,iv,ev}` and `nature`.

### Keys that are deliberately absent

| Key | Why |
|---|---|
| `secret` | Stripped before writing by `_strip_secrets()` (`persistence.py:112-114`), even though `sim_process.py:105` builds the entry with `include_secret=True`. Never log or transmit it. |
| `max_hp` | Not on disk. `max_health` is the persisted name; `max_hp` exists only in `create_snapshot()` (`soul.py:331`) and the Hub WebSocket stream (`client/system/network/viewport_client.py:55,63`). A consumer of both transports must accept either. |
| `state` | Only the server and the Hub stream carry it (`server/biology.py:80`, `viewport_client.py:348,374,421,446-448`). The offline sim has no collapsed state machine — HP floors at 0 (`biology.py:240`). Expect no `collapsed` state offline. |
| `x` / `y` | Snapshot-only (`soul.py:325-326`), never persisted. |

### Sibling key names across transports

| Concept | `souls.json` (offline) | `create_snapshot()` / Hub WS |
|---|---|---|
| position | `position` | `x`, `y` |
| max health | `max_health` | `max_hp` |
| collapsed | absent | `state` |

## Offline vs online

`souls.json` is written **only** in offline mode. `save_souls()` branches on
`get_client_mode()` (`persistence.py:153`); in `online` it POSTs to the Hub and
does not touch disk. `sim_process.py:166-173` pushes owned souls to the Hub once
and exits. The Hub is the single source of truth online
(`docs/adr/0001-hub-authoritative-persistent-simulation.md`).

## Open items

1. **Linux data dir divergence** — sim uses `CWD/.soulscape`, the Godot client
   uses `~/.soulscape`. Needs one decision.
2. **`display_width` / `display_height` have no producer.** The sim needs them
   for roam bounds and silently falls back to 1920x1080, which is wrong on any
   other resolution.
3. **No schema validation on read.** `load_souls()` checks only that the
   top level is a list (`persistence.py:232-234`).
4. **The 5 s cadence is unversioned.** Consumers must not assume a fixed
   interval.
