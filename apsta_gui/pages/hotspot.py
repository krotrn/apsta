"""Hotspot tab: on/off hero, connection details, sharing, quick options."""

from __future__ import annotations

from gi.repository import Adw, Gtk

from ..compat import copy_to_clipboard, esc, switch_row
from ..helpers import band_label, hero_text, uplink


class HotspotPage:
    def __init__(self, window):
        self.window = window
        self._updating = False
        self._profiles: list = []
        self.widget = Adw.PreferencesPage()

        # ── hero ──────────────────────────────────────────────────────────────
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12)
        self.icon = Gtk.Image(pixel_size=96)
        self.icon.add_css_class("dim-label")
        self.title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.title.add_css_class("title-1")
        self.subtitle = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.subtitle.add_css_class("dim-label")
        self.toggle = Gtk.Button(halign=Gtk.Align.CENTER, margin_top=12)
        self.toggle.add_css_class("pill")
        self.toggle.set_size_request(200, -1)
        self.toggle.connect("clicked", lambda *_: self.toggle_hotspot())
        self.note = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, margin_top=4)
        for w in (self.icon, self.title, self.subtitle, self.toggle, self.note):
            hero.append(w)
        hero_group = Adw.PreferencesGroup()
        hero_group.add(hero)
        self.widget.add(hero_group)

        # ── details (while running) ───────────────────────────────────────────
        self.details = Adw.PreferencesGroup(title="Connection")
        self.ssid_row = Adw.ActionRow(title="Network name")
        copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Copy name")
        copy_btn.add_css_class("flat")
        copy_btn.connect("clicked", lambda *_: self._copy_ssid())
        self.ssid_row.add_suffix(copy_btn)
        self.share_row = Adw.ActionRow(title="Share", subtitle="Show a QR code and the password")
        share_btn = Gtk.Button(label="Share…", valign=Gtk.Align.CENTER)
        share_btn.connect("clicked", lambda *_: self.window.share())
        self.share_row.add_suffix(share_btn)
        self.share_row.set_activatable_widget(share_btn)
        self.channel_row = Adw.ActionRow(title="Band and channel")
        self.clients_row = Adw.ActionRow(title="Connected devices", activatable=True)
        self.clients_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.clients_row.connect("activated", lambda *_: self.window.show_page("clients"))
        self.iface_row = Adw.ActionRow(title="Interface")
        self.addr_row = Adw.ActionRow(title="Addresses")
        for row in (self.ssid_row, self.share_row, self.channel_row, self.clients_row, self.iface_row, self.addr_row):
            self.details.add(row)
        self.widget.add(self.details)

        # ── why it runs this way (while running) ──────────────────────────────
        self.why = Adw.PreferencesGroup(
            title="Why it runs this way",
            description="What apsta chose, and any setting it couldn't follow. Also in /var/log/apsta.log.",
        )
        self.why_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6)
        self.why.add(self.why_box)
        self.widget.add(self.why)
        self._notes: list = []

        # ── options (while stopped) ───────────────────────────────────────────
        self.options = Adw.PreferencesGroup(title="Options")
        self.profile_row = Adw.ComboRow(title="Profile", subtitle="Network name and password to use")
        self.profile_model = Gtk.StringList()
        self.profile_row.set_model(self.profile_model)
        self.profile_row.connect("notify::selected", self._on_profile_selected)
        self.allow_row, self.allow_switch = switch_row(
            "Allow disconnecting Wi-Fi",
            "Only for cards that can't host while connected. Wi-Fi drops while the hotspot runs.",
        )
        self.options.add(self.profile_row)
        self.options.add(self.allow_row)
        self.widget.add(self.options)

    # ── updates ───────────────────────────────────────────────────────────────

    def update(self, data: dict, detect: dict) -> None:
        hotspot = data.get("hotspot")
        title, subtitle, icon = hero_text(data)
        self.icon.set_from_icon_name(icon)
        (self.icon.remove_css_class if hotspot else self.icon.add_css_class)("dim-label")
        (self.icon.add_css_class if hotspot else self.icon.remove_css_class)("success")
        self.title.set_label(title)
        self.subtitle.set_label(subtitle)

        for cls in ("suggested-action", "destructive-action"):
            self.toggle.remove_css_class(cls)
        if not data:
            self.toggle.set_label("Try Again")
        else:
            self.toggle.set_label("Stop Hotspot" if hotspot else "Start Hotspot")
            self.toggle.add_css_class("destructive-action" if hotspot else "suggested-action")
        self.toggle.set_sensitive(not self.window.busy)

        self._update_note(data, detect)
        self._update_why((hotspot or {}).get("notes") or [])
        self.details.set_visible(bool(hotspot))
        self.options.set_visible(bool(data) and not hotspot)
        if hotspot:
            self.ssid_row.set_subtitle(esc(hotspot["ssid"]))
            self.channel_row.set_subtitle(f"{band_label(hotspot['band'])}, channel {hotspot['channel']}")
            self.clients_row.set_subtitle(str(len(data.get("clients") or [])))
            self.iface_row.set_subtitle(esc(f"{hotspot['ap_interface']} ({hotspot['method']})"))
            self.addr_row.set_visible(bool(hotspot.get("subnet")))
            self.addr_row.set_subtitle(esc(hotspot.get("subnet") or ""))
        self._update_profiles(data.get("config") or {})

        cap = (detect or {}).get("capability") or {}
        self.allow_row.set_visible(not cap or not cap.get("ap_sta"))
        has_options = self.profile_row.get_visible() or self.allow_row.get_visible()
        self.options.set_visible(bool(data) and not hotspot and has_options)

    def _update_why(self, notes: list) -> None:
        self.why.set_visible(bool(notes))
        if notes == self._notes:
            return
        self._notes = list(notes)
        child = self.why_box.get_first_child()
        while child is not None:
            self.why_box.remove(child)
            child = self.why_box.get_first_child()
        for note in notes:
            label = Gtk.Label(label=note, xalign=0, wrap=True, selectable=True)
            self.why_box.append(label)

    def _update_note(self, data: dict, detect: dict) -> None:
        for cls in ("success", "warning", "dim-label"):
            self.note.remove_css_class(cls)
        text, cls = "", "dim-label"
        if data.get("hotspot"):
            ssid = uplink(data)
            if ssid:
                text, cls = f"Still connected to {ssid}", "success"
            elif data["hotspot"]["method"] == "nmcli-single":
                text, cls = "Wi-Fi is disconnected while the hotspot runs", "warning"
        elif data.get("stale"):
            text, cls = "The last hotspot stopped unexpectedly. Starting again cleans it up.", "warning"
        elif (detect or {}).get("verdict", {}).get("mode") == "single":
            text, cls = "This Wi-Fi card can't host and stay connected at the same time.", "warning"
        self.note.set_label(text)
        self.note.add_css_class(cls)
        self.note.set_visible(bool(text))

    def _update_profiles(self, config: dict) -> None:
        names = config.get("profiles") or []
        active = config.get("active_profile")
        self._updating = True
        try:
            if names != self._profiles:
                self.profile_model.splice(0, self.profile_model.get_n_items(), names)
                self._profiles = list(names)
            if active in names:
                self.profile_row.set_selected(names.index(active))
            self.profile_row.set_visible(len(names) > 1)
        finally:
            self._updating = False

    # ── actions ───────────────────────────────────────────────────────────────

    def toggle_hotspot(self) -> None:
        if not self.window.data:
            self.window.refresh()
        elif self.window.data.get("hotspot"):
            self.window.run_privileged(self.window.backend.stop)
        else:
            allow = self.allow_switch.get_active()
            self.window.run_privileged(lambda: self.window.backend.start(allow_disconnect=allow))

    def _on_profile_selected(self, *_):
        if self._updating:
            return
        index = self.profile_row.get_selected()
        if 0 <= index < len(self._profiles):
            name = self._profiles[index]
            if name != (self.window.data.get("config") or {}).get("active_profile"):
                self.window.run_privileged(lambda: self.window.backend.use_profile(name))

    def _copy_ssid(self) -> None:
        hotspot = self.window.data.get("hotspot") or {}
        if hotspot.get("ssid"):
            copy_to_clipboard(self.widget, hotspot["ssid"])
            self.window.toast("Network name copied.")
