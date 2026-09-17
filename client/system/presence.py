"""Tamer presence pipeline, client side (issue #28).

ONLINE-mode only: the pipeline is started by NetworkService, which only
runs in online mode. Offline has no Hub to ship to, so nothing is sampled
or shipped there.

Trust boundary: PresenceRedactor is the SOLE consumer of raw desktop
signals. Its only input channel is the PresenceSampler interface
(last-input age, lock state, coarse foreground category). Exact timestamps,
window titles, paths, URLs, keystroke counts -- none of these can reach the
redactor, because the sampler interface has no methods that return them.
Everything downstream of the redactor sees ONLY the allowlisted payload:

    {presence: active|idle|locked,
     idle_bucket: "0-5"|"5-30"|"30+",
     event?: tamer_return|lock|unlock,
     app_category?: <closed category set>}

"away" is never emitted by the client; the Hub derives it from missed
heartbeats. Keystrokes, content, screenshots, paths, URLs, mic/cam are
NEVER collected -- structurally impossible: no sampler method returns
them and the redactor accepts no other input.

Cadence: the sampler is polled locally every SAMPLER_POLL_SECONDS; the
redacted payload ships upstream as a `tamer_presence` intent on CHANGE or
every HEARTBEAT_SECONDS (60 s), whichever comes first. The tamer's
app-category opt-in lives in client settings (key
`presence_app_category_opt_in`, default OFF); enabling it also requires the
server-side toggle (PUT /presence/app-opt-in) or the Hub rejects the field.
"""

from __future__ import annotations

import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

APP_CATEGORIES = frozenset({"game", "browser", "media", "chat", "work", "other"})

PRESENCE_ACTIVE = "active"
PRESENCE_IDLE = "idle"
PRESENCE_LOCKED = "locked"

BUCKET_0_5 = "0-5"
BUCKET_5_30 = "5-30"
BUCKET_30_PLUS = "30+"

EVENT_TAMER_RETURN = "tamer_return"
EVENT_LOCK = "lock"
EVENT_UNLOCK = "unlock"

SAMPLER_POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 60.0
TAMER_RETURN_ABSENCE_SECONDS = 30 * 60.0

OPT_IN_SETTINGS_KEY = "presence_app_category_opt_in"


class PresenceSampler(ABC):
    """Platform abstraction for raw desktop signals (#10 precedent).

    The ONLY raw-signal surface the redactor may touch. Deliberately
    narrow: last-input age (seconds, float), lock state, and a coarse
    foreground-app category drawn from the closed APP_CATEGORIES set.
    There is intentionally no method for keystrokes, window titles,
    paths, URLs, screenshots, or mic/cam -- those signals do not exist
    in this interface and therefore cannot leak through it.
    """

    @abstractmethod
    def last_input_age_s(self) -> float:
        """Seconds since the last keyboard/mouse input event."""

    @abstractmethod
    def is_locked(self) -> bool:
        """True when the session is locked / on a secure desktop."""

    @abstractmethod
    def foreground_category(self) -> Optional[str]:
        """Coarse category of the foreground app, or None.

        Must return a member of APP_CATEGORIES or None -- never a raw
        process name, title, or path.
        """


_PROCESS_CATEGORIES: dict[str, str] = {
    "chrome.exe": "browser",
    "firefox.exe": "browser",
    "msedge.exe": "browser",
    "brave.exe": "browser",
    "opera.exe": "browser",
    "code.exe": "work",
    "devenv.exe": "work",
    "notepad.exe": "work",
    "winword.exe": "work",
    "excel.exe": "work",
    "powerpnt.exe": "work",
    "outlook.exe": "work",
    "teams.exe": "chat",
    "discord.exe": "chat",
    "slack.exe": "chat",
    "telegram.exe": "chat",
    "whatsapp.exe": "chat",
    "spotify.exe": "media",
    "vlc.exe": "media",
    "wmplayer.exe": "media",
    "netflix.exe": "media",
    "steam.exe": "game",
    "epicgameslauncher.exe": "game",
}


