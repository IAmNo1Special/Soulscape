"""CI guard: capture libraries stay unreachable from the online path.

Scoping (deliberate):
- The OFFLINE path legitimately keeps screen-capture machinery. The
  parked ADK/Gemini brain (client/ai) processes screenshots for vision,
  and SoulPhysics reads the cursor via pyautogui. This guard does NOT
  ban capture libraries repo-wide.
- The ONLINE path is a viewport to Hub authority: no local sim stepping,
  no brain instantiation, no screenshot capture, no local stores. A
  capture library must be neither imported by nor reachable from the
  online entry modules.

Two checks implement this:
1. AST scan: no capture-library import statement (module-level or
   function-level) anywhere in the online entry modules.
2. Runtime: importing the online modules in a fresh interpreter must not
   place any capture library (or client.ai) in sys.modules.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

CAPTURE_ROOTS = {"pyautogui", "mss", "pyscreenshot", "pyscreeze"}
CAPTURE_FROM_IMPORTS = {("PIL", "ImageGrab")}

ONLINE_ENTRY_MODULES = [
    "client/system/network/viewport_client.py",
    "client/system/network/client.py",
    "client/system/network/presence.py",
    "client/system/network_service.py",
    "client/system/persistence.py",
    "client/core/stores/__init__.py",
    "client/core/stores/remote_store.py",
    "client/core/commands.py",
    "client/main.py",
]

ONLINE_RUNTIME_MODULES = [
    "client.system.network.viewport_client",
    "client.system.network.client",
    "client.system.network.presence",
    "client.system.network_service",
    "client.system.persistence",
    "client.core.stores",
    "client.core.stores.remote_store",
    "client.core.commands",
    "client.core.soul.physics",
    "client.core",
]


def _capture_imports(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in CAPTURE_ROOTS:
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0]
            if root in CAPTURE_ROOTS:
                found.append(module)
            for alias in node.names:
                if (root, alias.name) in CAPTURE_FROM_IMPORTS:
                    found.append(f"{module}.{alias.name}")
    return found


def test_online_entry_modules_have_no_capture_imports():
    offenders: dict[str, list[str]] = {}
    for rel in ONLINE_ENTRY_MODULES:
        path = REPO_ROOT / rel
        assert path.exists(), f"online entry module missing: {rel}"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        hits = _capture_imports(tree)
        if hits:
            offenders[rel] = hits
    assert not offenders, "capture libraries imported on the online path: " + "; ".join(
        f"{m} -> {sorted(h)}" for m, h in offenders.items()
    )


def test_online_path_imports_no_capture_libraries():
    script = (
        "import importlib, sys\n"
        f"modules = {ONLINE_RUNTIME_MODULES!r}\n"
        "roots = {'pyautogui', 'mss', 'pyscreenshot', 'pyscreeze'}\n"
        "[importlib.import_module(m) for m in modules]\n"
        "bad = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m.split('.')[0] in roots or m == 'PIL.ImageGrab'\n"
        ")\n"
        "bad_ai = sorted(\n"
        "    m for m in sys.modules\n"
        "    if m == 'client.ai' or m.startswith('client.ai.')\n"
        ")\n"
        "assert not bad, f'capture libraries reachable: {bad}'\n"
        "assert not bad_ai, f'brain reachable from online path: {bad_ai}'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, "online-path import check failed:\n" + proc.stderr
