#!/usr/bin/env python3
"""
apsta-gtk — GTK4 / Libadwaita GUI for apsta

Works on any desktop that runs GTK4: GNOME, KDE (with GTK theme), Xfce,
MATE, Cinnamon, and plain window managers. Does NOT require COSMIC.

Requires:
    python3-gi  (PyGObject)
    gir1.2-gtk-4.0
    gir1.2-adw-1  (Libadwaita)
    apsta installed at /usr/local/bin/apsta

Install deps:
    sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
"""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk, Adw, GLib, Gio, Pango

import json
import os
import shlex
import subprocess
import threading
import shutil
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

APP_ID   = "com.github.apsta.Gtk"
APSTA    = shutil.which("apsta") or "/usr/local/bin/apsta"
CONFIG   = Path("/etc/apsta/config.json")
VERSION  = "0.5.1"

# Background poll interval in seconds — keeps status in sync with daemon
POLL_INTERVAL = 5


# ── Helpers ────────────────────────────────────────────────────────────────────

def read_config() -> dict:
    """Read /etc/apsta/config.json. Returns {} on any error."""
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def run_apsta(*args: str) -> tuple[int, str, str]:
    """
    Run apsta with the given arguments. Returns (returncode, stdout, stderr).
    Does NOT use pkexec — for read-only commands (detect, status, scan-usb).
    """
    try:
        r = subprocess.run(
            [APSTA, *args],
            capture_output=True, text=True,
            env={**os.environ, "NO_COLOR": "1"},
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"apsta not found at {APSTA}"


def run_apsta_root_script(script: str, *positional: str) -> tuple[int, str, str]:
    """
    Run a shell script via pkexec, passing user-supplied values as positional
    arguments ($1, $2, ...) rather than interpolating them into the script.
    This prevents shell injection from SSID/password values.

    pkexec exit codes:
        0   — success
        126 — user cancelled the auth dialog
        127 — command not found
    """
    try:
        r = subprocess.run(
            ["pkexec", "sh", "-c", script, "--", *positional],
            capture_output=True, text=True,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return 127, "", "pkexec not found — cannot escalate privileges"


def pkexec_error_message(returncode: int, stderr: str, stdout: str = "") -> str:
    """Translate pkexec exit codes and apsta output into human-readable messages.

    apsta writes its error messages to stdout (coloured terminal output) not
    stderr, so we check stdout first when stderr is empty.
    """
    if returncode == 126:
        return "Authentication cancelled."
    if returncode == 127:
        return "pkexec or apsta not found. Is apsta installed?"
    raw = stderr or stdout or "Unknown error"
    return strip_ansi(raw).strip()[:200]


def strip_ansi(s: str) -> str:
    """Remove ANSI ESC[...m sequences. Requires exact ESC[ prefix."""
    result = []
    i = 0
    while i < len(s):
        if s[i] == "\x1b" and i + 1 < len(s) and s[i + 1] == "[":
            i += 2
            while i < len(s) and s[i] != "m":
                i += 1
            i += 1  # skip 'm'
        else:
            result.append(s[i])
            i += 1
    return "".join(result)


def first_error_line(text: str) -> str:
    """Extract the first meaningful error line from apsta output.

    apsta prefixes errors with ✘ and warnings with ⚠. We look for those
    specifically so we skip headers like 'apsta — Configuration'.
    Falls back to the first non-empty, non-header line if none found.
    """
    lines = text.splitlines()

    # First pass: look for explicit error/warning markers
    for line in lines:
        stripped = line.strip()
        if any(marker in stripped for marker in ("✘", "⚠", "Error", "error", "failed", "Failed")):
            # Remove the marker prefix characters for cleaner display
            clean = stripped.lstrip("✘⚠ ").strip()
            if clean:
                return clean

    # Second pass: skip headers (contain —) and nav lines (start with →)
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("→"):
            continue
        if "—" in stripped:  # header lines like "apsta — Configuration"
            continue
        return stripped

    return text[:120]  # fallback: return the first 120 chars of the raw output

# ── Main Window ────────────────────────────────────────────────────────────────

class ApstaWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="apsta — Hotspot Manager")
        self.set_default_size(480, 620)
        self.set_resizable(True)

        # Navigation view — three pages stacked via Adw.ViewStack
        self._build_ui()
        self._refresh_status()

        # Background poll: update status every POLL_INTERVAL seconds
        # GLib.timeout_add_seconds fires on the main loop — safe for UI updates
        self._refreshing = False  # guard against overlapping polls
        GLib.timeout_add_seconds(POLL_INTERVAL, self._on_poll_tick)

    # ── UI construction ────────────────────────────────────────────────────────

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

    def _refresh_status(self):
        """Read config.json directly (0o644 — no root needed) and update UI."""
        cfg = read_config()
        ap_iface = cfg.get("ap_interface") or ""
        active   = bool(ap_iface)
        ssid     = cfg.get("ssid") or "—"

        if active:
            self._status_row.set_subtitle("Active")
            self._status_icon.set_from_icon_name("network-wireless-hotspot-symbolic")
            self._ssid_status_row.set_subtitle(ssid)
            self._iface_row.set_subtitle(ap_iface)
            # Get channel from iw asynchronously
            threading.Thread(
                target=self._fetch_channel_info,
                args=(ap_iface,),
                daemon=True,
            ).start()
        else:
            self._status_row.set_subtitle("Inactive")
            self._status_icon.set_from_icon_name("network-wireless-symbolic")
            self._ssid_status_row.set_subtitle("—")
            self._iface_row.set_subtitle("—")
            self._channel_row.set_subtitle("—")

        # Keep control fields pre-filled with current config
        if ssid and ssid != "—":
            self._ssid_entry.set_text(ssid)
        pwd = cfg.get("password") or ""
        if pwd:
            self._pass_entry.set_text(pwd)

        self._start_btn.set_sensitive(not active)
        self._stop_btn.set_sensitive(active)

    def _fetch_channel_info(self, iface: str):
        """Run `iw dev <iface> info` in a thread; update UI via GLib.idle_add."""
        try:
            r = subprocess.run(
                ["iw", "dev", iface, "info"],
                capture_output=True, text=True,
            )
            channel, band = "", ""
            for line in r.stdout.splitlines():
                line = line.strip()
                if line.startswith("channel "):
                    parts = line.split()
                    if len(parts) >= 3:
                        channel = parts[1]
                        freq_str = parts[2].lstrip("(")
                        try:
                            freq = int(freq_str)
                            band = "5 GHz" if freq >= 5000 else "2.4 GHz"
                        except ValueError:
                            pass
                    break
            label = f"ch{channel} ({band})" if channel else "—"
            GLib.idle_add(self._channel_row.set_subtitle, label)
        except Exception:
            GLib.idle_add(self._channel_row.set_subtitle, "—")

    def _on_poll_tick(self) -> bool:
        """
        Background poll — called by GLib every POLL_INTERVAL seconds.
        Must return True to keep the timer alive.
        Guard with _refreshing so overlapping polls can't stack up if a
        config read takes longer than the poll interval.
        """
        if not self._refreshing:
            self._refreshing = True
            threading.Thread(target=self._poll_worker, daemon=True).start()
        return True  # keep polling

    def _poll_worker(self):
        # Run refresh on the main loop thread via idle_add, but wait for it
        # to complete before clearing the guard — prevents a second poll from
        # starting before the first one finishes updating the UI.
        done = threading.Event()
        def _run():
            self._refresh_status()
            done.set()
            return False
        GLib.idle_add(_run)
        done.wait()
        self._refreshing = False

    def _load_config_into_settings(self):
        cfg = read_config()
        self._cfg_ssid.set_text(cfg.get("ssid") or "")
        self._cfg_pass.set_text(cfg.get("password") or "")
        self._cfg_iface.set_text(cfg.get("interface") or "")

    # ── Button handlers ────────────────────────────────────────────────────────

    def _on_start_clicked(self, _btn):
        ssid = self._ssid_entry.get_text().strip()
        pwd  = self._pass_entry.get_text().strip()
        if not ssid or not pwd:
            self._show_banner("SSID and password cannot be empty.", error=True)
            return
        force = self._force_switch.get_active()
        self._set_busy(True)
        threading.Thread(
            target=self._bg_start, args=(ssid, pwd, force), daemon=True
        ).start()

    def _bg_start(self, ssid: str, pwd: str, force: bool):
        force_flag = "--force" if force else ""
        script = (
            f"{APSTA} config --set ssid=\"$1\" && "
            f"{APSTA} config --set password=\"$2\" && "
            f"{APSTA} start {force_flag}".strip()
        )
        rc, stdout, stderr = run_apsta_root_script(script, ssid, pwd)
        if rc == 0:
            GLib.idle_add(self._on_action_done, True, "Hotspot started.")
        else:
            # apsta writes errors to stdout (coloured terminal output),
            # stderr is often empty. Show the first meaningful error line.
            raw = strip_ansi(stderr or stdout or "Unknown error").strip()
            msg = first_error_line(raw)
            GLib.idle_add(self._on_action_done, False, msg)

    def _on_stop_clicked(self, _btn):
        self._set_busy(True)
        threading.Thread(target=self._bg_stop, daemon=True).start()

    def _bg_stop(self):
        rc, stdout, stderr = run_apsta_root_script(f"{APSTA} stop")
        if rc == 0:
            GLib.idle_add(self._on_action_done, True, "Hotspot stopped.")
        else:
            GLib.idle_add(self._on_action_done, False, pkexec_error_message(rc, stderr, stdout))

    def _on_action_done(self, success: bool, message: str):
        self._set_busy(False)
        self._show_banner(message, error=not success)
        self._refresh_status()

    def _on_detect_clicked(self, _btn):
        self._detect_buf.set_text("Running detect…")
        threading.Thread(target=self._bg_detect, daemon=True).start()

    def _bg_detect(self):
        rc, stdout, stderr = run_apsta("detect")
        output = strip_ansi(stdout if rc == 0 else stderr or stdout)
        GLib.idle_add(self._detect_buf.set_text, output)

    def _on_usb_scan_clicked(self, _btn):
        self._usb_buf.set_text("Scanning…")
        threading.Thread(target=self._bg_usb_scan, daemon=True).start()

    def _bg_usb_scan(self):
        rc, stdout, stderr = run_apsta("scan-usb")
        output = strip_ansi(stdout if rc == 0 else stderr or stdout)
        GLib.idle_add(self._usb_buf.set_text, output or "No USB WiFi adapters found.")

    def _on_recommend_clicked(self, _btn):
        self._rec_buf.set_text("Checking…")
        threading.Thread(target=self._bg_recommend, daemon=True).start()

    def _bg_recommend(self):
        rc, stdout, stderr = run_apsta("recommend")
        output = strip_ansi(stdout if rc == 0 else stderr or stdout)
        GLib.idle_add(self._rec_buf.set_text, output)

    def _on_save_config_clicked(self, _btn):
        ssid  = self._cfg_ssid.get_text().strip()
        pwd   = self._cfg_pass.get_text().strip()
        iface = self._cfg_iface.get_text().strip()

        if not ssid or not pwd:
            self._show_banner("SSID and password cannot be empty.", error=True)
            return

        # Build a single pkexec call for all config changes
        cmds = [f"{APSTA} config --set ssid=\"$1\""]
        args = [ssid]

        cmds.append(f"{APSTA} config --set password=\"$2\"")
        args.append(pwd)

        if iface:
            # Interface uses $3
            cmds.append(f"{APSTA} config --set interface=\"$3\"")
            args.append(iface)
        else:
            cmds.append(f"{APSTA} config --set interface=none")

        script = " && ".join(cmds)
        threading.Thread(
            target=self._bg_save_config,
            args=(script, args),
            daemon=True,
        ).start()

    def _bg_save_config(self, script: str, args: list):
        rc, stdout, stderr = run_apsta_root_script(script, *args)
        if rc == 0:
            GLib.idle_add(self._show_banner, "Configuration saved.", False)
            GLib.idle_add(self._load_config_into_settings)
        else:
            GLib.idle_add(
                self._show_banner,
                pkexec_error_message(rc, stderr, stdout),
                True,
            )

    def _on_enable_clicked(self, _btn):
        threading.Thread(target=self._bg_enable, daemon=True).start()

    def _bg_enable(self):
        rc, stdout, stderr = run_apsta_root_script(f"{APSTA} enable")
        msg = "Auto-start enabled." if rc == 0 else pkexec_error_message(rc, stderr, stdout)
        GLib.idle_add(self._show_banner, msg, rc != 0)

    def _on_disable_clicked(self, _btn):
        threading.Thread(target=self._bg_disable, daemon=True).start()

    def _bg_disable(self):
        rc, stdout, stderr = run_apsta_root_script(f"{APSTA} disable")
        msg = "Auto-start disabled." if rc == 0 else pkexec_error_message(rc, stderr, stdout)
        GLib.idle_add(self._show_banner, msg, rc != 0)

    # ── UI utilities ───────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        """Disable buttons while a privileged operation is in progress."""
        self._start_btn.set_sensitive(not busy)
        self._stop_btn.set_sensitive(not busy)
        if busy:
            self._start_btn.set_label("Working…")
            self._stop_btn.set_label("Working…")
        else:
            self._start_btn.set_label("Start Hotspot")
            self._stop_btn.set_label("Stop Hotspot")

    def _show_banner(self, message: str, error: bool = False):
        """Show the Adw.Banner at the top of the Status page."""
        self._banner.set_title(message)
        if error:
            self._banner.add_css_class("error")
        else:
            self._banner.remove_css_class("error")
        self._banner.set_revealed(True)

        # Cancel the existing hide timer before starting a new one.
        # Without this, rapid successive actions (start → stop within 4s)
        # leave a stale timer that prematurely hides the newer banner.
        if self._banner_timeout_id is not None:
            GLib.source_remove(self._banner_timeout_id)

        self._banner_timeout_id = GLib.timeout_add(4000, self._hide_banner)

    def _hide_banner(self) -> bool:
        self._banner.set_revealed(False)
        self._banner_timeout_id = None
        return False  # returning False removes the timer from the GLib main loop


# ── Application ────────────────────────────────────────────────────────────────

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


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    # Verify apsta is installed before opening the window
    if not Path(APSTA).exists():
        print(f"Error: apsta not found at {APSTA}")
        print("Install apsta first: https://github.com/krotrn/apsta")
        raise SystemExit(1)

    app = ApstaApp()
    raise SystemExit(app.run(None))


if __name__ == "__main__":
    main()