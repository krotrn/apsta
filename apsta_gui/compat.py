"""Use the newest libadwaita/GTK widgets available, and fall back on older systems.

The GUI targets libadwaita 1.1 + GTK 4.6 (Ubuntu/Pop!_OS 22.04, Linux Mint 21)
up to the latest release. Each helper here picks the modern widget when the
installed library has it, so up-to-date systems get the current look and
behaviour while older ones still work:

| Helper              | Modern widget (libadwaita)           | Fallback                          |
| ------------------- | ------------------------------------ | --------------------------------- |
| ``toolbar``         | ToolbarView (1.4)                    | vertical Gtk.Box                  |
| ``adaptive_tabs``   | ViewSwitcher + ViewSwitcherBar + Breakpoint (1.4) | narrow ViewSwitcher in header |
| ``EntryField``      | EntryRow / PasswordEntryRow (1.2)    | ActionRow + Gtk.Entry             |
| ``switch_row``      | SwitchRow (1.4)                      | ActionRow + Gtk.Switch            |
| ``button_row``      | ButtonRow (1.6)                      | ActionRow + Gtk.Button            |
| ``Spinner``         | Adw.Spinner (1.6)                    | Gtk.Spinner                       |
| ``Dialog``          | Adw.Dialog (1.5, adaptive sheet)     | modal Adw.Window                  |
| ``show_about``      | AboutDialog (1.5) / AboutWindow (1.2)| Gtk.AboutDialog                   |

Outside this module the GUI uses only widgets present in libadwaita 1.1;
``tests/unit/test_gui.py`` enforces that.
"""

from __future__ import annotations

from pathlib import Path

from gi.repository import Adw, Gdk, GLib, Gtk

ADW_VERSION = (Adw.get_major_version(), Adw.get_minor_version())
HAS_ENTRY_ROW = hasattr(Adw, "EntryRow")
HAS_TOOLBAR_VIEW = hasattr(Adw, "ToolbarView")
HAS_BREAKPOINT = hasattr(Adw, "Breakpoint") and hasattr(Adw, "ViewSwitcherBar")
HAS_SWITCH_ROW = hasattr(Adw, "SwitchRow")
HAS_BUTTON_ROW = hasattr(Adw, "ButtonRow")
HAS_SPINNER = hasattr(Adw, "Spinner")
HAS_DIALOG = hasattr(Adw, "Dialog")
HAS_ABOUT_DIALOG = hasattr(Adw, "AboutDialog")
HAS_ABOUT_WINDOW = hasattr(Adw, "AboutWindow")

ICONS_DIR = Path(__file__).resolve().parent / "data" / "icons"
NARROW_WIDTH = "max-width: 550sp"


def register_bundled_icons(app_id: str) -> None:
    """Make the app icon findable when running from source or a pip install.

    Distribution packages install it into hicolor; this is a harmless fallback there.
    """
    display = Gdk.Display.get_default()
    if display is not None and ICONS_DIR.is_dir():
        Gtk.IconTheme.get_for_display(display).add_search_path(str(ICONS_DIR))
    Gtk.Window.set_default_icon_name(app_id)


def ensure_font_dpi() -> None:
    """Give GTK a DPI when the session doesn't provide one.

    libadwaita 1.5 (Ubuntu 24.04) converts ``sp`` sizes with ``gtk-xft-dpi`` and,
    when it is unset (-1, e.g. X11 without a settings daemon), clamps page
    content to its minimum width. 96 is GTK's own default DPI.
    """
    settings = Gtk.Settings.get_default()
    if settings is not None and settings.get_property("gtk-xft-dpi") <= 0:
        settings.set_property("gtk-xft-dpi", 96 * 1024)


def esc(text) -> str:
    """Escape user data for row titles/subtitles and toasts, which are Pango markup."""
    return GLib.markup_escape_text(str(text if text is not None else ""))


# ── layout ────────────────────────────────────────────────────────────────────


def toolbar(top: Gtk.Widget, content: Gtk.Widget, bottom: Gtk.Widget = None) -> Gtk.Widget:
    """Header + content (+ bottom bar), with ToolbarView's flat/raised styling when available."""
    if HAS_TOOLBAR_VIEW:
        view = Adw.ToolbarView()
        view.add_top_bar(top)
        view.set_content(content)
        if bottom is not None:
            view.add_bottom_bar(bottom)
        return view
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(top)
    content.set_vexpand(True)
    box.append(content)
    if bottom is not None:
        box.append(bottom)
    return box


