"""Clients tab: one row per connected device with speed-limit / disconnect / block actions."""

from __future__ import annotations

from typing import Dict

from gi.repository import Adw, Gtk

from ..compat import esc
from ..helpers import client_subtitle, client_title


class ClientRow:
    """A device row. Kept across refreshes so an open menu isn't destroyed."""

    def __init__(self, page: ClientsPage, mac: str):
        self.page = page
        self.mac = mac
        self.widget = Adw.ActionRow()
        self.widget.add_prefix(Gtk.Image(icon_name="computer-symbolic"))

        menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Actions")
        menu.add_css_class("flat")
        self.popover = Gtk.Popover()
        menu.set_popover(self.popover)
        self.widget.add_suffix(menu)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6
        )
        label = Gtk.Label(label="Speed limit (Mbit/s)", xalign=0)
        label.add_css_class("heading")
        self.rate = Gtk.SpinButton.new_with_range(0.1, 1000, 0.5)
        self.rate.set_digits(1)
        self.rate.set_value(8)
        limit_buttons = Gtk.Box(spacing=6, homogeneous=True)
        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", lambda *_: self._act(self._limit))
        self.remove_btn = Gtk.Button(label="Remove")
        self.remove_btn.connect("clicked", lambda *_: self._act(lambda b: b.unlimit(self.mac)))
        limit_buttons.append(self.apply_btn)
        limit_buttons.append(self.remove_btn)
        disconnect = Gtk.Button(label="Disconnect")
        disconnect.add_css_class("flat")
        disconnect.connect("clicked", lambda *_: self._act(lambda b: b.disconnect(self.mac)))
        self.block = Gtk.Button(label="Disconnect and block")
        self.block.add_css_class("flat")
        self.block.add_css_class("error")
        self.block.connect("clicked", lambda *_: self._act(lambda b: b.disconnect(self.mac, block=True)))
        for w in (label, self.rate, limit_buttons, Gtk.Separator(), disconnect, self.block):
            box.append(w)
        self.popover.set_child(box)

    def update(self, client: dict, can_block: bool) -> None:
        self.widget.set_title(esc(client_title(client)))
        self.widget.set_subtitle(esc(client_subtitle(client)))
        self.remove_btn.set_sensitive(bool(client.get("limit_kbps")))
        self.block.set_visible(can_block)

    def _limit(self, backend):
        return backend.limit(self.mac, int(self.rate.get_value() * 1000))

    def _act(self, call) -> None:
        self.popover.popdown()
        backend = self.page.window.backend
        self.page.window.run_privileged(lambda: call(backend))


class ClientsPage:
    def __init__(self, window):
        self.window = window
        self.rows: Dict[str, ClientRow] = {}
        self.blocked_rows: Dict[str, Adw.ActionRow] = {}

        self.widget = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.empty = Adw.StatusPage(icon_name="network-wireless-offline-symbolic")
        self.widget.add_named(self.empty, "empty")

        self.page = Adw.PreferencesPage()
        self.group = Adw.PreferencesGroup(title="Connected devices")
        self.blocked = Adw.PreferencesGroup(
            title="Blocked devices", description="These devices can't join until unblocked."
        )
        self.page.add(self.group)
        self.page.add(self.blocked)
        self.widget.add_named(self.page, "list")

    def update(self, data: dict, detect: dict) -> None:
        hotspot = data.get("hotspot")
        clients = data.get("clients") or []
        blocked = (hotspot or {}).get("blocked") or []
        if not hotspot:
            self.empty.set_title("Hotspot is off")
            self.empty.set_description("Devices that join your hotspot appear here.")
            self.widget.set_visible_child_name("empty")
            return
        if not clients and not blocked:
            self.empty.set_icon_name("network-wireless-hotspot-symbolic")
            self.empty.set_title("No devices yet")
            self.empty.set_description(
                f"Join “{esc(hotspot['ssid'])}” from another device, or use Share on the Hotspot tab."
            )
            self.widget.set_visible_child_name("empty")
            return
        self.empty.set_icon_name("network-wireless-offline-symbolic")
        self.widget.set_visible_child_name("list")

        can_block = hotspot.get("method") == "hostapd"
        self.group.set_description(None if can_block else "Blocking devices needs hostapd mode.")
        self._sync_rows(clients, can_block)
        self._sync_blocked(blocked)

    def _sync_rows(self, clients, can_block) -> None:
        seen = set()
        for client in clients:
            mac = client["mac"]
            seen.add(mac)
            if mac not in self.rows:
                self.rows[mac] = ClientRow(self, mac)
                self.group.add(self.rows[mac].widget)
            self.rows[mac].update(client, can_block)
        for mac in list(self.rows):
            if mac not in seen:
                self.group.remove(self.rows.pop(mac).widget)
        self.group.set_visible(bool(self.rows))
        self.group.set_title(f"Connected devices ({len(self.rows)})")

    def _sync_blocked(self, blocked) -> None:
        for mac in list(self.blocked_rows):
            if mac not in blocked:
                self.blocked.remove(self.blocked_rows.pop(mac))
        for mac in blocked:
            if mac not in self.blocked_rows:
                row = Adw.ActionRow(title=esc(mac))
                button = Gtk.Button(label="Unblock", valign=Gtk.Align.CENTER)
                button.connect(
                    "clicked", lambda _b, m=mac: self.window.run_privileged(lambda: self.window.backend.unblock(m))
                )
                row.add_suffix(button)
                self.blocked_rows[mac] = row
                self.blocked.add(row)
        self.blocked.set_visible(bool(self.blocked_rows))
