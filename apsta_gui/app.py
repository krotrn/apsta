#!/usr/bin/env python3
"""apsta-gtk application bootstrap and window wiring."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib
from pathlib import Path

from .mixins.actions import ApstaWindowActionsMixin
from .helpers import APP_ID, APSTA, POLL_INTERVAL
from .mixins.pages import ApstaWindowPagesMixin


class ApstaWindow(ApstaWindowPagesMixin, ApstaWindowActionsMixin, Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="apsta — Hotspot Manager")
        self.set_default_size(480, 620)
        self.set_resizable(True)

        self._build_ui()
        self._refresh_status()

        self._refreshing = False
        GLib.timeout_add_seconds(POLL_INTERVAL, self._on_poll_tick)


class ApstaApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        win = ApstaWindow(app)
        win.present()


def main():
    if not Path(APSTA).exists():
        print(f"Error: apsta not found at {APSTA}")
        print("Install apsta first: https://github.com/krotrn/apsta")
        raise SystemExit(1)

    app = ApstaApp()
    raise SystemExit(app.run(None))
