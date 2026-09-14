# Parked AI brain (LLM / magetools)

Core dev continues WITHOUT this code. Nothing under `client/core/`,
`client/system/`, `client/ui/`, `server/` or `shared/` imports from here,
and the default test suite ignores this directory.

## What lives here

| Path | Origin | Notes |
|---|---|---|
| `agent.py` | `client/core/soul/agent.py` | `SoulAgent` (Gemini via google-adk + magetools `Grimorium`). Moved with history via `git mv`. |
| `spells/` | `client/core/soul/.magetools/` | 8 spell modules + `manifest.json` / `grimorium_summary.md` per area. |
| `magetools.yaml` | `client/magetools.yaml` | Magetools config; `magetools_dir_name` still points at the old `core/soul/.magetools` path — stale until plug-back. |
| `tests/` | `client/tests/integration/test_agent_loop.py`, `test_queued_spells.py` | Spell-path lookups rewritten to `client/ai/spells/`. Local `conftest.py` carries the fixtures they need. |

Deleted, not moved: `client/core/soul/.magetools/.chroma_db/` (regenerable
Chroma data, gitignored) and all `__pycache__` dirs.

## Backup

Pre-extraction state: branch `park/ai-brain-v1`, tag `ai-brain-v1`.

## Removed dependencies (to restore on plug-back)

Pinned versions at removal time (`uv tree --package client`):

- `google-adk==1.25.0`
- `google-genai==1.63.0`
- `magetools[full]==1.3.0`
- `numpy==2.4.2` (nothing in core imports it; it came in via magetools → chromadb)

Kept on purpose: `pillow` (used by `client/system/tray.py`) and
`pyautogui` (used by `client/core/soul/physics.py`).

Retired env var: `SOUL_DECISION_INTERVAL` (lived on `SoulAgent`; no core
references remain).

## Running the parked tests (opt-in rot check)

Default runs never collect this directory. Explicit path still works:

```bash
py -m pytest client/ai -q
```

Note: `test_agent_loop.py` exercises the full agent and only passes once
the brain is plugged back (see below).

## Plug-back procedure

1. Rebuild the spell index first: run whatever `Grimorium` init the
   magetools version you restore documents (this recreates `.chroma_db/`;
   paste the exact command here when known).
2. Restore the deps: re-add `google-adk`, `google-genai`,
   `magetools[full]` (+ `numpy` if wanted) to `client/pyproject.toml`,
   then `uv lock`.
3. Move the code back:
   ```bash
   git mv client/ai/agent.py client/core/soul/agent.py
   git mv client/ai/spells client/core/soul/.magetools
   git mv client/ai/magetools.yaml client/magetools.yaml
   git mv client/ai/tests/test_agent_loop.py client/tests/integration/
   git mv client/ai/tests/test_queued_spells.py client/tests/integration/
   ```
   (If the moves were committed instead, use the same commands with the
   current paths, or `git revert` the extraction.)
4. Verify BEFORE touching core: confirm magetools `Grimorium` discovers
   spells at `client/core/soul/.magetools/` (rewrite `manifest.json` root
   paths if the lib expects a different layout) and that
   `magetools.yaml: magetools_dir_name` matches again.
5. Rewire core (reverse of the extraction): restore
   `from .agent import SoulAgent` in `client/core/soul/soul.py`, the spawn
   block in `Soul.__init__`, the `SoulAgent` exports in both `__init__.py`
   files, the `mock_grimorium`/`mock_vision` fixtures in
   `client/tests/conftest.py`, and the `test_soul.py` agent assertions.
   (Use `git show park/ai-brain-v1 -- <file>` / `git diff` against the
   branch to recover exact code.)
6. Run `py -m pytest client/ai -q` (opt-in), then the full suite + ruff.
