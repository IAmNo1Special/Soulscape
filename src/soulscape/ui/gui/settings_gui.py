# settings_gui.py
"""Tkinter-based settings dialog for Soulscape soul configuration."""

import tkinter as tk
from tkinter import colorchooser


class SoulSettingsDialog:
    """Dialog for configuring a new or existing soul."""

    def __init__(
        self,
        parent=None,
        name="New Soul",
        orb_color=(0.56, 0.93, 0.56),
        aura_color=(1.0, 0.5, 0.0),
        stats=None,
        on_apply=None,
    ):
        """
        Initialize the settings dialog.

        Args:
            parent: Optional parent window
            name: Initial soul name
            orb_color: Initial orb color as RGB tuple (0.0-1.0)
            aura_color: Initial aura color as RGB tuple (0.0-1.0)
            on_apply: Callback with (name, orb_color, aura_color) when applied
        """
        self.on_apply = on_apply
        self.result = None

        # Create the window
        self.root = tk.Toplevel(parent) if parent else tk.Tk()
        self.root.title("Soul Settings")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)

        # Center the window
        window_width = 320
        window_height = 450
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

    def _create_widgets(self, name):
        """Create the dialog widgets."""
        # Main frame with padding
        frame = tk.Frame(self.root, padx=20, pady=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # Name entry
        tk.Label(frame, text="Soul Name:").grid(
            row=0, column=0, sticky=tk.W, pady=5
        )
        self.name_entry = tk.Entry(frame, width=20)
        self.name_entry.insert(0, name)
        self.name_entry.grid(
            row=0, column=1, columnspan=2, sticky=tk.EW, pady=5
        )

        # Orb color picker
        tk.Label(frame, text="Orb Color:").grid(
            row=1, column=0, sticky=tk.W, pady=5
        )
        self.orb_color_btn = tk.Button(
            frame,
            text="Choose...",
            command=self._pick_orb_color,
            width=10,
        )
        self.orb_color_btn.grid(row=1, column=1, pady=5)
        self.orb_preview = tk.Label(frame, text="  ", width=3)
        self.orb_preview.grid(row=1, column=2, pady=5, padx=5)
        self._update_orb_preview()

        # Aura color picker
        tk.Label(frame, text="Aura Color:").grid(
            row=2, column=0, sticky=tk.W, pady=5
        )
        self.aura_color_btn = tk.Button(
            frame,
            text="Choose...",
            command=self._pick_aura_color,
            width=10,
        )
        self.aura_color_btn.grid(row=2, column=1, pady=5)
        self.aura_preview = tk.Label(frame, text="  ", width=3)
        self.aura_preview.grid(row=2, column=2, pady=5, padx=5)
        self._update_aura_preview()

        # Stats Display (Read-only for now)
        if self.stats:
            stats_frame = tk.LabelFrame(frame, text="Stats", padx=10, pady=5)
            stats_frame.grid(
                row=3, column=0, columnspan=3, sticky=tk.EW, pady=10
            )

            # Nature
            nature = self.stats.get("nature", "Unknown")
            tk.Label(stats_frame, text=f"Nature: {nature}").grid(
                row=0, column=0, columnspan=2, sticky=tk.W
            )
            tk.Label(
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

            # We need the calculated values. Passing the whole stats dict which has structure:
            # {base: {}, ivs: {}, evs: {}, level: 1, nature: 'Safe'}
            # Wait, Soul.to_dict calls SoulStats.to_dict which returns raw data.
            # But the USER wants to see the calculated values?
            # SoulStats.to_dict DOES NOT return calculated values. It returns base/ivs/evs.
            # I must calculate them here or update SoulStats.to_dict to include them.
            # Or simpler: The Plan said "display these stats".
            # If I only have base/iv/ev, I can't easily show final stats without re-implementing formula here.
            # Better approach: Update SoulStats.to_dict to include "calculated" stats?
            # Or just show IVs/EVs for now?
            # Let's show IVs for now as they are the "genes".
            # And maybe calculate approximate final if possible?
            # Actually, re-implementing the formula here is trivial if I just import the formula? But this runs in a separate process (GUI).
            # importing soulscape.core.stats here is safe? Yes.

            # Let's import SoulStats in this file to use its calculation logic if needed, OR
            # simpler: Update Soul.to_dict to include "calculated_stats" key!
            # That is the cleanest separation. GUI shouldn't calculate stats.
            # But I already edited Soul.to_dict.
            # Let's just show raw IVs for now, or just import SoulStats here?
            # Importing SoulStats here is fine.

            # Grid headers
            tk.Label(stats_frame, text="Stat").grid(
                row=1, column=0, sticky=tk.W
            )
            tk.Label(stats_frame, text="IV").grid(row=1, column=1)
            tk.Label(stats_frame, text="EV").grid(row=1, column=2)
            tk.Label(stats_frame, text="Base").grid(row=1, column=3)

            ivs = self.stats.get("ivs", {})
            evs = self.stats.get("evs", {})
            base = self.stats.get("base", {})

            for i, (label, key) in enumerate(stats_data):
                row = i + 2
                tk.Label(stats_frame, text=label).grid(
                    row=row, column=0, sticky=tk.W
                )
                tk.Label(stats_frame, text=str(ivs.get(key, 0))).grid(
                    row=row, column=1
                )
                tk.Label(stats_frame, text=str(evs.get(key, 0))).grid(
                    row=row, column=2
                )
                tk.Label(stats_frame, text=str(base.get(key, 0))).grid(
                    row=row, column=3
                )

        # Buttons frame
        btn_frame = tk.Frame(frame)
        btn_frame.grid(row=4, column=0, columnspan=3, pady=20)

        tk.Button(
            btn_frame, text="Apply", command=self._on_apply, width=10
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="Cancel", command=self._on_cancel, width=10
        ).pack(side=tk.LEFT, padx=5)

    def _update_orb_preview(self):
        """Update the orb color preview label."""
        hex_color = rgb_to_hex(self.orb_color)
        self.orb_preview.configure(bg=hex_color)

    def _update_aura_preview(self):
        """Update the aura color preview label."""
        hex_color = rgb_to_hex(self.aura_color)
        self.aura_preview.configure(bg=hex_color)

    def _pick_orb_color(self):
        """Open color picker for orb color."""
        initial = rgb_to_hex(self.orb_color)
        result = colorchooser.askcolor(color=initial, title="Choose Orb Color")
        if result[1]:  # result is ((r, g, b), "#hexcolor")
            self.orb_color = hex_to_rgb_float(result[1])
            self._update_orb_preview()

    def _pick_aura_color(self):
        """Open color picker for aura color."""
        initial = rgb_to_hex(self.aura_color)
        result = colorchooser.askcolor(color=initial, title="Choose Aura Color")
        if result[1]:
            self.aura_color = hex_to_rgb_float(result[1])
            self._update_aura_preview()

    def _on_apply(self):
        """Handle Apply button click."""
        name = self.name_entry.get().strip() or "Unnamed Soul"
        self.result = (name, self.orb_color, self.aura_color)
        if self.on_apply:
            self.on_apply(name, self.orb_color, self.aura_color)
        self.root.destroy()

    def _on_cancel(self):
        """Handle Cancel button click."""
        self.result = None
        self.root.destroy()

    def show(self):
        """Show the dialog and wait for it to close."""
        self.root.grab_set()
        self.root.wait_window()
        return self.result


class SoulContextMenu:
    """Context menu for a soul, running in its own Tkinter root."""

    def __init__(
        self, soul_name, on_edit, on_toggle_aura, on_dismiss, parent=None
    ):
        """
        Initialize the context menu.

        Args:
            soul_name (str): Name of the soul.
            on_edit (callable): Callback for Edit action.
            on_toggle_aura (callable): Callback for Toggle Aura action.
            on_dismiss (callable): Callback for Dismiss action.
            parent: Optional parent Tk/Toplevel window.
        """
        self.soul_name = soul_name
        self.on_edit = on_edit
        self.on_toggle_aura = on_toggle_aura
        self.on_dismiss = on_dismiss
        self.parent = parent
        self.root = None  # Used if we create our own root/toplevel
        self.menu = None  # Keep reference

    def show(self, x, y):
        """
        Show the context menu at the specified screen coordinates.
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

    def _build_menu(self, menu):
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

    def _handle_choice(self, callback):
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
        parent=None,
        current_opacity=100,
        run_on_startup=False,
        on_apply=None,
    ):
        """
        Initialize the global settings dialog.

        Args:
            parent: Optional parent window
            current_opacity: Current opacity percentage (15-100)
            run_on_startup: Whether app runs on Windows startup
            on_apply: Callback with (opacity, run_on_startup)
        """
        self.on_apply = on_apply
        self.result = None

        # Create the window
        self.root = tk.Toplevel(parent) if parent else tk.Tk()
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

        self._create_widgets(name=None)  # Corrected internal call

    def _create_widgets(
        self, name=None
    ):  # Accept argument to be safe, though not used here
        """Create the dialog widgets."""
        # Main frame with padding
        frame = tk.Frame(self.root, padx=20, pady=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # Opacity slider
        tk.Label(frame, text="Global Opacity:").grid(
            row=0, column=0, sticky=tk.W, pady=5
        )
        opacity_frame = tk.Frame(frame)
        opacity_frame.grid(row=0, column=1, sticky=tk.EW, pady=5)

        self.opacity_slider = tk.Scale(
            opacity_frame,
            from_=0,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.opacity_var,
            length=150,
        )
        self.opacity_slider.pack(side=tk.LEFT)
        tk.Label(opacity_frame, text="%").pack(side=tk.LEFT)

        # Run on Startup checkbox
        tk.Checkbutton(
            frame,
            text="Run on Windows Startup",
            variable=self.run_on_startup_var,
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=5)

        # Buttons frame
        btn_frame = tk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=20)

        tk.Button(
            btn_frame, text="Apply", command=self._on_apply, width=10
        ).pack(side=tk.LEFT, padx=5)
        tk.Button(
            btn_frame, text="Cancel", command=self._on_cancel, width=10
        ).pack(side=tk.LEFT, padx=5)

    def _on_apply(self):
        """Handle Apply button click."""
        ui_opacity = self.opacity_var.get()
        real_opacity = self._ui_to_real(ui_opacity)
        run_on_startup = self.run_on_startup_var.get()

        self.result = (real_opacity, run_on_startup)
        if self.on_apply:
            self.on_apply(real_opacity, run_on_startup)
        self.root.destroy()

    def _real_to_ui(self, real_val):
        """Convert real opacity (15-100) to UI value (0-100)."""
        real_val = max(15, min(100, real_val))
        return int((real_val - 15) / 85 * 100)

    def _ui_to_real(self, ui_val):
        """Convert UI value (0-100) to real opacity (15-100)."""
        ui_val = max(0, min(100, ui_val))
        return int(15 + (ui_val / 100 * 85))

    def _on_cancel(self):
        """Handle Cancel button click."""
        self.result = None
        self.root.destroy()

    def show(self):
        """Show the dialog and wait for it to close."""
        self.root.grab_set()
        self.root.wait_window()
        return self.result


def rgb_to_hex(rgb_tuple):
    """Convert RGB tuple (0.0-1.0) to hex string."""
    r = int(rgb_tuple[0] * 255)
    g = int(rgb_tuple[1] * 255)
    b = int(rgb_tuple[2] * 255)
    return f"#{r:02x}{g:02x}{b:02x}"


def hex_to_rgb_float(hex_color):
    """Convert hex string to RGB tuple (0.0-1.0)."""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0
    return (r, g, b)


def show_add_soul_dialog(on_apply):
    """
    Show a dialog to add a new soul.

    Args:
        on_apply: Callback with (name, orb_color, aura_color) when applied
    """
    dialog = SoulSettingsDialog(on_apply=on_apply)
    dialog.show()
