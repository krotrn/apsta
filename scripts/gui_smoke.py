#!/usr/bin/env python3
"""Headless GUI smoke test and screenshot tool.

Builds apsta-gtk in several states against a fake backend on a Broadway
display (no desktop session needed), exercises the main interactions, and
saves a PNG of every view. Exits non-zero on any exception; run with
G_DEBUG=fatal-criticals to also fail on GTK criticals (CI does).

    python3 scripts/gui_smoke.py [OUTPUT_DIR]        # default: ./gui-screenshots

Needs PyGObject, GTK 4, libadwaita and a headless display: gtk4-broadwayd
(Debian 12+/Ubuntu 24.04+: libgtk-4-bin, Arch: gtk4, Fedora: gtk4) or, where
GTK was built without Broadway (Ubuntu 22.04), Xvfb.

Screenshots need PyGObject >= 3.44; on older bindings the test still builds
and exercises every view but skips the PNGs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DISPLAY = os.environ.get("BROADWAY_DISPLAY", ":42")


def start_display():
    """Start a headless display server and point GDK at it."""
    broadwayd = shutil.which("gtk4-broadwayd") or shutil.which("broadwayd")
    if broadwayd:
        proc = subprocess.Popen([broadwayd, DISPLAY], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.environ.update(GDK_BACKEND="broadway", BROADWAY_DISPLAY=DISPLAY)
    elif shutil.which("Xvfb"):
        proc = subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ.update(GDK_BACKEND="x11", DISPLAY=":99", GSK_RENDERER="cairo")
    else:
        sys.exit("Need gtk4-broadwayd or Xvfb for a headless display.")
    time.sleep(1.5)
    return proc


DISPLAY_SERVER = start_display()
SCREENSHOTS = {"enabled": True}

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from apsta_gui import compat  # noqa: E402

# PyGObject < 3.44 can't return GskRenderNode from Gtk.Snapshot.to_node()
# (3.42.1 even emits a GLib critical), so only the screenshots are skipped there.
if gi.version_info < (3, 44, 0):
    print(f"note: PyGObject {gi.__version__} is too old for screenshots; building views only")
    SCREENSHOTS["enabled"] = False
from apsta_gui.backend import Result  # noqa: E402
from apsta_gui.window import ApstaWindow, MissingApstaWindow  # noqa: E402

HOTSPOT = {
    "method": "hostapd",
    "base_interface": "wlo1",
    "ap_interface": "wlo1_ap",
    "ssid": "Café <Laptop> & co",
    "channel": 6,
    "band": "bg",
    "subnet": "192.168.42.0/24",
    "gateway": "192.168.42.1",
    "blocked": ["de:ad:be:ef:00:01"],
}
CLIENTS = [
    {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.42.17", "hostname": "pixel-8", "limit_kbps": 8000, "blocked": False},
    {"mac": "11:22:33:44:55:66", "ip": "", "hostname": "", "limit_kbps": None, "blocked": False},
]
BASE = {
    "stale": False,
    "interfaces": [{"name": "wlo1", "mac": "x", "type": "managed", "state": "UP", "connected_ssid": "Home"}],
    "autostart": {"init": "systemd", "enabled": False, "running": False},
    "config": {
        "active_profile": "default",
        "profiles": ["default", "travel"],
        "ssid": HOTSPOT["ssid"],
        "band": "bg",
        "channel": "6",
        "interface": None,
    },
}
DETECT_OK = {
    "capability": {
        "interface": "wlo1",
        "supports_ap": True,
        "ap_sta": True,
        "same_channel_required": True,
        "driver": "iwlwifi",
        "chipset": "Intel Wi-Fi 6 AX201",
    },
    "methods": {"hostapd": "ready", "nmcli": "ready"},
    "verdict": {"mode": "ap+sta"},
}
DETECT_SINGLE = {
    "capability": {"interface": "wlo1", "supports_ap": True, "ap_sta": False, "driver": "rtl88x2bu"},
    "methods": {"hostapd": "needs hostapd, dnsmasq", "nmcli": "ready"},
    "verdict": {"mode": "single"},
}

SCENARIOS = {
    "on": ({**BASE, "active": True, "hotspot": HOTSPOT, "clients": CLIENTS}, DETECT_OK),
    "on-empty": ({**BASE, "active": True, "hotspot": {**HOTSPOT, "blocked": []}, "clients": []}, DETECT_OK),
    "off": ({**BASE, "active": False, "hotspot": None, "clients": []}, DETECT_OK),
    "off-single-radio": ({**BASE, "active": False, "hotspot": None, "clients": [], "stale": True}, DETECT_SINGLE),
    "unavailable": ({}, {}),
}


class FakeBackend:
    apsta = "/usr/bin/apsta"

    def __init__(self, status, detect):
        self._status, self._detect = status, detect
        self.calls = []

    def available(self):
        return True

    def status(self):
        return self._status

    def detect(self):
        return self._detect

    def text(self, *args):
        return "No USB WiFi adapters detected."

    def secrets(self):
        return Result(True, "", {"settings": {"ssid": HOTSPOT["ssid"]}, "password": "kd7Ws3qPzT9mXbR2"})

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return Result(True, f"{name} done")

        return call


def pump(seconds: float) -> None:
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)


def x11_screenshot(widget: Gtk.Widget, path: Path) -> bool:
    """Grab the window from Xvfb with ImageMagick (for PyGObject < 3.44)."""
    if os.environ.get("GDK_BACKEND") != "x11" or not shutil.which("import"):
        return False
    pump(0.6)
    geometry = f"{widget.get_width()}x{widget.get_height()}+0+0"
    subprocess.run(["import", "-window", "root", "-crop", geometry, str(path)], check=False)
    return True


def screenshot(widget: Gtk.Widget, path: Path) -> None:
    if not SCREENSHOTS["enabled"]:
        if not x11_screenshot(widget, path):
            pump(0.3)  # still let the view lay out and draw
        return
    for _ in range(100):
        widget.queue_draw()
        pump(0.05)
        snapshot = Gtk.Snapshot.new()
        Gtk.WidgetPaintable.new(widget).snapshot(snapshot, widget.get_width(), widget.get_height())
        try:
            node = snapshot.to_node()
        except TypeError:  # PyGObject < 3.44 can't return GskRenderNode
            print("note: PyGObject too old for screenshots; building views only")
            SCREENSHOTS["enabled"] = False
            pump(0.3)
            return
        if node is not None:
            widget.get_native().get_renderer().render_texture(node, None).save_to_png(str(path))
            return
    raise RuntimeError(f"nothing rendered for {path.name}")


def other_windows(main):
    return [w for w in Gtk.Window.list_toplevels() if w is not main and w.get_visible()]


def run(app: Adw.Application, out: Path) -> None:
    compat.register_bundled_icons("com.github.apsta.Gtk")
    for name, (status, detect) in SCENARIOS.items():
        print(f"scenario {name}", flush=True)
        backend = FakeBackend(status, detect)
        win = ApstaWindow(app, backend)
        win.present()
        pump(0.8)
        for page in ("hotspot", "clients", "settings"):
            print(f"  page {page}", flush=True)
            win.show_page(page)
            screenshot(win, out / f"{name}-{page}.png")
        if status.get("hotspot"):
            win.show_page("hotspot")
            print("  share dialog", flush=True)
            win.share()
            pump(0.8)
            dialogs = other_windows(win)
            assert dialogs, "share dialog did not open"
            screenshot(dialogs[0], out / f"{name}-share.png")
            for dialog in dialogs:
                dialog.close()
            if win.clients_page.rows:
                print("  client menu", flush=True)
                row = next(iter(win.clients_page.rows.values()))
                row.popover.popup()
                pump(0.3)
                row.apply_btn.emit("clicked")
                pump(0.5)
                assert "limit" in backend.calls, backend.calls
        print("  toggle", flush=True)
        win.hotspot_page.toggle.emit("clicked")
        pump(0.5)
        if status:
            assert {"stop", "start"} & set(backend.calls), backend.calls
        else:
            assert win.hotspot_page.toggle.get_label() == "Try Again"
        win.close()
        pump(0.2)

    print("about window", flush=True)
    host = ApstaWindow(app, FakeBackend(*SCENARIOS["off"]))
    host.present()
    pump(0.5)
    compat.show_about(host, "0.0.0")
    pump(0.8)
    abouts = other_windows(host)
    assert abouts, "about window did not open"
    screenshot(abouts[0], out / "about.png")
    for window in abouts + [host]:
        window.close()

    missing = MissingApstaWindow(app, "/usr/bin/apsta")
    missing.present()
    screenshot(missing, out / "missing-apsta.png")
    missing.close()


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "gui-screenshots")
    out.mkdir(parents=True, exist_ok=True)
    app = Adw.Application(application_id="com.github.apsta.Smoke")
    failure = []

    def activate(app):
        app.hold()
        try:
            run(app, out)
        except Exception as exc:  # noqa: BLE001 - report anything as a failure
            import traceback

            traceback.print_exc()
            failure.append(exc)
        finally:
            app.release()
            app.quit()

    app.connect("activate", activate)
    app.run([])
    DISPLAY_SERVER.terminate()
    version = ".".join(map(str, compat.ADW_VERSION))
    gtk = f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}"
    if failure:
        print(f"GUI smoke test FAILED (libadwaita {version}, GTK {gtk})")
        return 1
    print(f"GUI smoke test passed (libadwaita {version}, GTK {gtk}); screenshots in {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
