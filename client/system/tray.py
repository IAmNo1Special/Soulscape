"""System tray controller for Soulscape using pystray.

Issue #30 grows the original menu into a dashboard:

- Status submenu: soul count + Hub connection state (dynamic labels).
- Wallet submenu: per-soul essence streamed from the Hub viewport
  (economy ops / snapshot wallets).
- Mailbag: badge slot showing the count. #32 (mailbag) is not done, so
  the count is 0 and the item is labeled "not yet" -- the slot exists,
  the count wires up in #32.
- Whereabouts submenu: per-soul location string (plot label from Hub
  world coords, "traveling..." for traveling souls, "home" when no
  position is known). Richer abroad strings arrive with #35.
- Quips submenu: per-soul "Ask <name> for a quip" -- hits the Hub quip
  endpoint; rejections (budget / essence) surface as a tray
  notification AND a system bubble.
- Market: no client surface exists yet, so the item is present but
  disabled and labeled. Social opens the message-board window (the
  existing social surface).
- Pause simulation toggle: pauses local sim/reflex visuals.
- Work mode toggle: suppresses unsolicited bubbles + dims visuals.
- Settings... and Exit Soulscape as before.

All dashboard data arrives through injected provider callables, so the
menu builds and every callback are unit-testable headless (mock
pystray in sys.modules before importing this module).
"""

from __future__ import annotations

import threading
from typing import Any, Callable

import pystray
from PIL import Image, ImageDraw
from pystray import MenuItem as Item

SoulInfo = dict[str, Any]


