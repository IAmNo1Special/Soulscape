"""Tkinter-based settings dialog for Soulscape soul configuration."""

from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser
from typing import Any, Callable

import ttkbootstrap as ttk


class SoulSettingsDialog:
    """Dialog for configuring a new or existing soul."""

    def __init__(
        self,
        parent: tk.Tk | tk.Toplevel | ttk.Window | None = None,
        name: str = "New Soul",
        orb_color: tuple[float, float, float] = (0.56, 0.93, 0.56),
        aura_color: tuple[float, float, float] = (1.0, 0.5, 0.0),
        stats: dict[str, Any] | None = None,
        on_apply: (
            Callable[
                [str, tuple[float, float, float], tuple[float, float, float]],
                None,
            ]
            | None
        ) = None,
    ):
        """Initialize the settings dialog.

        Args:
            parent: Optional parent window.
            name: Initial soul name.
            orb_color: Initial orb color as RGB tuple (0.0-1.0).
            aura_color: Initial aura color as RGB tuple (0.0-1.0).
            stats: Dictionary of soul statistics (Nature, Genes, etc.).
            on_apply: Callback with (name, orb_color, aura_color) when applied.
        """
        self.on_apply = on_apply
        self.result: (
            tuple[str, tuple[float, float, float], tuple[float, float, float]]
            | None
        ) = None

        # Create the window
        self.root = ttk.Toplevel(parent) if parent else ttk.Window()
        self.root.title("Soul Settings")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        # Center the window
        window_width = 350  # Slightly wider for stats
        window_height = 500  # Taller for stats
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")

        # Store colors
        self.orb_color = orb_color
        self.aura_color = aura_color
        self.stats = stats or {}

        self._create_widgets(name)

    def _create_widgets(self, name: str) -> None:
        """Create the dialog widgets."""
        # Main frame with padding
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        # Name entry
        ttk.Label(frame, text="Soul Name:").grid(
            row=0, column=0, sticky=tk.W, pady=5
        )
        self.name_entry = ttk.Entry(frame, width=25)
        self.name_entry.insert(0, name)
        self.name_entry.grid(
            row=0, column=1, columnspan=2, sticky=tk.EW, pady=5
        )

        # Orb color picker
        ttk.Label(frame, text="Orb Color:").grid(
            row=1, column=0, sticky=tk.W, pady=5
        )
        self.orb_color_btn = ttk.Button(
            frame,
            text="Choose...",
            command=self._pick_orb_color,
            width=10,
            bootstyle="secondary",
        )
        self.orb_color_btn.grid(row=1, column=1, pady=5)
        self.orb_preview = tk.Label(frame, text="  ", width=4, relief="sunken")
        self.orb_preview.grid(row=1, column=2, pady=5, padx=5)
        self._update_orb_preview()

        # Aura color picker
        ttk.Label(frame, text="Aura Color:").grid(
            row=2, column=0, sticky=tk.W, pady=5
        )
        self.aura_color_btn = ttk.Button(
            frame,
            text="Choose...",
            command=self._pick_aura_color,
            width=10,
            bootstyle="secondary",
        )
        self.aura_color_btn.grid(row=2, column=1, pady=5)
        self.aura_preview = tk.Label(frame, text="  ", width=4, relief="sunken")
        self.aura_preview.grid(row=2, column=2, pady=5, padx=5)
        self._update_aura_preview()

        # Stats Display (Read-only for now)
        if self.stats:
            stats_frame = ttk.Labelframe(
                frame, text="Gene Sequence", padding=10
            )
            stats_frame.grid(
                row=3, column=0, columnspan=3, sticky=tk.EW, pady=15
            )

            # Nature
            nature = self.stats.get("nature", "Unknown")
            ttk.Label(stats_frame, text=f"Nature: {nature}").grid(
                row=0, column=0, columnspan=2, sticky=tk.W
            )
            ttk.Label(
                stats_frame, text=f"Level: {self.stats.get('level', 1)}"
            ).grid(row=0, column=2, columnspan=2, sticky=tk.E)

            # Stat Grid
            stats_data = [
                ("HP", "hp"),
                ("Atk", "attack"),
                ("Def", "defense"),
                ("SpA", "sp_atk"),
                ("SpD", "sp_def"),
                ("Spe", "speed"),
            ]

            # Grid headers
            ttk.Label(
                stats_frame, text="Stat", font=("Segoe UI", 9, "bold")
            ).grid(row=1, column=0, sticky=tk.W, padx=2)
            ttk.Label(
                stats_frame, text="IV", font=("Segoe UI", 9, "bold")
            ).grid(row=1, column=1, padx=2)
            ttk.Label(
                stats_frame, text="EV", font=("Segoe UI", 9, "bold")
            ).grid(row=1, column=2, padx=2)
            ttk.Label(
                stats_frame, text="Base", font=("Segoe UI", 9, "bold")
            ).grid(row=1, column=3, padx=2)

            ivs = self.stats.get("ivs", {})
            evs = self.stats.get("evs", {})
            base = self.stats.get("base", {})

            for i, (label, key) in enumerate(stats_data):
                row = i + 2
                ttk.Label(stats_frame, text=label).grid(
                    row=row, column=0, sticky=tk.W, padx=2
                )
                ttk.Label(stats_frame, text=str(ivs.get(key, 0))).grid(
                    row=row, column=1, padx=2
                )
                ttk.Label(stats_frame, text=str(evs.get(key, 0))).grid(
                    row=row, column=2, padx=2
                )
                ttk.Label(stats_frame, text=str(base.get(key, 0))).grid(
                    row=row, column=3, padx=2
                )

        # Buttons frame
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=4, column=0, columnspan=3, pady=20)

        ttk.Button(
            btn_frame,
            text="Apply",
            command=self._on_apply,
            width=10,
            bootstyle="success",
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame,
            text="Cancel",
            command=self._on_cancel,
            width=10,
            bootstyle="secondary",
        ).pack(side=tk.LEFT, padx=5)

    def _update_orb_preview(self) -> None:
        """Update the orb color preview label."""
        hex_color = rgb_to_hex(self.orb_color)
        self.orb_preview.configure(bg=hex_color)

    def _update_aura_preview(self) -> None:
        """Update the aura color preview label."""
        hex_color = rgb_to_hex(self.aura_color)
        self.aura_preview.configure(bg=hex_color)

    def _pick_orb_color(self) -> None:
        """Open color picker for orb color."""
        initial = rgb_to_hex(self.orb_color)
        result = colorchooser.askcolor(color=initial, title="Choose Orb Color")
        if result[1]:  # result is ((r, g, b), "#hexcolor")
            self.orb_color = hex_to_rgb_float(result[1])
            self._update_orb_preview()

    def _pick_aura_color(self) -> None:
        """Open color picker for aura color."""
        initial = rgb_to_hex(self.aura_color)
        result = colorchooser.askcolor(color=initial, title="Choose Aura Color")
        if result[1]:
            self.aura_color = hex_to_rgb_float(result[1])
            self._update_aura_preview()

    def _on_apply(self) -> None:
        """Handle Apply button click."""
        name = self.name_entry.get().strip() or "Unnamed Soul"
        self.result = (name, self.orb_color, self.aura_color)
        if self.on_apply:
            self.on_apply(name, self.orb_color, self.aura_color)
        self.root.destroy()

    def _on_cancel(self) -> None:
        """Handle Cancel button click."""
        self.result = None
        self.root.destroy()

    def show(
        self,
    ) -> (
        tuple[str, tuple[float, float, float], tuple[float, float, float]]
        | None
    ):
        """Show the dialog and wait for it to close."""
        self.root.grab_set()
        self.root.wait_window()
        return self.result