class WindowsPresenceSampler(PresenceSampler):
    """Real sampler using Win32 APIs. Windows-only by construction."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WindowsPresenceSampler requires Windows (win32)")
        import ctypes
        import ctypes.wintypes

        self._ctypes = ctypes
        self._wintypes = ctypes.wintypes
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32

        class _LastInputInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.UINT),
                ("dwTime", ctypes.wintypes.DWORD),
            ]

        self._info_cls = _LastInputInfo
        self._user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
        self._user32.GetLastInputInfo.restype = ctypes.wintypes.BOOL
        self._user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        self._kernel32.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        self._kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE

    def last_input_age_s(self) -> float:
        try:
            info = self._info_cls()
            info.cbSize = self._ctypes.sizeof(self._info_cls)
            if not self._user32.GetLastInputInfo(self._ctypes.byref(info)):
                return 0.0
            now_ms = self._kernel32.GetTickCount64()
            age_ms = (now_ms - info.dwTime) & 0xFFFFFFFFFFFFFFFF
            return max(0.0, age_ms / 1000.0)
        except Exception:
            return 0.0

    def is_locked(self) -> bool:
        try:
            desktop = self._user32.OpenInputDesktop(
                0,
                False,
                0x10000000,  # GENERIC_READ
            )
            if not desktop:
                return True
            self._user32.CloseDesktop(desktop)
            return False
        except Exception:
            return False

    def foreground_category(self) -> Optional[str]:
        try:
            hwnd = self._user32.GetForegroundWindow()
            if not hwnd:
                return None
            pid = self._wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
            handle = self._kernel32.OpenProcess(
                0x1000,
                False,
                pid.value,  # PROCESS_QUERY_LIMITED_INFORMATION
            )
            if not handle:
                return None
            try:
                buf = self._ctypes.create_unicode_buffer(260)
                psapi = self._ctypes.windll.psapi
                if not psapi.GetModuleBaseNameW(handle, None, buf, 260):
                    return None
                name = (buf.value or "").lower()
            finally:
                self._kernel32.CloseHandle(handle)
            return _PROCESS_CATEGORIES.get(name, "other")
        except Exception:
            return None


class NullPresenceSampler(PresenceSampler):
    """Inert sampler: always active, never locked, no category."""

    def last_input_age_s(self) -> float:
        return 0.0

    def is_locked(self) -> bool:
        return False

    def foreground_category(self) -> Optional[str]:
        return None


class FakePresenceSampler(PresenceSampler):
    """Scriptable sampler for tests."""

    def __init__(
        self,
        age_s: float = 0.0,
        locked: bool = False,
        category: Optional[str] = None,
    ) -> None:
        self.age_s = age_s
        self.locked = locked
        self.category = category

    def last_input_age_s(self) -> float:
        return self.age_s

    def is_locked(self) -> bool:
        return self.locked

    def foreground_category(self) -> Optional[str]:
        return self.category


def _idle_bucket(age_s: float) -> str:
    if age_s < 300.0:
        return BUCKET_0_5
    if age_s < 1800.0:
        return BUCKET_5_30
    return BUCKET_30_PLUS


class PresenceRedactor:
    """THE trust boundary. Raw signals enter; only the allowlist leaves.

    Consumes ONLY the PresenceSampler interface. The exact idle age is
    bucketed before it leaves; the foreground category is emitted only
    when the tamer explicitly opted in (default OFF) and only as a
    closed-set member -- anything else is dropped.
    """

    def __init__(
        self,
        sampler: PresenceSampler,
        app_category_opt_in: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sampler = sampler
        self._opt_in = bool(app_category_opt_in)
        self._clock = clock
        self._was_locked = False
        self._absent_since: Optional[float] = None

    def set_app_category_opt_in(self, enabled: bool) -> None:
        self._opt_in = bool(enabled)

    def sample(self) -> dict[str, Any]:
        age_s = self._sampler.last_input_age_s()
        locked = self._sampler.is_locked()
        now = self._clock()

        bucket = _idle_bucket(age_s)
        presence = (
            PRESENCE_LOCKED
            if locked
            else (PRESENCE_ACTIVE if age_s < 300.0 else PRESENCE_IDLE)
        )

        event: Optional[str] = None
        if locked and not self._was_locked:
            event = EVENT_LOCK
            if self._absent_since is None:
                self._absent_since = now
        elif not locked and self._was_locked:
            event = EVENT_UNLOCK
            self._absent_since = None
        elif not locked:
            if presence == PRESENCE_IDLE and bucket == BUCKET_30_PLUS:
                if self._absent_since is None:
                    self._absent_since = now
            elif presence == PRESENCE_ACTIVE:
                if (
                    self._absent_since is not None
                    and now - self._absent_since >= TAMER_RETURN_ABSENCE_SECONDS
                ):
                    event = EVENT_TAMER_RETURN
                self._absent_since = None
        self._was_locked = locked

        payload: dict[str, Any] = {
            "presence": presence,
            "idle_bucket": bucket,
        }
        if event is not None:
            payload["event"] = event
        if self._opt_in:
            category = self._sampler.foreground_category()
            if category in APP_CATEGORIES:
                payload["app_category"] = category
        return payload


class PresencePipeline:
    """Ships redacted presence upstream as `tamer_presence` intents.

    Polls the redactor every `poll_s`; ships on CHANGE or every
    `heartbeat_s`. Runs on a daemon thread; `send` is the network
    service's thread-safe send_intent.
    """

    def __init__(
        self,
        redactor: PresenceRedactor,
        send: Callable[..., Any],
        get_soul_id: Callable[[], str],
        poll_s: float = SAMPLER_POLL_SECONDS,
        heartbeat_s: float = HEARTBEAT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._redactor = redactor
        self._send = send
        self._get_soul_id = get_soul_id
        self._poll_s = poll_s
        self._heartbeat_s = heartbeat_s
        self._clock = clock
        self._last_shipped: Optional[dict[str, Any]] = None
        self._last_ship_at: float = 0.0
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def tick_once(self) -> Optional[dict[str, Any]]:
        """Sample once; ship when changed or heartbeat due. Test seam."""
        payload = self._redactor.sample()
        now = self._clock()
        changed = payload != self._last_shipped
        heartbeat_due = (now - self._last_ship_at) >= self._heartbeat_s
        if not changed and not heartbeat_due:
            return None
        try:
            soul_id = self._get_soul_id() or "tamer"
        except Exception:
            soul_id = "tamer"
        self._send("tamer_presence", soul_id, **payload)
        self._last_shipped = dict(payload)
        self._last_ship_at = now
        return payload

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="presence-pipeline", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while self._running:
            try:
                self.tick_once()
            except Exception:
                pass
            time.sleep(self._poll_s)


def get_app_category_opt_in() -> bool:
    """Client-side opt-in flag (default OFF)."""
    try:
        from .persistence import load_settings

        return bool(load_settings().get(OPT_IN_SETTINGS_KEY, False))
    except Exception:
        return False


def set_app_category_opt_in(enabled: bool) -> bool:
    """Persist the client-side opt-in flag."""
    try:
        from .persistence import load_settings, save_settings

        settings = load_settings()
        settings[OPT_IN_SETTINGS_KEY] = bool(enabled)
        return bool(save_settings(settings))
    except Exception:
        return False


def build_sampler() -> PresenceSampler:
    """Real sampler for this platform, or an inert one elsewhere."""
    if sys.platform == "win32":
        return WindowsPresenceSampler()
    return NullPresenceSampler()
