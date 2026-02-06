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
        window_width = 300
        window_height = 200
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")

        # Store colors
        self.orb_color = orb_color
        self.aura_color = aura_color

        self._create_widgets(name)

    def _create_widgets(self, name):
        """Create the dialog widgets."""
        # Main frame with padding
        frame = tk.Frame(self.root, padx=20, pady=15)
        frame.pack(fill=tk.BOTH, expand=True)

        # Name entry
        tk.Label(frame, text="Soul Name:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.name_entry = tk.Entry(frame, width=20)
        self.name_entry.insert(0, name)
        self.name_entry.grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=5)

        # Orb color picker
        tk.Label(frame, text="Orb Color:").grid(row=1, column=0, sticky=tk.W, pady=5)
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
        tk.Label(frame, text="Aura Color:").grid(row=2, column=0, sticky=tk.W, pady=5)
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

        # Buttons frame
        btn_frame = tk.Frame(frame)
        btn_frame.grid(row=3, column=0, columnspan=3, pady=20)

        tk.Button(btn_frame, text="Apply", command=self._on_apply, width=10).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(btn_frame, text="Cancel", command=self._on_cancel, width=10).pack(
            side=tk.LEFT, padx=5
        )

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

    def __init__(self, soul_name, on_edit, on_toggle_aura, on_dismiss):
        """
        Initialize the context menu.

        Args:
            soul_name (str): Name of the soul.
            on_edit (callable): Callback for Edit action.
            on_toggle_aura (callable): Callback for Toggle Aura action.
            on_dismiss (callable): Callback for Dismiss action.
        """
        self.soul_name = soul_name
        self.on_edit = on_edit
        self.on_toggle_aura = on_toggle_aura
        self.on_dismiss = on_dismiss
        self.root = None

    def show(self, x, y):
        """
        Show the context menu at the specified screen coordinates.
        This blocks until the menu is closed.
        """
        self.root = tk.Tk()
        self.root.withdraw()  # Hide the root window
        # Ensure the root (and thus the menu) is topmost
        self.root.attributes("-topmost", True)

        try:
            menu = tk.Menu(self.root, tearoff=0)
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

            # tk_popup expects int coordinates
            menu.tk_popup(int(x), int(y))

            self.root.mainloop()
        finally:
            # Ensure cleanup if something goes wrong or mainloop exits
            if self.root:
                try:
                    self.root.destroy()
                except tk.TclError:
                    pass

    def _handle_choice(self, callback):
        """Handle a menu selection."""
        # Destroy root mainly to close the menu/app context
        if self.root:
            self.root.destroy()
            self.root = None

        # Execute callback
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
        # Variables
        # Map real opacity (15-100) to UI opacity (0-100)
        ui_opacity = self._real_to_ui(current_opacity)
        self.opacity_var = tk.IntVar(value=ui_opacity)
        self.run_on_startup_var = tk.BooleanVar(value=run_on_startup)

        self._create_widgets()

    def _create_widgets(self):
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

        tk.Button(btn_frame, text="Apply", command=self._on_apply, width=10).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(btn_frame, text="Cancel", command=self._on_cancel, width=10).pack(
            side=tk.LEFT, padx=5
        )

    def _on_apply(self):
        """Handle Apply button click."""
        ui_opacity = self.opacity_var.get()
        # Convert UI opacity (0-100) back to real opacity (15-100)
        real_opacity = self._ui_to_real(ui_opacity)

        run_on_startup = self.run_on_startup_var.get()

        self.result = (real_opacity, run_on_startup)
        if self.on_apply:
            self.on_apply(real_opacity, run_on_startup)
        self.root.destroy()

    def _real_to_ui(self, real_val):
        """Convert real opacity (15-100) to UI value (0-100)."""
        # Linear map: 15->0, 100->100
        # val = (real - 15) / 85 * 100
        real_val = max(15, min(100, real_val))
        return int((real_val - 15) / 85 * 100)

    def _ui_to_real(self, ui_val):
        """Convert UI value (0-100) to real opacity (15-100)."""
        # Linear map: 0->15, 100->100
        # val = 15 + (ui / 100 * 85)
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