class SoulContextMenu:
    """Context menu for a soul, running in its own Tkinter root."""

    def __init__(
        self,
        soul_name: str,
        on_edit: Callable[[], None],
        on_toggle_aura: Callable[[], None],
        on_dismiss: Callable[[], None],
        parent: tk.Tk | tk.Toplevel | ttk.Window | None = None,
    ):
        """Initialize the context menu.

        Args:
            soul_name: Name of the soul.
            on_edit: Callback for Edit action.
            on_toggle_aura: Callback for Toggle Aura action.
            on_dismiss: Callback for Dismiss action.
            parent: Optional parent Tk/Toplevel window.
        """
        self.soul_name = soul_name
        self.on_edit = on_edit
        self.on_toggle_aura = on_toggle_aura
        self.on_dismiss = on_dismiss
        self.parent = parent
        self.root: tk.Tk | None = (
            None  # Used if we create our own root/toplevel
        )
        self.menu: tk.Menu | None = None  # Keep reference

    def show(self, x: int, y: int) -> None:
        """Show the context menu at the specified screen coordinates.

        Args:
            x: Screen x-coordinate.
            y: Screen y-coordinate.
        """
        # If we have a parent, attach the menu to it directly
        if self.parent:
            self.menu = tk.Menu(self.parent, tearoff=0)
            self._build_menu(self.menu)

            # Use post() instead of tk_popup().
            self.menu.post(int(x), int(y))
            return

        # Legacy/Standalone mode
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.attributes("-topmost", True)

        try:
            self.menu = tk.Menu(self.root, tearoff=0)
            self._build_menu(self.menu)
            self.menu.tk_popup(int(x), int(y))
            self.root.mainloop()
        finally:
            if self.root:
                try:
                    self.root.destroy()
                except tk.TclError:
                    pass

    def _build_menu(self, menu: tk.Menu) -> None:
        """Helper to populate menu items."""
        menu.add_command(
            label=f"Edit {self.soul_name}...",
            command=lambda: self._handle_choice(self.on_edit),
        )
        menu.add_command(
            label="Toggle Aura",
            command=lambda: self._handle_choice(self.on_toggle_aura),
        )
        menu.add_separator()
        menu.add_command(
            label="Dismiss Soul",
            command=lambda: self._handle_choice(self.on_dismiss),
        )

    def _handle_choice(self, callback: Callable[[], None] | None) -> None:
        """Handle a menu selection."""
        # 1. Try Unpost
        if self.menu:
            try:
                self.menu.unpost()
            except Exception:
                pass

            # 2. Force Destroy (Separate Block)
            try:
                self.menu.destroy()
            except Exception:
                pass
            self.menu = None

        # Clean up root if we created one
        if self.root:
            try:
                self.root.destroy()
            except Exception:
                pass
            self.root = None

        if callback:
            callback()


