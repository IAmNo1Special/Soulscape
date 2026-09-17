"""Client viewport consumer for hub-authoritative rendering (issue #13).

Pyglet-independent: consumes Hub viewport SNAPSHOT/DELTA frames, keeps a
per-Soul timestamped position buffer, and renders positions at
``now - INTERP_DELAY_SECONDS`` with linear interpolation between samples,
arc dead-reckoning past the buffer edge (capped), and snap-op buffer
clearing. Also maps the Hub world region onto the monitor with a single
uniform scale, expanding the visible world on the slack axis.
"""

from __future__ import annotations

import collections
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from shared import protocol

INTERP_DELAY_SECONDS = 0.2
MAX_EXTRAPOLATE_SECONDS = 0.4
MAX_BUFFER_SAMPLES = 32
_MAX_TURN_RATE = 2.0 * math.pi


def viewport_mode_enabled() -> bool:
    """Viewport mode is active only with the flag set AND a Hub URL."""
    flag = os.getenv("HUB_AUTHORITATIVE", "").lower() in ("1", "true", "yes")
    return flag and bool(os.getenv("HUB_URL"))


@dataclass
class _Sample:
    t: float
    x: float
    y: float


def _wrap_angle(delta: float) -> float:
    while delta > math.pi:
        delta -= 2.0 * math.pi
    while delta < -math.pi:
        delta += 2.0 * math.pi
    return delta


class SoulTrack:
    """Timestamped position history for one Soul with render sampling."""

    def __init__(self) -> None:
        self.samples: collections.deque[_Sample] = collections.deque(
            maxlen=MAX_BUFFER_SAMPLES
        )

    def snap(self, x: float, y: float, now: float) -> None:
        """Clear history and anchor at the new position instantly."""
        self.samples.clear()
        self.samples.append(_Sample(now, x, y))

    def push(self, x: float, y: float, now: float) -> None:
        """Append a streamed position sample."""
        if self.samples and now < self.samples[-1].t:
            self.snap(x, y, now)
            return
        self.samples.append(_Sample(now, x, y))

    def position_at(self, t: float) -> tuple[float, float] | None:
        """Render position at time t: interpolate, dead-reckon, or hold."""
        if not self.samples:
            return None
        if len(self.samples) == 1:
            only = self.samples[0]
            return (only.x, only.y)
        first = self.samples[0]
        last = self.samples[-1]
        if t <= first.t:
            return (first.x, first.y)
        if t >= last.t:
            return self._extrapolate(t)
        prev = first
        for cur in list(self.samples)[1:]:
            if cur.t >= t:
                span = cur.t - prev.t
                if span <= 0.0:
                    return (cur.x, cur.y)
                frac = (t - prev.t) / span
                return (
                    prev.x + (cur.x - prev.x) * frac,
                    prev.y + (cur.y - prev.y) * frac,
                )
            prev = cur
        return (last.x, last.y)

    def _extrapolate(self, t: float) -> tuple[float, float]:
        """Arc dead-reckoning from recent motion, capped at 400 ms."""
        last = self.samples[-1]
        dt = min(t - last.t, MAX_EXTRAPOLATE_SECONDS)
        if dt <= 0.0:
            return (last.x, last.y)
        pts = list(self.samples)
        prev = pts[-2]
        seg_dt = last.t - prev.t
        if seg_dt <= 0.0:
            return (last.x, last.y)
        vx = (last.x - prev.x) / seg_dt
        vy = (last.y - prev.y) / seg_dt
        speed = math.hypot(vx, vy)
        heading = math.atan2(vy, vx)
        omega = 0.0
        if speed > 0.0 and len(pts) >= 3:
            older = pts[-3]
            old_dt = prev.t - older.t
            if old_dt > 0.0:
                old_heading = math.atan2(prev.y - older.y, prev.x - older.x)
                turn = _wrap_angle(heading - old_heading)
                mid_old = (older.t + prev.t) / 2.0
                mid_new = (prev.t + last.t) / 2.0
                if mid_new > mid_old:
                    omega = max(
                        -_MAX_TURN_RATE,
                        min(_MAX_TURN_RATE, turn / (mid_new - mid_old)),
                    )
        if speed <= 0.0 or abs(omega) < 1e-9:
            return (last.x + vx * dt, last.y + vy * dt)
        bend = omega * dt
        radius = speed / omega
        return (
            last.x + radius * (math.sin(heading + bend) - math.sin(heading)),
            last.y + radius * (math.cos(heading) - math.cos(heading + bend)),
        )


