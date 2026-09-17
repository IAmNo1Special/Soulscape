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
import time
from collections.abc import Callable
from dataclasses import dataclass

from shared import protocol

from ..persistence import MODE_ONLINE, get_client_mode

INTERP_DELAY_SECONDS = 0.2
MAX_EXTRAPOLATE_SECONDS = 0.4
MAX_BUFFER_SAMPLES = 32
_MAX_TURN_RATE = 2.0 * math.pi

#: Hub soul state that renders as a statue (issue #21).
STATUE_STATE = "collapsed"
_NORMAL_STATE = "normal"

#: Seconds without a Hub sample before a soul's presence reads stale
#: (issue #29: the "offline" statue variant). Well above the 400 ms
#: dead-reckoning cap so live souls never flicker.
STALE_THRESHOLD_SECONDS = 2.0

#: Dim factor applied to the desaturated statue color.
_STATUE_DIM = 0.72

_BIO_DEFAULTS = {"satiety": 100.0, "hydration": 100.0, "hp": 100.0, "max_hp": 100.0}


def _bio_from_entry(entry: dict) -> dict[str, float]:
    return {
        "satiety": float(entry.get("satiety", 100.0)),
        "hydration": float(entry.get("hydration", 100.0)),
        "hp": float(entry.get("hp", 100.0)),
        "max_hp": float(entry.get("max_hp", 100.0)),
    }

#: Warm tint for dormant statues (issue #22): amber-shifted stone so a
#: frozen (unfunded) soul is distinguishable from a collapsed one.
#: Cheap distinction -- same desaturation, different hue.
_DORMANT_TINT = (1.0, 0.78, 0.45)


def is_statue(state: str | None) -> bool:
    """Whether a Hub soul state renders as a statue."""
    return (state or _NORMAL_STATE) == STATUE_STATE


