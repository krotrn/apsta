"""Run on every libadwaita/GTK the supported distributions ship.

Baseline: libadwaita 1.0 + GTK 4.6 (Ubuntu/Pop!_OS 22.04, Linux Mint 21).
Newer widgets are used only through these helpers, which fall back to
baseline equivalents. Outside this module the GUI may only use widgets from
the baseline; ``tests/unit/test_gui.py`` checks that.

| Widget                 | libadwaita | Fallback                     |
| ---------------------- | ---------- | ---------------------------- |
| EntryRow, PasswordEntryRow | 1.2    | ActionRow + Gtk.Entry        |
| AboutWindow            | 1.2        | Gtk.AboutDialog              |
"""

from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gdk, GLib, Gtk

ADW_VERSION = (Adw.get_major_version(), Adw.get_minor_version())
HAS_ENTRY_ROW = hasattr(Adw, "EntryRow")
HAS_ABOUT_WINDOW = hasattr(Adw, "AboutWindow")


ICONS_DIR = Path(__file__).resolve().parent / "data" / "icons"


def register_bundled_icons(app_id: str) -> None:
    """Make the app icon findable when running from source or a pip install.

    Distribution packages install it into hicolor; this is a harmless fallback there.
    """
    display = Gdk.Display.get_default()
    if display is not None and ICONS_DIR.is_dir():
        Gtk.IconTheme.get_for_display(display).add_search_path(str(ICONS_DIR))
    Gtk.Window.set_default_icon_name(app_id)


def esc(text) -> str:
    """Escape user data for row titles/subtitles and toasts, which are Pango markup."""
    return GLib.markup_escape_text(str(text if text is not None else ""))


class EntryField:
    """A labelled text entry row with a uniform API across libadwaita versions."""

    def __init__(self, title: str, password: bool = False, placeholder: str = ""):
        if HAS_ENTRY_ROW:
            self.widget = Adw.PasswordEntryRow(title=title) if password else Adw.EntryRow(title=title)
            self._entry = self.widget
        else:
            self.widget = Adw.ActionRow(title=title)
            self._entry = Gtk.PasswordEntry(show_peek_icon=True) if password else Gtk.Entry()
            self._entry.set_valign(Gtk.Align.CENTER)
            self._entry.set_hexpand(True)
            if placeholder and not password:
                self._entry.set_placeholder_text(placeholder)
            self.widget.add_suffix(self._entry)
            self.widget.set_activatable_widget(self._entry)

    def get_text(self) -> str:
        return self._entry.get_text()

    def set_text(self, text: str) -> None:
        self._entry.set_text(text)

    def connect_changed(self, callback) -> None:
        self._entry.connect("changed", lambda *_: callback())


def switch_row(title: str, subtitle: str = ""):
    """ActionRow + Gtk.Switch (Adw.SwitchRow needs 1.4)."""
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    switch = Gtk.Switch(valign=Gtk.Align.CENTER)
    row.add_suffix(switch)
    row.set_activatable_widget(switch)
    return row, switch


def copy_to_clipboard(widget: Gtk.Widget, text: str) -> None:
    clipboard = widget.get_clipboard()
    if hasattr(clipboard, "set_text"):
        clipboard.set_text(text)
    else:  # pragma: no cover - older PyGObject
        clipboard.set(text)


def show_about(parent: Gtk.Window, version: str) -> None:
    info = dict(
        application_name="apsta",
        version=version,
        comments="Run a Wi-Fi hotspot while staying connected to Wi-Fi.",
        website="https://github.com/krotrn/apsta",
        license_type=Gtk.License.MIT_X11,
        developers=["krotrn and contributors"],
    )
    if HAS_ABOUT_WINDOW:
        about = Adw.AboutWindow(
            transient_for=parent,
            application_icon="com.github.apsta.Gtk",
            issue_url="https://github.com/krotrn/apsta/issues",
            **info,
        )
    else:
        about = Gtk.AboutDialog(
            transient_for=parent,
            modal=True,
            logo_icon_name="com.github.apsta.Gtk",
            program_name=info.pop("application_name"),
            authors=info.pop("developers"),
            **info,
        )
    about.present()