class ViewportConsumer:
    """Consumes Hub viewport frames into per-Soul interpolation buffers."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._tracks: dict[str, SoulTrack] = {}
        self._region: tuple[float, float, float, float] | None = None

    @property
    def region(self) -> tuple[float, float, float, float] | None:
        """Latest world region from the Hub snapshot, if any."""
        return self._region

    def soul_ids(self) -> list[str]:
        """Soul ids currently tracked by the consumer."""
        return list(self._tracks)

    def apply_frame(self, frame: dict) -> None:
        """Apply a Hub SNAPSHOT or DELTA frame using the consumer clock."""
        msg_type = frame.get("type")
        now = self._clock()
        if msg_type == protocol.MessageType.SNAPSHOT.value:
            self._apply_snapshot(frame, now)
        elif msg_type == protocol.MessageType.DELTA.value:
            self._apply_delta(frame, now)

    def _apply_snapshot(self, frame: dict, now: float) -> None:
        seen: set[str] = set()
        for entry in frame.get("souls") or []:
            sid = entry.get("soul_id")
            if not sid:
                continue
            seen.add(sid)
            self._track(sid).snap(
                float(entry.get("x", 0.0)), float(entry.get("y", 0.0)), now
            )
        for sid in [key for key in self._tracks if key not in seen]:
            del self._tracks[sid]
        region = frame.get("region")
        if isinstance(region, dict):
            w = float(region.get("w", 0.0))
            h = float(region.get("h", 0.0))
            if w > 0.0 and h > 0.0:
                self._region = (
                    float(region.get("x", 0.0)),
                    float(region.get("y", 0.0)),
                    w,
                    h,
                )

    def _apply_delta(self, frame: dict, now: float) -> None:
        for op in frame.get("ops") or []:
            kind = op.get("op")
            sid = op.get("soul_id")
            if not sid:
                continue
            if kind == protocol.EntityOpKind.REMOVE.value:
                self._tracks.pop(sid, None)
                continue
            if kind != protocol.EntityOpKind.UPSERT.value:
                continue
            state = op.get("state") or {}
            if "x" not in state or "y" not in state:
                continue
            track = self._track(sid)
            x = float(state["x"])
            y = float(state["y"])
            if op.get("snap"):
                track.snap(x, y, now)
            else:
                track.push(x, y, now)

    def _track(self, soul_id: str) -> SoulTrack:
        track = self._tracks.get(soul_id)
        if track is None:
            track = SoulTrack()
            self._tracks[soul_id] = track
        return track

    def rendered_positions(
        self, now: float | None = None
    ) -> dict[str, tuple[float, float]]:
        """Interpolated Soul positions rendered at now minus 200 ms."""
        base = now if now is not None else self._clock()
        moment = base - INTERP_DELAY_SECONDS
        out: dict[str, tuple[float, float]] = {}
        for sid, track in self._tracks.items():
            pos = track.position_at(moment)
            if pos is not None:
                out[sid] = pos
        return out


class ViewportMapper:
    """Uniform-scale plot-to-monitor mapping with slack-axis world expansion."""

    def __init__(self) -> None:
        self.region: tuple[float, float, float, float] | None = None

    def set_region(self, x: float, y: float, w: float, h: float) -> None:
        """Set the Hub world region the map is anchored to."""
        self.region = (x, y, w, h)

    def reset(self) -> None:
        """Drop the Hub region; mapping falls back to monitor bounds."""
        self.region = None

    def visible_world(
        self, monitor_w: float, monitor_h: float
    ) -> tuple[float, float, float, float]:
        """World rect visible on the monitor: region fitted with one scale.

        The slack axis keeps the monitor's aspect by showing adjacent world
        instead of bars.
        """
        if self.region is None or self.region[2] <= 0 or self.region[3] <= 0:
            return (0.0, 0.0, float(monitor_w), float(monitor_h))
        rx, ry, rw, rh = self.region
        scale = min(monitor_w / rw, monitor_h / rh)
        vis_w = monitor_w / scale
        vis_h = monitor_h / scale
        return (
            rx - (vis_w - rw) / 2.0,
            ry - (vis_h - rh) / 2.0,
            vis_w,
            vis_h,
        )

    def world_to_screen(
        self, wx: float, wy: float, monitor_w: float, monitor_h: float
    ) -> tuple[float, float]:
        """Map a Hub world position to monitor pixels."""
        vx, vy, vw, vh = self.visible_world(monitor_w, monitor_h)
        return (
            (wx - vx) / vw * monitor_w,
            (wy - vy) / vh * monitor_h,
        )