def adaptive_tabs(window: Adw.ApplicationWindow, header: Adw.HeaderBar, stack: Adw.ViewStack):
    """Tabs in the header on wide windows and in a bottom bar on narrow ones.

    Returns the bottom bar widget (or None) for ``toolbar``.
    """
    if not HAS_BREAKPOINT:
        header.set_title_widget(Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.NARROW))
        return None
    header.set_title_widget(Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE))
    bar = Adw.ViewSwitcherBar(stack=stack)
    breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(NARROW_WIDTH))
    breakpoint.add_setter(bar, "reveal", True)
    breakpoint.add_setter(header, "show-title", False)
    window.add_breakpoint(breakpoint)
    return bar


# ── rows ──────────────────────────────────────────────────────────────────────


class EntryField:
    """A labelled text entry row with a uniform API across libadwaita versions."""

    def __init__(self, title: str, password: bool = False):
        if HAS_ENTRY_ROW:
            self.widget = Adw.PasswordEntryRow(title=title) if password else Adw.EntryRow(title=title)
            self._entry = self.widget
        else:
            self.widget = Adw.ActionRow(title=title)
            self._entry = Gtk.PasswordEntry(show_peek_icon=True) if password else Gtk.Entry()
            self._entry.set_valign(Gtk.Align.CENTER)
            self._entry.set_hexpand(True)
            self.widget.add_suffix(self._entry)
            self.widget.set_activatable_widget(self._entry)

    def get_text(self) -> str:
        return self._entry.get_text()

    def set_text(self, text: str) -> None:
        self._entry.set_text(text)

    def connect_changed(self, callback) -> None:
        self._entry.connect("changed", lambda *_: callback())


def switch_row(title: str, subtitle: str = ""):
    """Returns ``(row, toggle)``; ``toggle`` has ``active`` / ``get_active`` / ``notify::active``."""
    if HAS_SWITCH_ROW:
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        return row, row
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    switch = Gtk.Switch(valign=Gtk.Align.CENTER)
    row.add_suffix(switch)
    row.set_activatable_widget(switch)
    return row, switch


def button_row(title: str, on_activate, style: str = "", subtitle: str = ""):
    """A row that performs an action. Returns ``(row, sensitive_target)``."""
    if HAS_BUTTON_ROW:
        row = Adw.ButtonRow(title=title)
        if style:
            row.add_css_class(style)
        row.connect("activated", lambda *_: on_activate())
        return row, row
    row = Adw.ActionRow(title=subtitle)
    button = Gtk.Button(label=title, valign=Gtk.Align.CENTER)
    if style:
        button.add_css_class(style)
    button.connect("clicked", lambda *_: on_activate())
    row.add_suffix(button)
    return row, button


# ── feedback ──────────────────────────────────────────────────────────────────


class Spinner:
    """Busy indicator: Adw.Spinner where available, else Gtk.Spinner."""

    def __init__(self):
        self.widget = Adw.Spinner() if HAS_SPINNER else Gtk.Spinner()
        self.widget.set_visible(False)

    def set_busy(self, busy: bool) -> None:
        self.widget.set_visible(busy)
        if not HAS_SPINNER:
            self.widget.set_spinning(busy)


class Dialog:
    """A titled dialog: adaptive Adw.Dialog (bottom sheet on phones) or a modal window."""

    def __init__(self, parent: Gtk.Window, title: str, child: Gtk.Widget, width: int = 360):
        self.parent = parent
        content = toolbar(Adw.HeaderBar(), child)
        if HAS_DIALOG:
            self.widget = Adw.Dialog(title=title, content_width=width)
            self.widget.set_child(content)
        else:
            self.widget = Adw.Window(
                transient_for=parent, modal=True, title=title, default_width=width, resizable=False
            )
            self.widget.set_content(content)

    def present(self) -> None:
        if HAS_DIALOG:
            self.widget.present(self.parent)
        else:
            self.widget.present()


def copy_to_clipboard(widget: Gtk.Widget, text: str) -> None:
    widget.get_clipboard().set_text(text)


def show_about(parent: Gtk.Window, version: str, app_id: str) -> None:
    info = dict(
        application_name="apsta",
        version=version,
        comments="Run a Wi-Fi hotspot while staying connected to Wi-Fi.",
        website="https://github.com/krotrn/apsta",
        license_type=Gtk.License.MIT_X11,
        developers=["krotrn and contributors"],
    )
    if HAS_ABOUT_DIALOG:
        Adw.AboutDialog(application_icon=app_id, issue_url="https://github.com/krotrn/apsta/issues", **info).present(
            parent
        )
    elif HAS_ABOUT_WINDOW:
        Adw.AboutWindow(
            transient_for=parent, application_icon=app_id, issue_url="https://github.com/krotrn/apsta/issues", **info
        ).present()
    else:
        Gtk.AboutDialog(
            transient_for=parent,
            modal=True,
            logo_icon_name=app_id,
            program_name=info.pop("application_name"),
            authors=info.pop("developers"),
            **info,
        ).present()
