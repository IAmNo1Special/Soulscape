# Offline client contract (`souls.json` / `settings.json`)

The file boundary between the **headless Python sim** (writer) and the **Godot
overlay client** (reader, the separate `soulscape-client` project). Offline mode
only — online mode never touches these files.

Nothing in the repo documented this before; the schema was implicit in
`Soul.to_dict()` and `SoulBiology.to_flat_dict()`. Everything below was
verified against a live sim run on 2026-10-02.

## Where the files live

Resolved by `client/utils/helpers.py` (`get_appdata_dir`), which is the single
source of truth for persistence, logging and the override:

| Order | Platform | Path |
|---|---|---|
| 1 | any | `$SOULSCAPE_DATA_DIR` |
| 2 | Windows | `%APPDATA%\Soulscape\` |
| 3 | Linux / macOS | `~/.soulscape` |
| 4 | no `HOME` at all | `CWD/.soulscape` |

`APPDATA` is normally unset on Linux and macOS, so those platforms use
`~/.soulscape` — the same path the Godot client resolves — and the two find
each other with no configuration. `SOULSCAPE_DATA_DIR` overrides for both, so a
non-default location is a single setting rather than a per-process argument.

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
| `display_width` | int | — | Explicit override for roam bounds. Never written automatically — see below. |
| `display_height` | int | — | as above |

Unknown keys are preserved across rewrites (`:307-311`), so hand-added
`display_width` / `display_height` survive.

### Roam bounds resolution

`resolve_screen_size()` in `client/sim_process.py` decides how large a world
the soul roams in, and therefore how world coordinates in `souls.json` should
be interpreted by a renderer. Precedence:

1. `display_width` / `display_height` from `settings.json`, if both are ints
2. `SOULSCAPE_DISPLAY_WIDTH` / `SOULSCAPE_DISPLAY_HEIGHT`
3. Real display detection (`_detect_display_size()`)
4. `1920x1080`

Detection covers Windows (`GetSystemMetrics`) and macOS
(`CGMainDisplayPixelsWide/High`). On Linux it is **deliberately skipped under
Wayland** and a warning is logged instead: Godot and the sim both reach X11
through XWayland, whose virtual screen spans every monitor rather than
describing one. On this machine that reports `3200x1080` for two displays of
`800x1280` and `1920x1080`, which would put the roam space out of sync with the
monitor the overlay is actually on. Detection still applies to a native X11
session.

The settings keys are never auto-filled. A key that is present always wins, so
writing a detected value once would freeze a stale measurement — a resolution
or monitor change would then never be picked up. Set them per machine, or use
the environment variables.

The renderer should use the same value. It does read these keys
(`soul_overlay._sim_region()`), falling back to `DisplayServer.screen_get_size()`
— which, for the same XWayland reason, is not the real monitor size.

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

1. ~~**Linux data dir divergence**~~ — resolved. Both sides use `~/.soulscape`
   and honour `SOULSCAPE_DATA_DIR`.
2. **`display_width` / `display_height` are per-machine manual config.** There
   is no automatic producer, by design — see "Roam bounds resolution". Under
   Wayland nothing trustworthy can detect the real monitor size, so the sim
   logs a warning and falls back to 1920x1080, which is wrong on any other
   resolution. A renderer and the sim must agree on this value or the pet is
   drawn in the wrong place.
3. **No schema validation on read.** `load_souls()` checks only that the
   top level is a list (`persistence.py:232-234`).
4. **The 5 s cadence is unversioned.** Consumers must not assume a fixed
   interval.
