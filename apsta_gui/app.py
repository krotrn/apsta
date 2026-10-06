"""apsta-gtk: application bootstrap."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from apsta_cli import __version__  # noqa: E402

from .backend import ApstaBackend  # noqa: E402
from .compat import ICONS_DIR, ensure_font_dpi, register_bundled_icons, show_about  # noqa: E402
from .helpers import APP_ID, band_change, band_label, tray_state  # noqa: E402
from .tray import TrayIcon, svg_pixmaps  # noqa: E402
from .window import ApstaWindow, MissingApstaWindow  # noqa: E402

TRAY_GRACE = 3  # seconds a --background start waits for a tray before showing the window


class AppTray:
    """The tray icon for one window: status as icon and tooltip, plus a menu."""

    def __init__(self, app: "ApstaApp", window: ApstaWindow):
        self.app, self.window = app, window
        # Fallback pictures for trays whose icon theme lacks the status icons:
        # the app's symbolic icon in a grey that reads on light and dark panels.
        try:
            svg = (ICONS_DIR / "hicolor/symbolic/apps" / f"{APP_ID}-symbolic.svg").read_text()
        except OSError:
            svg = ""
        on = svg.replace("#2e3436", "#bebebe")
        off = on.replace("<svg ", '<svg opacity="0.5" ', 1)
        self.pixmaps = {True: svg_pixmaps(on.encode()) if svg else [], False: svg_pixmaps(off.encode()) if svg else []}
        self.icon = TrayIcon("apsta", "Hotspot (apsta)", window.toggle_visible, self._on_menu, app.on_tray_change)
        window.tray = self
        self.update({}, False)

    @property
    def available(self) -> bool:
        return self.icon.available

    def update(self, data: dict, busy: bool) -> None:
        icon, title, body, items = tray_state(data, busy)
        self.icon.update(icon, title, body, items, self.pixmaps[bool(data.get("hotspot"))])

    def _on_menu(self, key: str) -> None:
        window = self.window
        if key == "toggle":
            window.hotspot_page.toggle_hotspot()
        elif key == "share":
            window.present()
            window.share()
        elif key in ("devices", "settings"):
            window.show_page("clients" if key == "devices" else "settings")
            window.present()
        elif key.startswith("profile:"):
            name = key.split(":", 1)[1]
            window.run_privileged(lambda: window.backend.use_profile(name))
        elif key.startswith("band:"):
            changes = band_change(window.data.get("config") or {}, key.split(":", 1)[1])
            window.run_privileged(
                lambda: window.backend.set_config(changes, success=f"Band set to {band_label(changes['band'])}.")
            )
        elif key == "autostart":
            enabled = not (window.data.get("autostart") or {}).get("enabled")
            window.run_privileged(lambda: window.backend.set_autostart(enabled))
        elif key == "show":
            window.present()
        elif key == "quit":
            self.app.quit()

    def close(self) -> None:
        self.icon.close()


class ApstaApp(Adw.Application):
    def __init__(self, backend: ApstaBackend, background: bool = False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.backend = backend
        self.background = background  # start hidden in the tray (e.g. at login)
        self.tray = None
        self.connect("activate", self._on_activate)
        for name, callback, accels in (
            ("quit", lambda *_: self.quit(), ["<Control>q"]),
            ("about", lambda *_: show_about(self.get_active_window(), __version__, APP_ID), []),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("win.refresh", ["<Control>r", "F5"])

    def do_startup(self):
        Adw.Application.do_startup(self)
        register_bundled_icons(APP_ID)
        ensure_font_dpi()

    def do_shutdown(self):
        if self.tray is not None:
            self.tray.close()
        Adw.Application.do_shutdown(self)

    def _on_activate(self, app):
        window = self.get_active_window()
        if window is not None:
            window.present()
        elif not self.backend.available():
            MissingApstaWindow(app, self.backend.apsta).present()
        else:
            window = ApstaWindow(app, self.backend)
            self.tray = AppTray(self, window)
            if self.background:
                # Show the window after all if no tray turns up to hold the icon.
                GLib.timeout_add_seconds(TRAY_GRACE, lambda: self.on_tray_change(self.tray.available) and False)
            else:
                window.present()

    def on_tray_change(self, available: bool) -> None:
        """Never leave the app running with neither a window nor a tray icon."""
        window = self.tray.window if self.tray else None
        if not available and window is not None and not window.get_visible():
            window.present()


def main():
    background = "--background" in sys.argv[1:]
    raise SystemExit(ApstaApp(ApstaBackend(), background=background).run(sys.argv[:1]))
