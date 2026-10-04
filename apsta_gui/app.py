"""apsta-gtk: application bootstrap."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio  # noqa: E402

from apsta_cli import __version__  # noqa: E402

from .backend import ApstaBackend  # noqa: E402
from .compat import ensure_font_dpi, register_bundled_icons, show_about  # noqa: E402
from .helpers import APP_ID  # noqa: E402
from .window import ApstaWindow, MissingApstaWindow  # noqa: E402


class ApstaApp(Adw.Application):
    def __init__(self, backend: ApstaBackend):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.backend = backend
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

    def _on_activate(self, app):
        window = self.get_active_window()
        if window is None:
            if self.backend.available():
                window = ApstaWindow(app, self.backend)
            else:
                window = MissingApstaWindow(app, self.backend.apsta)
        window.present()


def main():
    raise SystemExit(ApstaApp(ApstaBackend()).run(sys.argv[:1]))
