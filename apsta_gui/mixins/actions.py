"""Event handlers and background work for the GTK window.

Rules followed here:
* anything that may block (subprocesses) runs in a worker thread;
* widgets are touched only on the main loop (``GLib.idle_add``);
* periodic refreshes never overwrite a field the user has edited.
"""

import io
import threading

from gi.repository import Gdk, GdkPixbuf, GLib

from ..helpers import band_label, format_clients, wifi_share_string


class ApstaWindowActionsMixin:
    # ── threading helpers ─────────────────────────────────────────────────────

    def _in_background(self, work, done=None):
        """Run ``work()`` off the main loop, then ``done(result)`` on it."""

        def runner():
            result = work()
            if done is not None:
                GLib.idle_add(done, result)

        threading.Thread(target=runner, daemon=True).start()

    def _privileged(self, work, button=None, refresh=True):
        if button is not None:
            button.set_sensitive(False)

        def done(result):
            if button is not None:
                button.set_sensitive(True)
            self._show_banner(result.message, error=not result.ok)
            if refresh:
                self._request_refresh()
            return False

        self._in_background(work, done)

    # ── status polling ────────────────────────────────────────────────────────

    def _on_poll_tick(self) -> bool:
        self._request_refresh()
        return True  # keep the timer

    def _request_refresh(self):
        if self._refreshing:
            return
        self._refreshing = True
        self._in_background(self._backend.status, self._apply_status)

    def _sync_entry(self, entry, key: str, value: str):
        """Update ``entry`` from config only if the user hasn't edited it since the last sync."""
        last = self._synced.get(key)
        if last is None or entry.get_text() == last:
            entry.set_text(value)
            self._synced[key] = value

    def _apply_status(self, data: dict):
        self._refreshing = False
        hotspot = data.get("hotspot")
        config = data.get("config") or {}
        active = bool(hotspot)

        if active:
            self._status_row.set_subtitle(f"Active ({hotspot['method']})")
            self._status_icon.set_from_icon_name("network-wireless-hotspot-symbolic")
            self._ssid_status_row.set_subtitle(hotspot["ssid"])
            self._iface_row.set_subtitle(hotspot["ap_interface"])
            self._channel_row.set_subtitle(f"ch{hotspot['channel']} ({band_label(hotspot['band'])})")
        else:
            self._status_row.set_subtitle("Unavailable" if not data else "Inactive")
            self._status_icon.set_from_icon_name("network-wireless-symbolic")
            for row in (self._ssid_status_row, self._iface_row, self._channel_row):
                row.set_subtitle("—")

        if config:
            self._sync_entry(self._ssid_entry, "ssid", config.get("ssid") or "")
            self._sync_entry(self._profile_entry, "profile", config.get("active_profile") or "default")
            self._sync_entry(self._cfg_ssid, "cfg_ssid", config.get("ssid") or "")
            self._sync_entry(self._cfg_iface, "cfg_iface", config.get("interface") or "")

        self._start_btn.set_sensitive(not active and not self._busy)
        self._stop_btn.set_sensitive(active and not self._busy)
        self._clients_buf.set_text(
            format_clients(data.get("clients") or []) if active else "Start the hotspot to see clients."
        )
        return False

    # ── start / stop ──────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        self._busy = busy
        label = "Working…" if busy else None
        self._start_btn.set_label(label or "Start Hotspot")
        self._stop_btn.set_label(label or "Stop Hotspot")
        self._start_btn.set_sensitive(not busy)
        self._stop_btn.set_sensitive(not busy)

    def _run_busy(self, work):
        self._set_busy(True)

        def done(result):
            self._set_busy(False)
            self._show_banner(result.message, error=not result.ok)
            if result.ok:
                self._pass_entry.set_text("")
            self._request_refresh()
            return False

        self._in_background(work, done)

    def _on_start_clicked(self, _btn):
        ssid = self._ssid_entry.get_text().strip()
        password = self._pass_entry.get_text()
        if not ssid:
            self._show_banner("SSID cannot be empty.", error=True)
            return
        allow = self._allow_disconnect_switch.get_active()
        self._run_busy(lambda: self._backend.start(ssid, password, allow))

    def _on_stop_clicked(self, _btn):
        self._run_busy(self._backend.stop)

    # ── profiles / config / service ───────────────────────────────────────────

    def _on_apply_profile_clicked(self, btn):
        name = self._profile_entry.get_text().strip()
        if not name:
            self._show_banner("Profile name cannot be empty.", error=True)
            return
        self._synced.pop("ssid", None)  # take the new profile's SSID
        self._privileged(lambda: self._backend.use_profile(name), btn)

    def _on_save_config_clicked(self, btn):
        ssid = self._cfg_ssid.get_text().strip()
        if not ssid:
            self._show_banner("SSID cannot be empty.", error=True)
            return
        password = self._cfg_pass.get_text()
        iface = self._cfg_iface.get_text().strip()

        def work():
            result = self._backend.save_config(ssid, password, iface)
            if result.ok:
                GLib.idle_add(self._cfg_pass.set_text, "")
            return result

        self._privileged(work, btn)

    def _on_enable_clicked(self, btn):
        self._privileged(self._backend.enable_service, btn)

    def _on_disable_clicked(self, btn):
        self._privileged(self._backend.disable_service, btn)

    # ── clients ───────────────────────────────────────────────────────────────

    def _client_id(self):
        ident = self._client_entry.get_text().strip()
        if not ident:
            self._show_banner("Enter a client MAC, IP, or hostname.", error=True)
        return ident

    def _on_disconnect_client_clicked(self, btn):
        ident = self._client_id()
        if ident:
            self._privileged(lambda: self._backend.disconnect(ident), btn)

    def _on_block_client_clicked(self, btn):
        ident = self._client_id()
        if ident:
            self._privileged(lambda: self._backend.disconnect(ident, block=True), btn)

    def _on_limit_client_clicked(self, btn):
        ident = self._client_id()
        kbps = self._limit_kbps_entry.get_text().strip()
        if not ident:
            return
        if not kbps.isdigit() or int(kbps) <= 0:
            self._show_banner("Limit must be a positive number of Kbps.", error=True)
            return
        self._privileged(lambda: self._backend.limit(ident, int(kbps)), btn)

    # ── hardware page ─────────────────────────────────────────────────────────

    def _show_command_output(self, buffer, *args):
        buffer.set_text("Working…")
        self._in_background(lambda: self._backend.text(*args), lambda out: buffer.set_text(out or "(no output)"))

    def _on_detect_clicked(self, _btn):
        self._show_command_output(self._detect_buf, "detect")

    def _on_usb_scan_clicked(self, _btn):
        self._show_command_output(self._usb_buf, "scan-usb")

    def _on_recommend_clicked(self, _btn):
        self._show_command_output(self._rec_buf, "recommend")

    # ── sharing ───────────────────────────────────────────────────────────────

    def _share_payload(self) -> str:
        payload = wifi_share_string(self._ssid_entry.get_text().strip(), self._pass_entry.get_text())
        if not payload:
            self._show_banner("Type the SSID and password to share them.", error=True)
            self._qr_hint.set_label("The saved password is root-only; type it above to share it.")
        return payload

    def _on_show_wifi_qr_clicked(self, _btn):
        payload = self._share_payload()
        if payload and self._render_wifi_qr(payload):
            self._show_banner("QR code generated.")

    def _on_copy_wifi_uri_clicked(self, _btn):
        payload = self._share_payload()
        if not payload:
            return
        display = Gdk.Display.get_default()
        if display is None:
            self._show_banner("Could not access the clipboard.", error=True)
            return
        display.get_clipboard().set(payload)
        self._show_banner("Share string copied.")

    def _render_wifi_qr(self, payload: str) -> bool:
        try:
            import qrcode
        except ImportError:
            self._show_banner("Install the Python qrcode and Pillow packages for QR codes.", error=True)
            return False
        try:
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
            qr.add_data(payload)
            qr.make(fit=True)
            buf = io.BytesIO()
            qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
            loader = GdkPixbuf.PixbufLoader.new_with_type("png")
            loader.write(buf.getvalue())
            loader.close()
            self._qr_texture = Gdk.Texture.new_for_pixbuf(loader.get_pixbuf())  # keep a reference
            self._qr_picture.set_paintable(self._qr_texture)
            self._qr_hint.set_label("Scan with your phone camera to join the hotspot.")
            return True
        except Exception as exc:  # noqa: BLE001 - surface any rendering failure in the UI
            self._show_banner(f"Could not generate QR: {str(exc)[:120]}", error=True)
            return False

    # ── banner ────────────────────────────────────────────────────────────────

    def _show_banner(self, message: str, error: bool = False):
        self._banner.set_title(message)
        (self._banner.add_css_class if error else self._banner.remove_css_class)("error")
        self._banner.set_revealed(True)
        if self._banner_timeout_id is not None:
            GLib.source_remove(self._banner_timeout_id)
        self._banner_timeout_id = GLib.timeout_add(4000, self._hide_banner)
        return False

    def _hide_banner(self) -> bool:
        self._banner.set_revealed(False)
        self._banner_timeout_id = None
        return False
