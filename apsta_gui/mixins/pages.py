#!/usr/bin/env python3
"""UI construction mixin for the GTK window."""

from gi.repository import Adw, Gtk


class ApstaWindowPagesMixin:
    def _build_ui(self):
        # Root: ToolbarView wraps a ViewStack with a top HeaderBar
        toolbar_view = Adw.ToolbarView()

        # Header bar with view switcher
        header = Adw.HeaderBar()
        self._view_switcher = Adw.ViewSwitcher()
        self._view_switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self._view_switcher)
        toolbar_view.add_top_bar(header)

        # ViewStack — three pages
        self._stack = Adw.ViewStack()
        self._view_switcher.set_stack(self._stack)

        self._stack.add_titled_with_icon(
            self._build_status_page(),
            "status", "Status",
            "network-wireless-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._build_hardware_page(),
            "hardware", "Hardware",
            "computer-symbolic",
        )
        self._stack.add_titled_with_icon(
            self._build_settings_page(),
            "settings", "Settings",
            "preferences-system-symbolic",
        )

        toolbar_view.set_content(self._stack)
        self.set_content(toolbar_view)

    # ── Status page ───────────────────────────────────────────────────────────

    def _build_status_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        # ── Status group ──
        status_group = Adw.PreferencesGroup(title="Hotspot Status")

        self._status_row = Adw.ActionRow(
            title="Status",
            subtitle="Loading…",
        )
        self._status_icon = Gtk.Image.new_from_icon_name("network-wireless-symbolic")
        self._status_row.add_prefix(self._status_icon)
        status_group.add(self._status_row)

        self._ssid_status_row = Adw.ActionRow(title="SSID", subtitle="—")
        status_group.add(self._ssid_status_row)

        self._iface_row = Adw.ActionRow(title="Interface", subtitle="—")
        status_group.add(self._iface_row)

        self._channel_row = Adw.ActionRow(title="Channel / Band", subtitle="—")
        status_group.add(self._channel_row)

        page.add(status_group)

        # ── Control group ──
        control_group = Adw.PreferencesGroup(title="Control")

        self._ssid_entry = Adw.EntryRow(title="SSID")
        self._ssid_entry.set_text("apsta-hotspot")
        control_group.add(self._ssid_entry)

        self._pass_entry = Adw.PasswordEntryRow(title="Password")
        self._pass_entry.set_text("changeme123")
        control_group.add(self._pass_entry)

        # Force mode toggle — needed when AP+STA concurrent is not supported.
        # When enabled, apsta start --force is passed, which disconnects the
        # existing WiFi connection and uses the single interface as AP.
        force_row = Adw.ActionRow(
            title="Force start",
            subtitle="Disconnect WiFi to run hotspot (single-radio cards)",
        )
        self._force_switch = Gtk.Switch()
        self._force_switch.set_valign(Gtk.Align.CENTER)
        force_row.add_suffix(self._force_switch)
        force_row.set_activatable_widget(self._force_switch)
        control_group.add(force_row)

        # Start / Stop button row
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_margin_top(4)
        btn_box.set_halign(Gtk.Align.CENTER)

        self._start_btn = Gtk.Button(label="Start Hotspot")
        self._start_btn.add_css_class("suggested-action")
        self._start_btn.add_css_class("pill")
        self._start_btn.connect("clicked", self._on_start_clicked)

        self._stop_btn = Gtk.Button(label="Stop Hotspot")
        self._stop_btn.add_css_class("destructive-action")
        self._stop_btn.add_css_class("pill")
        self._stop_btn.connect("clicked", self._on_stop_clicked)

        btn_box.append(self._start_btn)
        btn_box.append(self._stop_btn)

        btn_row = Adw.ActionRow()
        btn_row.set_activatable(False)
        btn_row.set_child(btn_box)
        control_group.add(btn_row)

        share_row = Adw.ActionRow(
            title="Share hotspot",
            subtitle="Render QR or copy Wi-Fi share string",
        )

        share_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        show_qr_btn = Gtk.Button(label="Show QR")
        show_qr_btn.add_css_class("pill")
        show_qr_btn.set_valign(Gtk.Align.CENTER)
        show_qr_btn.connect("clicked", self._on_show_wifi_qr_clicked)

        share_btn = Gtk.Button(label="Copy Share String")
        share_btn.add_css_class("pill")
        share_btn.set_valign(Gtk.Align.CENTER)
        share_btn.connect("clicked", self._on_copy_wifi_uri_clicked)

        share_box.append(show_qr_btn)
        share_box.append(share_btn)
        share_row.add_suffix(share_box)
        control_group.add(share_row)

        qr_row = Adw.ActionRow(title="QR Code", subtitle="Scan to join hotspot")
        qr_row.set_activatable(False)

        qr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        qr_box.set_margin_top(6)
        qr_box.set_margin_bottom(6)

        self._qr_picture = Gtk.Picture()
        self._qr_picture.set_size_request(220, 220)
        self._qr_picture.set_halign(Gtk.Align.CENTER)

        self._qr_hint = Gtk.Label(label="Press 'Show QR' to generate a scannable code.")
        self._qr_hint.set_halign(Gtk.Align.CENTER)
        self._qr_hint.add_css_class("dim-label")

        qr_box.append(self._qr_picture)
        qr_box.append(self._qr_hint)
        qr_row.set_child(qr_box)
        control_group.add(qr_row)

        page.add(control_group)

        # ── Feedback banner ──
        self._banner = Adw.Banner(title="")
        self._banner.set_revealed(False)
        self._banner_timeout_id = None  # track timer so we can cancel before creating a new one
        # Banners go above the scroll content — wrap page in a box
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(self._banner)
        outer.append(page)
        return outer

    # ── Hardware page ─────────────────────────────────────────────────────────

    def _build_hardware_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        # Detect group
        detect_group = Adw.PreferencesGroup(title="Built-in Card Detection")
        detect_btn_row = Adw.ActionRow(title="Run hardware detection")
        detect_btn = Gtk.Button(label="Detect")
        detect_btn.add_css_class("pill")
        detect_btn.set_valign(Gtk.Align.CENTER)
        detect_btn.connect("clicked", self._on_detect_clicked)
        detect_btn_row.add_suffix(detect_btn)
        detect_group.add(detect_btn_row)

        # Output text view (read-only, monospace, scrollable)
        self._detect_buf = Gtk.TextBuffer()
        self._detect_buf.set_text("Press Detect to check your hardware.")
        detect_tv = Gtk.TextView(buffer=self._detect_buf)
        detect_tv.set_editable(False)
        detect_tv.set_monospace(True)
        detect_tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        detect_tv.set_margin_start(8)
        detect_tv.set_margin_end(8)
        detect_tv.set_margin_top(8)
        detect_tv.set_margin_bottom(8)

        detect_scroll = Gtk.ScrolledWindow()
        detect_scroll.set_child(detect_tv)
        detect_scroll.set_min_content_height(160)
        detect_scroll.set_vexpand(False)

        detect_output_row = Adw.ActionRow()
        detect_output_row.set_activatable(False)
        detect_output_row.set_child(detect_scroll)
        detect_group.add(detect_output_row)
        page.add(detect_group)

        # USB scan group
        usb_group = Adw.PreferencesGroup(title="USB WiFi Adapters")
        usb_btn_row = Adw.ActionRow(title="Scan for USB WiFi adapters")
        usb_btn = Gtk.Button(label="Scan")
        usb_btn.add_css_class("pill")
        usb_btn.set_valign(Gtk.Align.CENTER)
        usb_btn.connect("clicked", self._on_usb_scan_clicked)
        usb_btn_row.add_suffix(usb_btn)
        usb_group.add(usb_btn_row)

        self._usb_buf = Gtk.TextBuffer()
        self._usb_buf.set_text("Press Scan to check for USB WiFi adapters.")
        usb_tv = Gtk.TextView(buffer=self._usb_buf)
        usb_tv.set_editable(False)
        usb_tv.set_monospace(True)
        usb_tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        usb_tv.set_margin_start(8)
        usb_tv.set_margin_end(8)
        usb_tv.set_margin_top(8)
        usb_tv.set_margin_bottom(8)

        usb_scroll = Gtk.ScrolledWindow()
        usb_scroll.set_child(usb_tv)
        usb_scroll.set_min_content_height(120)

        usb_output_row = Adw.ActionRow()
        usb_output_row.set_activatable(False)
        usb_output_row.set_child(usb_scroll)
        usb_group.add(usb_output_row)

        # Recommend group
        rec_group = Adw.PreferencesGroup(title="Recommendations")
        rec_btn_row = Adw.ActionRow(title="Suggest a USB adapter to buy")
        rec_btn = Gtk.Button(label="Recommend")
        rec_btn.add_css_class("pill")
        rec_btn.set_valign(Gtk.Align.CENTER)
        rec_btn.connect("clicked", self._on_recommend_clicked)
        rec_btn_row.add_suffix(rec_btn)
        rec_group.add(rec_btn_row)

        self._rec_buf = Gtk.TextBuffer()
        self._rec_buf.set_text("Press Recommend to see adapter suggestions.")
        rec_tv = Gtk.TextView(buffer=self._rec_buf)
        rec_tv.set_editable(False)
        rec_tv.set_monospace(True)
        rec_tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        rec_tv.set_margin_start(8)
        rec_tv.set_margin_end(8)
        rec_tv.set_margin_top(8)
        rec_tv.set_margin_bottom(8)

        rec_scroll = Gtk.ScrolledWindow()
        rec_scroll.set_child(rec_tv)
        rec_scroll.set_min_content_height(120)

        rec_output_row = Adw.ActionRow()
        rec_output_row.set_activatable(False)
        rec_output_row.set_child(rec_scroll)
        rec_group.add(rec_output_row)

        page.add(usb_group)
        page.add(rec_group)
        return page

    # ── Settings page ─────────────────────────────────────────────────────────

    def _build_settings_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        cfg_group = Adw.PreferencesGroup(title="Hotspot Configuration")

        self._cfg_ssid  = Adw.EntryRow(title="SSID")
        self._cfg_pass  = Adw.PasswordEntryRow(title="Password")
        self._cfg_iface = Adw.EntryRow(title="Interface (leave blank = auto)")

        cfg_group.add(self._cfg_ssid)
        cfg_group.add(self._cfg_pass)
        cfg_group.add(self._cfg_iface)

        save_row = Adw.ActionRow()
        save_btn = Gtk.Button(label="Save Configuration")
        save_btn.add_css_class("suggested-action")
        save_btn.add_css_class("pill")
        save_btn.set_margin_top(8)
        save_btn.set_margin_bottom(8)
        save_btn.set_halign(Gtk.Align.CENTER)
        save_btn.connect("clicked", self._on_save_config_clicked)
        save_row.set_activatable(False)
        save_row.set_child(save_btn)
        cfg_group.add(save_row)

        page.add(cfg_group)

        # Service group
        svc_group = Adw.PreferencesGroup(title="Auto-start Service")

        enable_row = Adw.ActionRow(
            title="Enable auto-start",
            subtitle="Installs systemd service + sleep hook",
        )
        enable_btn = Gtk.Button(label="Enable")
        enable_btn.add_css_class("pill")
        enable_btn.set_valign(Gtk.Align.CENTER)
        enable_btn.connect("clicked", self._on_enable_clicked)
        enable_row.add_suffix(enable_btn)
        svc_group.add(enable_row)

        disable_row = Adw.ActionRow(
            title="Disable auto-start",
            subtitle="Removes systemd service + sleep hook",
        )
        disable_btn = Gtk.Button(label="Disable")
        disable_btn.add_css_class("pill")
        disable_btn.add_css_class("destructive-action")
        disable_btn.set_valign(Gtk.Align.CENTER)
        disable_btn.connect("clicked", self._on_disable_clicked)
        disable_row.add_suffix(disable_btn)
        svc_group.add(disable_row)

        page.add(svc_group)

        # Load current config into fields
        self._load_config_into_settings()
        return page

    # ── Status helpers ─────────────────────────────────────────────────────────
