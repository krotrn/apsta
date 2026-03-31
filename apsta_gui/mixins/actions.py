#!/usr/bin/env python3
"""Status, actions, and background worker mixin for the GTK window."""

import io
import json
import os
import subprocess
import threading

from gi.repository import Gdk, GdkPixbuf, GLib

from ..helpers import (
    APSTA,
    first_error_line,
    pkexec_error_message,
    read_config,
    run_apsta,
    run_apsta_root_script,
    strip_ansi,
)


class ApstaWindowActionsMixin:
    @staticmethod
    def _escape_wifi_field(value: str) -> str:
        escaped = value.replace("\\", "\\\\")
        escaped = escaped.replace(";", "\\;")
        escaped = escaped.replace(",", "\\,")
        escaped = escaped.replace(":", "\\:")
        return escaped

    def _build_wifi_share_string(self) -> str:
        ssid = self._ssid_entry.get_text().strip() or self._ssid_status_row.get_subtitle().strip()
        password = self._pass_entry.get_text().strip()
        if not ssid or ssid == "—":
            return ""

        ssid = self._escape_wifi_field(ssid)
        password = self._escape_wifi_field(password)
        return f"WIFI:T:WPA;S:{ssid};P:{password};;"

    def _render_wifi_qr(self, payload: str) -> bool:
        try:
            import qrcode
        except ImportError:
            self._show_banner("QR renderer missing. Install python3-qrcode and python3-pil.", error=True)
            self._qr_hint.set_label("QR library missing: install python3-qrcode and python3-pil.")
            return False

        try:
            qr = qrcode.QRCode(
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=8,
                border=2,
            )
            qr.add_data(payload)
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white")

            png_buf = io.BytesIO()
            image.save(png_buf, format="PNG")

            loader = GdkPixbuf.PixbufLoader.new_with_type("png")
            loader.write(png_buf.getvalue())
            loader.close()
            pixbuf = loader.get_pixbuf()
            if pixbuf is None:
                raise RuntimeError("Failed to decode generated QR image")

            # Keep a reference so the paintable is not GC'd while shown.
            self._qr_texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            self._qr_picture.set_paintable(self._qr_texture)
            self._qr_hint.set_label("Scan with your phone camera to join the hotspot.")
            return True
        except Exception as exc:
            self._show_banner(f"Could not generate QR: {str(exc)[:120]}", error=True)
            self._qr_hint.set_label("Failed to render QR code.")
            return False

    def _on_show_wifi_qr_clicked(self, _btn):
        payload = self._build_wifi_share_string()
        if not payload:
            self._show_banner("No SSID available to share.", error=True)
            self._qr_hint.set_label("No SSID found. Start hotspot or enter values first.")
            return

        if self._render_wifi_qr(payload):
            self._show_banner("QR code generated.")

    def _on_copy_wifi_uri_clicked(self, _btn):
        payload = self._build_wifi_share_string()
        if not payload:
            self._show_banner("No SSID available to share.", error=True)
            return

        display = Gdk.Display.get_default()
        if display is None:
            self._show_banner("Could not access display clipboard.", error=True)
            return

        clipboard = display.get_clipboard()
        clipboard.set(payload)
        self._show_banner("Share string copied. Paste into any Wi-Fi QR generator.")

    def _refresh_status(self):
        """Read config.json directly (0o644 — no root needed) and update UI."""
        cfg = read_config()
        ap_iface = cfg.get("ap_interface") or ""
        
        if ap_iface and not os.path.exists(f"/sys/class/net/{ap_iface}"):
            ap_iface = ""

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

        # Refresh client list asynchronously so status polling doesn't block UI.
        threading.Thread(target=self._bg_refresh_clients, daemon=True).start()

    def _bg_refresh_clients(self):
        rc, stdout, stderr = run_apsta("status", "--json")
        if rc != 0:
            message = first_error_line(strip_ansi(stderr or stdout or "Failed to load clients."))
            GLib.idle_add(self._clients_buf.set_text, f"Could not refresh clients:\n{message}")
            return

        try:
            payload = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            GLib.idle_add(self._clients_buf.set_text, "Could not parse client status output.")
            return

        method = payload.get("method")
        clients = payload.get("clients") or []
        if method != "hostapd":
            GLib.idle_add(self._clients_buf.set_text, "Client management is available in hostapd mode.")
            return

        if not clients:
            GLib.idle_add(self._clients_buf.set_text, "No clients connected.")
            return

        lines = ["HOSTNAME             MAC                 IP"]
        lines.append("------------------------------------------------")
        for client in clients:
            host = client.get("hostname") or "(no hostname)"
            mac = client.get("mac") or "-"
            ip = client.get("ip") or "-"
            lines.append(f"{host[:20]:<20} {mac:<18} {ip}")
        GLib.idle_add(self._clients_buf.set_text, "\n".join(lines))

    def _on_disconnect_client_clicked(self, _btn):
        identifier = self._disconnect_entry.get_text().strip()
        if not identifier:
            self._show_banner("Enter a client MAC, IP, or hostname.", error=True)
            return
        self._disconnect_btn.set_sensitive(False)
        threading.Thread(target=self._bg_disconnect_client, args=(identifier,), daemon=True).start()

    def _bg_disconnect_client(self, identifier: str):
        rc, stdout, stderr = run_apsta_root_script(f'"{APSTA}" status --disconnect "$1"', identifier)
        if rc == 0:
            GLib.idle_add(self._on_disconnect_done, True, "Client disconnected.")
            return

        raw = strip_ansi(stderr or stdout or "Unknown error").strip()
        msg = first_error_line(raw)
        GLib.idle_add(self._on_disconnect_done, False, msg)

    def _on_disconnect_done(self, success: bool, message: str):
        self._disconnect_btn.set_sensitive(True)
        self._show_banner(message, error=not success)
        if success:
            self._disconnect_entry.set_text("")
        threading.Thread(target=self._bg_refresh_clients, daemon=True).start()

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
        done = threading.Event()
        def _run():
            try:
                self._refresh_status()
            finally:
                done.set()
            return False
        GLib.idle_add(_run)
        done.wait(timeout=30)
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
            f'"{APSTA}" config --set ssid="$1" && '
            f'"{APSTA}" config --set password="$2" && '
            f'"{APSTA}" start {force_flag}'.strip()
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
        rc, stdout, stderr = run_apsta_root_script(f'"{APSTA}" stop')
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

        # FIX 1: Quote APSTA path throughout all shell scripts
        cmds = [f'"{APSTA}" config --set ssid="$1"']
        args = [ssid]

        cmds.append(f'"{APSTA}" config --set password="$2"')
        args.append(pwd)

        if iface:
            cmds.append(f'"{APSTA}" config --set interface="$3"')
            args.append(iface)
        else:
            cmds.append(f'"{APSTA}" config --set interface=none')

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
        rc, stdout, stderr = run_apsta_root_script(f'"{APSTA}" enable')
        msg = "Auto-start enabled." if rc == 0 else pkexec_error_message(rc, stderr, stdout)
        GLib.idle_add(self._show_banner, msg, rc != 0)

    def _on_disable_clicked(self, _btn):
        threading.Thread(target=self._bg_disable, daemon=True).start()

    def _bg_disable(self):
        # FIX 1: Quote APSTA path
        rc, stdout, stderr = run_apsta_root_script(f'"{APSTA}" disable')
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