class GlobalSettingsDialog:
    """Dialog for global application settings."""

    def __init__(
        self,
        parent: tk.Tk | tk.Toplevel | ttk.Window | None = None,
        current_opacity: float = 100.0,
        run_on_startup: bool = False,
        on_apply: Callable[[float, bool], None] | None = None,
    ):
        """Initialize the global settings dialog.

        Args:
            parent: Optional parent window.
            current_opacity: Current opacity percentage (15-100).
            run_on_startup: Whether app runs on Windows startup.
            on_apply: Callback with (opacity, run_on_startup).
        """
        self.on_apply = on_apply
        self.result: tuple[float, bool] | None = None

        # Create the window
        self.root = ttk.Toplevel(parent) if parent else ttk.Window()
        self.root.title("Soulscape Settings")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        # Center the window
        window_width = 320
        window_height = 200
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")

        # Variables
        # Map real opacity (15-100) to UI opacity (0-100)
        ui_opacity = self._real_to_ui(current_opacity)
        self.opacity_var = tk.IntVar(value=ui_opacity)
        self.run_on_startup_var = tk.BooleanVar(value=run_on_startup)

        self._create_widgets()

    def _create_widgets(self) -> None:
        """Create the dialog widgets."""
        # Main frame with padding
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        # Opacity slider
        ttk.Label(frame, text="Global Opacity:").grid(
            row=0, column=0, sticky=tk.W, pady=5
        )
        opacity_frame = ttk.Frame(frame)
        opacity_frame.grid(row=0, column=1, sticky=tk.EW, pady=5)

        self.opacity_slider = ttk.Scale(
            opacity_frame,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.opacity_var,
            length=150,
        )
        self.opacity_slider.pack(side=tk.LEFT)
        ttk.Label(opacity_frame, text="%").pack(side=tk.LEFT)

        # Run on Startup checkbox
        ttk.Checkbutton(
            frame,
            text="Run on Windows Startup",
            variable=self.run_on_startup_var,
            bootstyle="round-toggle",
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=5)

        # Buttons frame
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=20)

        ttk.Button(
            btn_frame,
            text="Apply",
            command=self._on_apply,
            width=10,
            bootstyle="success",
        ).pack(side=tk.LEFT, padx=5)
        ttk.Button(
            btn_frame,
            text="Cancel",
            command=self._on_cancel,
            width=10,
            bootstyle="secondary",
        ).pack(side=tk.LEFT, padx=5)

    def _on_apply(self) -> None:
        """Handle Apply button click."""
        ui_opacity = self.opacity_var.get()
        real_opacity = self._ui_to_real(ui_opacity)
        run_on_startup = self.run_on_startup_var.get()

        self.result = (real_opacity, run_on_startup)
        if self.on_apply:
            self.on_apply(real_opacity, run_on_startup)
        self.root.destroy()

    def _real_to_ui(self, real_val: float) -> int:
        """Convert real opacity (15-100) to UI value (0-100)."""
        real_val = max(15, min(100, real_val))
        return int((real_val - 15) / 85 * 100)

    def _ui_to_real(self, ui_val: int) -> float:
        """Convert UI value (0-100) to real opacity (15-100)."""
        ui_val = max(0, min(100, ui_val))
        return int(15 + (ui_val / 100 * 85))

    def _on_cancel(self) -> None:
        """Handle Cancel button click."""
        self.result = None
        self.root.destroy()

    def show(self) -> tuple[float, bool] | None:
        """Show the dialog and wait for it to close."""
        self.root.grab_set()
        self.root.wait_window()
        return self.result


def rgb_to_hex(rgb_tuple: tuple[float, float, float]) -> str:
    """Convert RGB tuple (0.0-1.0) to hex string."""
    r = int(rgb_tuple[0] * 255)
    g = int(rgb_tuple[1] * 255)
    b = int(rgb_tuple[2] * 255)
    return f"#{r:02x}{g:02x}{b:02x}"


def hex_to_rgb_float(hex_color: str) -> tuple[float, float, float]:
    """Convert hex string to RGB tuple (0.0-1.0)."""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0
    return (r, g, b)


def show_add_soul_dialog(
    on_apply: Callable[
        [str, tuple[float, float, float], tuple[float, float, float]], None
    ],
) -> None:
    """Show a dialog to add a new soul.

    Args:
        on_apply: Callback with (name, orb_color, aura_color) when applied.
    """
    dialog = SoulSettingsDialog(on_apply=on_apply)
    dialog.show()