def statue_orb_color(
    orb_color: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Desaturated, dimmed stone color for a collapsed soul.

    The arch's visual language maps state -> shader uniforms; the statue
    reuses the existing base_color_uniform with saturation removed.
    """
    r, g, b = orb_color
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    dimmed = lum * _STATUE_DIM
    return (dimmed, dimmed, dimmed)


def dormant_statue_orb_color(
    orb_color: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Amber-tinted stone for a dormant (unfunded) soul (issue #22).

    Same desaturated treatment as a collapsed statue, shifted warm so
    the two freeze states are visually distinguishable at a glance.
    """
    r, g, b = orb_color
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    dimmed = lum * _STATUE_DIM
    tr, tg, tb = _DORMANT_TINT
    return (dimmed * tr, dimmed * tg, dimmed * tb)


def viewport_mode_enabled() -> bool:
    """Viewport mode is the explicit online client mode from settings."""
    return get_client_mode() == MODE_ONLINE


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
        # Issue #29: last sample time, for the presence-staleness check
        # ("offline" statue variant).
        self.last_t: float | None = None

    def snap(self, x: float, y: float, now: float) -> None:
        """Clear history and anchor at the new position instantly."""
        self.samples.clear()
        self.samples.append(_Sample(now, x, y))
        self.last_t = now

    def push(self, x: float, y: float, now: float) -> None:
        """Append a streamed position sample."""
        if self.samples and now < self.samples[-1].t:
            self.snap(x, y, now)
            return
        self.samples.append(_Sample(now, x, y))
        self.last_t = now

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
        self._states: dict[str, str] = {}
        # Dormancy (issue #22): wallet-derived freeze, orthogonal to the
        # lifecycle state. Streams on its own delta domain + snapshot.
        self._dormant: dict[str, bool] = {}
        # Biology (issue #30, step 0 of #29): authoritative satiety /
        # hydration / hp so online shader uniforms stop assuming healthy
        # local defaults. Streams on its own delta domain + snapshot.
        self._bio: dict[str, dict[str, float]] = {}
        # Wallets (issue #30): per-soul essence for the tray dashboard.
        # Streams on the economy delta domain + the snapshot's wallets.
        self._wallets: dict[str, float] = {}
        # Transient bubble ops (issue #30): queued here, drained by the
        # app into the BubbleManager. Never part of the entity state.
        self._bubble_queue: collections.deque[dict] = collections.deque()
        self._region: tuple[float, float, float, float] | None = None

    @property
    def region(self) -> tuple[float, float, float, float] | None:
        """Latest world region from the Hub snapshot, if any."""
        return self._region

    def soul_ids(self) -> list[str]:
        """Soul ids currently tracked by the consumer."""
        return list(self._tracks)

    def soul_state(self, soul_id: str) -> str:
        """Latest Hub lifecycle state for a soul ('normal' when unknown)."""
        return self._states.get(soul_id, _NORMAL_STATE)

    def soul_states(self) -> dict[str, str]:
        """Snapshot of per-soul lifecycle states for the render path."""
        return dict(self._states)

    def is_dormant(self, soul_id: str) -> bool:
        """Whether the soul is frozen (unfunded) per the Hub (issue #22)."""
        return self._dormant.get(soul_id, False)

    def is_stale(self, soul_id: str, now: float | None = None) -> bool:
        """Whether the soul's Hub presence is stale/unknown (issue #29).

        True when no sample arrived within STALE_THRESHOLD_SECONDS --
        the "offline" statue variant. Unknown souls read as stale.
        """
        track = self._tracks.get(soul_id)
        if track is None or track.last_t is None:
            return True
        moment = self._clock() if now is None else now
        return (moment - track.last_t) > STALE_THRESHOLD_SECONDS

    def dormant_souls(self) -> dict[str, bool]:
        """Snapshot of per-soul dormancy for the render path."""
        return dict(self._dormant)

    def soul_biology(self, soul_id: str) -> dict[str, float]:
        """Authoritative biology for a soul (issue #30, step 0 of #29).

        Healthy defaults when the Hub has not streamed values yet --
        the same values the client used to assume locally.
        """
        return dict(self._bio.get(soul_id, _BIO_DEFAULTS))

    def soul_essence(self, soul_id: str) -> float | None:
        """Latest streamed essence for a soul (issue #30 tray wallet).

        None when no economy op / snapshot wallet has arrived yet.
        """
        return self._wallets.get(soul_id)

    def wallets(self) -> dict[str, float]:
        """Snapshot of per-soul essence for the tray dashboard."""
        return dict(self._wallets)

    def drain_bubbles(self) -> list[dict]:
        """Take queued transient bubble ops (issue #30).

        The app feeds these into the BubbleManager; the consumer never
        replays them (no resume, no snapshot).
        """
        out = list(self._bubble_queue)
        self._bubble_queue.clear()
        return out

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
            self._states[sid] = entry.get("state") or _NORMAL_STATE
            # Dormancy rides the snapshot (issue #22) so a fresh client
            # renders frozen statues without waiting for a delta.
            self._dormant[sid] = bool(entry.get("dormant", False))
            # Biology rides the snapshot (issue #30) so a fresh client
            # renders uniforms from authoritative values immediately.
            self._bio[sid] = _bio_from_entry(entry)
        for sid in [key for key in self._tracks if key not in seen]:
            del self._tracks[sid]
            self._states.pop(sid, None)
            self._dormant.pop(sid, None)
            self._bio.pop(sid, None)
            self._wallets.pop(sid, None)
        for wallet in frame.get("wallets") or []:
            wid = wallet.get("soul_id")
            if wid:
                self._wallets[wid] = float(wallet.get("essence", 0.0))
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
                self._states.pop(sid, None)
                self._dormant.pop(sid, None)
                self._bio.pop(sid, None)
                self._wallets.pop(sid, None)
                continue
            # Transient bubble ops (issue #30): queue for the app, never
            # touch the entity state model.
            if kind == "bubble":
                self._bubble_queue.append(
                    {
                        "soul_id": sid,
                        "text": str(op.get("text", ""))[:280],
                        "kind": str(op.get("kind", "speech")),
                        "solicited": bool(op.get("solicited", False)),
                    }
                )
                continue
            if kind != protocol.EntityOpKind.UPSERT.value:
                continue
            state = op.get("state") or {}
            # State-only ops (e.g. the issue-#21 statue stream) carry no
            # position; record the lifecycle state either way.
            if "state" in state:
                self._states[sid] = state["state"] or _NORMAL_STATE
            # Dormancy ops (issue #22) ride their own domain, orthogonal
            # to the lifecycle state.
            if op.get("domain") == "dormancy":
                self._dormant[sid] = bool(state.get("dormant", False))
            # Biology ops (issue #30): authoritative vitals for the
            # shader uniforms, orthogonal to position/state.
            if op.get("domain") == "biology":
                self._bio[sid] = _bio_from_entry(state)
            # Economy ops carry the per-soul essence for the tray wallet.
            if op.get("domain") == "economy" and "essence" in state:
                self._wallets[sid] = float(state["essence"])
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

    def screen_to_world(
        self, sx: float, sy: float, monitor_w: float, monitor_h: float
    ) -> tuple[float, float]:
        """Inverse of world_to_screen: monitor pixels to Hub world coords."""
        vx, vy, vw, vh = self.visible_world(monitor_w, monitor_h)
        return (
            vx + sx / monitor_w * vw,
            vy + sy / monitor_h * vh,
        )
