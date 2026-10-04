"""Main window: header, tabs, toasts, background work and periodic refresh."""

from __future__ import annotations

import threading

from gi.repository import Adw, Gio, GLib, Gtk

from .compat import esc
from .helpers import POLL_INTERVAL
from .pages.clients import ClientsPage
from .pages.hotspot import HotspotPage
from .pages.settings import SettingsPage
from .share import ShareDialog


class ApstaWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, backend):
        super().__init__(application=app, title="Hotspot", default_width=460, default_height=760)
        self.set_size_request(360, 480)
        self.backend = backend
        self.data: dict = {}
        self.detect: dict = {}
        self.busy = False
        self._refreshing = False

        self.hotspot_page = HotspotPage(self)
        self.clients_page = ClientsPage(self)
        self.settings_page = SettingsPage(self)
        self.pages = (self.hotspot_page, self.clients_page, self.settings_page)

        self.stack = Adw.ViewStack()
        for page, name, title, icon in (
            (self.hotspot_page, "hotspot", "Hotspot", "network-wireless-hotspot-symbolic"),
            (self.clients_page, "clients", "Devices", "computer-symbolic"),
            (self.settings_page, "settings", "Settings", "emblem-system-symbolic"),
        ):
            # add_titled + set_icon_name instead of add_titled_with_icon (libadwaita 1.2)
            self.stack.add_titled(page.widget, name, title).set_icon_name(icon)

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.NARROW)
        header.set_title_widget(switcher)
        self.spinner = Gtk.Spinner()
        header.pack_start(self.spinner)
        menu = Gio.Menu()
        menu.append("Refresh", "win.refresh")
        menu.append("About apsta", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        layout.append(header)
        layout.append(self.toasts)
        self.toasts.set_vexpand(True)
        self.set_content(layout)

        refresh = Gio.SimpleAction.new("refresh", None)
        refresh.connect("activate", lambda *_: self.refresh())
        self.add_action(refresh)

        self.refresh()
        self.run_async(self.backend.detect, self._on_detect)
        self._poll_id = GLib.timeout_add_seconds(POLL_INTERVAL, self._on_poll)
        self.connect("close-request", self._on_close)

    # ── background work ───────────────────────────────────────────────────────

    def run_async(self, work, done=None) -> None:
        """Run ``work()`` in a thread, then ``done(result)`` on the main loop."""

        def runner():
            result = work()
            if done is not None:
                GLib.idle_add(lambda: done(result) and False)

        threading.Thread(target=runner, daemon=True).start()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.spinner.set_spinning(busy)
        self.hotspot_page.toggle.set_sensitive(not busy)

    def run_privileged(self, work, on_success=None) -> None:
        """Run a privileged backend call, show its outcome as a toast and refresh."""
        if self.busy:
            self.toast("Please wait for the current action to finish.")
            return
        self.set_busy(True)

        def done(result):
            self.set_busy(False)
            if result.message:
                self.toast(result.message)
            if result.ok and on_success:
                on_success()
            self.refresh()

        self.run_async(work, done)

    # ── refresh ───────────────────────────────────────────────────────────────

    def _on_poll(self) -> bool:
        self.refresh()
        return True

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.run_async(self.backend.status, self._on_status)

    def _on_status(self, data: dict) -> None:
        self._refreshing = False
        self.data = data or {}
        for page in self.pages:
            page.update(self.data, self.detect)

    def _on_detect(self, detect: dict) -> None:
        self.detect = detect or {}
        self.settings_page.update_hardware(self.detect)
        self.hotspot_page.update(self.data, self.detect)

    def _on_close(self, *_):
        GLib.source_remove(self._poll_id)
        return False

    # ── helpers for pages ─────────────────────────────────────────────────────

    def toast(self, message: str) -> None:
        toast = Adw.Toast.new(esc(message))
        toast.set_timeout(4)
        self.toasts.add_toast(toast)

    def show_page(self, name: str) -> None:
        self.stack.set_visible_child_name(name)

    def share(self) -> None:
        """Show the share dialog; reading the saved password needs authentication."""
        if self.busy:
            return
        self.set_busy(True)

        def done(result):
            self.set_busy(False)
            if not result.ok:
                self.toast(result.message)
                return
            ssid, password = result.data.get("settings", {}).get("ssid"), result.data.get("password")
            if not password:
                self.toast("No password is set yet; start the hotspot once to generate one.")
                return
            ShareDialog(self, ssid, password).present()

        self.run_async(self.backend.secrets, done)


class MissingApstaWindow(Adw.ApplicationWindow):
    """Shown instead of exiting silently when the CLI isn't installed."""

    def __init__(self, app: Adw.Application, path: str):
        super().__init__(application=app, title="Hotspot", default_width=460, default_height=400)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        layout.append(Adw.HeaderBar())
        page = Adw.StatusPage(
            icon_name="dialog-warning-symbolic",
            title="apsta is not installed",
            description=f"The apsta command was not found at {esc(path)}.\n"
            "Install it from https://github.com/krotrn/apsta",
            vexpand=True,
        )
        layout.append(page)
        self.set_content(layout)
