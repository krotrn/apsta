"""apsta-gtk: application bootstrap and window wiring."""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from .backend import ApstaBackend  # noqa: E402
from .helpers import APP_ID, POLL_INTERVAL  # noqa: E402
from .mixins.actions import ApstaWindowActionsMixin  # noqa: E402
from .mixins.pages import ApstaWindowPagesMixin  # noqa: E402


class ApstaWindow(ApstaWindowPagesMixin, ApstaWindowActionsMixin, Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, backend: ApstaBackend):
        super().__init__(application=app, title="apsta — Hotspot Manager")
        self.set_default_size(480, 620)
        self._backend = backend
        self._refreshing = False
        self._busy = False
        self._synced = {}  # entry key -> last value written from config

        self._build_ui()
        self._request_refresh()
        GLib.timeout_add_seconds(POLL_INTERVAL, self._on_poll_tick)


class ApstaApp(Adw.Application):
    def __init__(self, backend: ApstaBackend):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self._backend = backend
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        window = self.get_active_window() or ApstaWindow(app, self._backend)
        window.present()


def main():
    backend = ApstaBackend()
    if not backend.available():
        print(f"Error: apsta not found at {backend.apsta}", file=sys.stderr)
        print("Install apsta first: https://github.com/krotrn/apsta", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(ApstaApp(backend).run(None))
