"""Local TOML config for bubbles + noise (issue #30).

Lives next to settings.json in the client app-data dir as
``soulscape_bubbles.toml``. Sections:

[noise]    caps_per_hour, quiet_start/quiet_end (local HH:MM),
           mutes (soul ids), work_mode
[display]  per-kind bubble durations, max visible per soul, queue depth
[exclusions]
           RESERVED. Intended for per-soul exclusions from ambient
           systems (bubbles, presence uplink, reflex visuals). No
           consumer reads this section yet; the loader preserves it
           verbatim so a future issue can adopt it without migration.

Loading is fail-soft: a missing file yields defaults, a corrupt file
yields defaults, and any single bad value falls back to its default
while the rest of the file still applies.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.helpers import get_appdata_dir
from .noise import NoiseSettings

CONFIG_FILENAME = "soulscape_bubbles.toml"

DEFAULT_DURATIONS = {
    "speech": 6.0,
    "quip": 8.0,
    "system": 5.0,
    "greeting": 5.0,
    # Issue #32: soul questions for the tamer stay up longer -- the
    # tamer may need a moment to notice and open the mailbag.
    "mailbag": 10.0,
    # Issue #33: the morning note carries up to 3 lines of overnight
    # highlights; give the tamer time to read it.
    "morning_note": 10.0,
    # Issue #35: expedition farewell/welcome bubbles are cinematic
    # moments -- long enough to read, short enough to stay transient.
    "expedition": 6.0,
}


@dataclass
class DisplaySettings:
    durations: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_DURATIONS)
    )
    max_visible_per_soul: int = 1
    queue_depth: int = 3


@dataclass
class BubbleConfig:
    noise: NoiseSettings = field(default_factory=NoiseSettings)
    display: DisplaySettings = field(default_factory=DisplaySettings)
    exclusions: dict[str, Any] = field(default_factory=dict)


def get_bubble_config_file() -> Path:
    return get_appdata_dir() / CONFIG_FILENAME


def _as_int(value: Any, default: int, lo: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and int(value) >= lo:
        return int(value)
    return default


def _as_float(value: Any, default: float, lo: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and float(value) > lo:
        return float(value)
    return default


def _as_hhmm(value: Any, default: str) -> str:
    if not isinstance(value, str):
        return default
    parts = value.strip().split(":")
    if len(parts) != 2:
        return default
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return default
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return default


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value if isinstance(v, (str, int))]


def _noise_from(data: dict[str, Any]) -> NoiseSettings:
    return NoiseSettings(
        caps_per_hour=_as_int(data.get("caps_per_hour"), 4),
        quiet_start=_as_hhmm(data.get("quiet_start"), "22:00"),
        quiet_end=_as_hhmm(data.get("quiet_end"), "07:00"),
        mutes=frozenset(_as_str_list(data.get("mutes"))),
        work_mode=bool(data.get("work_mode", False)),
    )


def _display_from(data: dict[str, Any]) -> DisplaySettings:
    durations = dict(DEFAULT_DURATIONS)
    raw = data.get("durations")
    if isinstance(raw, dict):
        for kind, default in DEFAULT_DURATIONS.items():
            if kind in raw:
                durations[kind] = _as_float(raw[kind], default)
    return DisplaySettings(
        durations=durations,
        max_visible_per_soul=_as_int(data.get("max_visible_per_soul"), 1, lo=1),
        queue_depth=_as_int(data.get("queue_depth"), 3, lo=1),
    )


def load_bubble_config(path: Path | None = None) -> BubbleConfig:
    path = path or get_bubble_config_file()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return BubbleConfig()
    if not isinstance(raw, dict):
        return BubbleConfig()
    noise_raw = raw.get("noise")
    display_raw = raw.get("display")
    exclusions_raw = raw.get("exclusions")
    return BubbleConfig(
        noise=_noise_from(noise_raw) if isinstance(noise_raw, dict) else NoiseSettings(),
        display=_display_from(display_raw)
        if isinstance(display_raw, dict)
        else DisplaySettings(),
        exclusions=dict(exclusions_raw)
        if isinstance(exclusions_raw, dict)
        else {},
    )


def _dump_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_dump_value(v) for v in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_dump_value(v)}" for k, v in value.items())
        return "{ " + inner + " }"
    raise TypeError(f"cannot serialize {type(value)}")


def save_bubble_config(config: BubbleConfig, path: Path | None = None) -> Path:
    path = path or get_bubble_config_file()
    lines = [
        "# Soulscape bubble + noise config (issue #30).",
        "# [exclusions] is reserved for per-soul exclusions from ambient",
        "# systems; no consumer reads it yet.",
        "",
        "[noise]",
        f"caps_per_hour = {_dump_value(config.noise.caps_per_hour)}",
        f"quiet_start = {_dump_value(config.noise.quiet_start)}",
        f"quiet_end = {_dump_value(config.noise.quiet_end)}",
        f"mutes = {_dump_value(sorted(config.noise.mutes))}",
        f"work_mode = {_dump_value(config.noise.work_mode)}",
        "",
        "[display]",
        f"durations = {_dump_value(config.display.durations)}",
        f"max_visible_per_soul = {_dump_value(config.display.max_visible_per_soul)}",
        f"queue_depth = {_dump_value(config.display.queue_depth)}",
        "",
        "[exclusions]",
    ]
    for key, value in config.exclusions.items():
        lines.append(f"{key} = {_dump_value(value)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