class TrayController:
    """Manages the system tray icon and menu for Soulscape."""

    def __init__(
        self,
        on_add_soul: Callable[[], None],
        on_toggle_auras: Callable[[], None],
        on_settings: Callable[[], None],
        on_message_board: Callable[[], None],
        on_exit: Callable[[], None],
        *,
        get_souls: Callable[[], list[SoulInfo]] | None = None,
        get_hub_status: Callable[[], str] | None = None,
        get_mailbag_count: Callable[[], int] | None = None,
        is_paused: Callable[[], bool] | None = None,
        on_pause_toggle: Callable[[], None] | None = None,
        is_work_mode: Callable[[], bool] | None = None,
        on_work_mode_toggle: Callable[[], None] | None = None,
        on_open_market: Callable[[], None] | None = None,
        on_request_quip: Callable[[str], dict[str, Any]] | None = None,
        notify_bubble: Callable[[str, str], None] | None = None,
    ) -> None:
        """Initialize the tray controller.

        Args:
            on_add_soul: Callback when "Add Soul" is clicked.
            on_toggle_auras: Callback when "Toggle All Auras" is clicked.
            on_settings: Callback when "Settings..." is clicked.
            on_message_board: Callback when "Social / Message Board" is clicked.
            on_exit: Callback when "Exit" is clicked.
            get_souls: Returns [{soul_id, name, essence, location, state}].
            get_hub_status: Returns "online"/"offline"/etc.
            get_mailbag_count: Returns the unread mailbag count (0 until #32).
            is_paused / on_pause_toggle: pause-simulation state + toggle.
            is_work_mode / on_work_mode_toggle: work-mode state + toggle.
            on_open_market: Opens the market window; None disables the item
                (no client market surface exists yet).
            on_request_quip: Called with a soul_id; returns the Hub quip
                response dict (success or rejection).
            notify_bubble: Called with (soul_id, text, kind) to show a
                local system bubble -- used to surface quip rejections.
        """
        self.on_add_soul = on_add_soul
        self.on_toggle_auras = on_toggle_auras
        self.on_settings = on_settings
        self.on_message_board = on_message_board
        self.on_exit = on_exit
        self.get_souls = get_souls or (lambda: [])
        self.get_hub_status = get_hub_status or (lambda: "offline")
        self.get_mailbag_count = get_mailbag_count or (lambda: 0)
        self.is_paused = is_paused or (lambda: False)
        self.on_pause_toggle = on_pause_toggle
        self.is_work_mode = is_work_mode or (lambda: False)
        self.on_work_mode_toggle = on_work_mode_toggle
        self.on_open_market = on_open_market
        self.on_request_quip = on_request_quip
        self.notify_bubble = notify_bubble
        self.icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None

    def _create_icon_image(self, size: int = 64) -> Image.Image:
        """Create a simple orb-like icon for the system tray."""
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        # Draw outer glow (aura)
        center = size // 2
        for i in range(center, 8, -2):
            alpha = int(255 * (1 - i / center) * 0.5)
            draw.ellipse(
                [center - i, center - i, center + i, center + i],
                fill=(100, 200, 100, alpha),
            )

        # Draw inner orb
        orb_radius = size // 4
        draw.ellipse(
            [
                center - orb_radius,
                center - orb_radius,
                center + orb_radius,
                center + orb_radius,
            ],
            fill=(80, 180, 80, 255),
        )

        # Draw highlight
        highlight_radius = orb_radius // 2
        draw.ellipse(
            [
                center - highlight_radius - 2,
                center - highlight_radius - 2,
                center,
                center,
            ],
            fill=(150, 255, 150, 200),
        )

        return image

    @staticmethod
    def _info(text: str) -> Item:
        return Item(text, None, enabled=False)

    def _status_menu(self) -> pystray.Menu:
        souls = self.get_souls()
        return pystray.Menu(
            self._info(f"Souls: {len(souls)}"),
            self._info(f"Hub: {self.get_hub_status()}"),
        )

    def _wallet_menu(self) -> pystray.Menu:
        souls = self.get_souls()
        if not souls:
            return pystray.Menu(self._info("no souls"))
        items = []
        for soul in souls:
            essence = soul.get("essence")
            amount = f"{essence:.1f} essence" if essence is not None else "?"
            items.append(self._info(f"{soul.get('name', '?')} — {amount}"))
        return pystray.Menu(*items)

    def _whereabouts_menu(self) -> pystray.Menu:
        souls = self.get_souls()
        if not souls:
            return pystray.Menu(self._info("no souls"))
        items = []
        for soul in souls:
            location = soul.get("location") or "home"
            items.append(self._info(f"{soul.get('name', '?')} — {location}"))
        return pystray.Menu(*items)

    def _quips_menu(self) -> pystray.Menu:
        souls = self.get_souls()
        if not souls:
            return pystray.Menu(self._info("no souls"))
        if self.on_request_quip is None:
            return pystray.Menu(self._info("quips unavailable offline"))
        items = []
        for soul in souls:
            sid = soul["soul_id"]
            name = soul.get("name", "?")
            items.append(
                Item(
                    f"Ask {name} for a quip",
                    lambda icon, item, sid=sid: self._on_request_quip(sid),
                )
            )
        return pystray.Menu(*items)

    def _create_menu(self) -> pystray.Menu:
        """Create the right-click context menu (rebuilt on refresh)."""
        mailbag_count = self.get_mailbag_count()
        market_item = (
            Item("Market", self._on_open_market)
            if self.on_open_market is not None
            else Item("Market (no client surface yet)", None, enabled=False)
        )
        return pystray.Menu(
            Item("Status", self._status_menu()),
            Item("Wallet", self._wallet_menu()),
            Item(
                f"Mailbag ({mailbag_count}) — not yet (#32)",
                None,
                enabled=False,
            ),
            Item("Whereabouts", self._whereabouts_menu()),
            Item("Quips", self._quips_menu()),
            pystray.Menu.SEPARATOR,
            Item("Add Soul", self._on_add_soul),
            Item("Toggle All Auras", self._on_toggle_auras),
            Item(
                "Pause simulation",
                self._on_pause_toggle,
                checked=lambda item: self.is_paused(),
            ),
            Item(
                "Work mode",
                self._on_work_mode_toggle,
                checked=lambda item: self.is_work_mode(),
            ),
            pystray.Menu.SEPARATOR,
            Item("Social / Message Board", self._on_message_board),
            market_item,
            Item("Settings...", self._on_settings),
            pystray.Menu.SEPARATOR,
            Item("Exit Soulscape", self._on_exit),
        )

    def refresh(self) -> None:
        """Rebuild the menu so dynamic labels pick up fresh data."""
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def _notify(self, message: str, title: str = "Soulscape") -> None:
        if self.icon is None:
            return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def _on_request_quip(self, soul_id: str) -> None:
        if self.on_request_quip is None:
            return
        try:
            result = self.on_request_quip(soul_id) or {}
        except Exception as exc:
            self._surface_quip_rejection(f"Quip failed: {exc}")
            return
        if result.get("status") == "success":
            return
        reason = result.get("reason") or "rejected"
        message = result.get("message") or f"Quip rejected ({reason})."
        self._surface_quip_rejection(soul_id, message)

    def _surface_quip_rejection(self, soul_id: str, message: str) -> None:
        self._notify(message)
        if self.notify_bubble is not None:
            try:
                self.notify_bubble(soul_id, message, "system")
            except Exception:
                pass

    def _on_add_soul(self, icon: Any, item: Any) -> None:
        """Handle Add Soul menu click."""
        if self.on_add_soul:
            self.on_add_soul()

    def _on_toggle_auras(self, icon: Any, item: Any) -> None:
        """Handle Toggle All Auras menu click."""
        if self.on_toggle_auras:
            self.on_toggle_auras()

    def _on_settings(self, icon: Any, item: Any) -> None:
        """Handle Settings menu click."""
        if self.on_settings:
            self.on_settings()

    def _on_message_board(self, icon: Any, item: Any) -> None:
        """Handle Social / Message Board menu click."""
        if self.on_message_board:
            self.on_message_board()

    def _on_open_market(self, icon: Any, item: Any) -> None:
        """Handle Market menu click."""
        if self.on_open_market:
            self.on_open_market()

    def _on_pause_toggle(self, icon: Any, item: Any) -> None:
        """Handle Pause simulation toggle."""
        if self.on_pause_toggle:
            self.on_pause_toggle()
        self.refresh()

    def _on_work_mode_toggle(self, icon: Any, item: Any) -> None:
        """Handle Work mode toggle."""
        if self.on_work_mode_toggle:
            self.on_work_mode_toggle()
        self.refresh()

    def _on_exit(self, icon: Any, item: Any) -> None:
        """Handle Exit menu click."""
        if self.on_exit:
            self.on_exit()
        self.stop()

    def start(self) -> None:
        """Start the system tray icon in a background thread."""
        self.icon = pystray.Icon(
            "Soulscape",
            self._create_icon_image(),
            "Soulscape",
            self._create_menu(),
        )
        self._thread = threading.Thread(target=self.icon.run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the system tray icon."""
        if self.icon:
            self.icon.stop()
            self.icon = None
