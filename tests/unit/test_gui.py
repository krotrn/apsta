"""GUI logic that doesn't need GTK, plus a portability check of the GTK code."""

import json
import re
import subprocess
import unittest
import unittest.mock as mock
from pathlib import Path

from apsta_gui import backend, helpers

GUI_DIR = Path(__file__).resolve().parents[2] / "apsta_gui"


def completed(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class HelperTests(unittest.TestCase):
    def test_wifi_share_string_escapes(self):
        self.assertEqual(helpers.wifi_share_string("My;Net", 'pa:ss,"w'), r"WIFI:T:WPA;S:My\;Net;P:pa\:ss\,\"w;;")
        self.assertEqual(helpers.wifi_share_string("", "x"), "")
        self.assertEqual(helpers.wifi_share_string("N", "p", hidden=True), "WIFI:T:WPA;S:N;P:p;H:true;;")

    def test_rates_and_client_text(self):
        self.assertEqual(helpers.format_rate(8000), "8 Mbit/s")
        self.assertEqual(helpers.format_rate(500), "500 kbit/s")
        self.assertEqual(helpers.format_rate(None), "")
        client = {"hostname": "", "mac": "aa", "ip": "", "limit_kbps": 1500}
        self.assertEqual(helpers.client_title(client), "Unknown device")
        self.assertEqual(helpers.client_subtitle(client), "no address yet · aa · limited to 1.5 Mbit/s")

    def test_hero_text(self):
        self.assertEqual(helpers.hero_text({})[0], "Status unavailable")
        self.assertEqual(helpers.hero_text({"hotspot": None, "config": {}})[0], "Hotspot is off")
        title, subtitle, _ = helpers.hero_text({"hotspot": {"ssid": "S"}, "clients": [{}]})
        self.assertEqual((title, subtitle), ("Hotspot is on", "S · 1 device connected"))

    def test_uplink(self):
        data = {
            "hotspot": {"base_interface": "wlo1"},
            "interfaces": [{"name": "wlo1", "connected_ssid": "Home"}, {"name": "x", "connected_ssid": "Other"}],
        }
        self.assertEqual(helpers.uplink(data), "Home")
        self.assertIsNone(helpers.uplink({}))

    def test_capability_rows(self):
        self.assertEqual(helpers.capability_rows({}), [])
        rows = helpers.capability_rows(
            {
                "capability": {"supports_ap": True, "ap_sta": True, "same_channel_required": True},
                "methods": {"hostapd": "needs hostapd", "nmcli": "ready"},
            }
        )
        self.assertEqual([r[2] for r in rows], [True, True, False, False, True])
        self.assertEqual(rows[3][1], "Needs hostapd")
        p2p = helpers.capability_rows(
            {
                "capability": {"supports_ap": True, "ap_sta": True, "same_channel_required": True},
                "methods": {"p2p": "ready"},
            }
        )
        self.assertEqual(
            p2p[2][1:], ("Hotspot may use a different channel than your Wi-Fi (as a Wi-Fi Direct group)", True)
        )
        self.assertEqual(p2p[3][0], "Wi-Fi Direct (own channel)")
        five = helpers.capability_rows({"capability": {"supports_ap": True, "ap_frequencies": [2437, 5745]}})
        self.assertEqual(five[-1][0], "Hotspot on 5 GHz")
        self.assertTrue(five[-1][2])
        self.assertEqual(helpers.band_label("a"), "5 GHz")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.b = backend.ApstaBackend("/usr/bin/apsta")

    def test_privileged_runs_apsta_directly(self):
        with mock.patch.object(backend.subprocess, "run", return_value=completed()) as run:
            self.assertTrue(self.b.start(allow_disconnect=True).ok)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["pkexec", "/usr/bin/apsta", "start"])
        self.assertIn("--allow-disconnect", argv)

    def test_password_goes_over_stdin(self):
        with mock.patch.object(backend.subprocess, "run", return_value=completed()) as run:
            self.b.save_config("Cafe", "secret123", "a", "")
        argv = run.call_args[0][0]
        self.assertNotIn("secret123", " ".join(argv))
        self.assertIn("--password-stdin", argv)
        self.assertIn("band=a", argv)
        self.assertIn("interface=auto", argv)
        self.assertIn("hidden=no", argv)
        self.assertEqual(run.call_args.kwargs["input"], "secret123\n")
        with mock.patch.object(backend.subprocess, "run", return_value=completed()) as run:
            self.b.save_config("Cafe", "", "bg", "wlan1", hidden=True)
        self.assertIsNone(run.call_args.kwargs["input"])
        self.assertIn("hidden=yes", run.call_args[0][0])

    def test_pkexec_failures(self):
        cases = [
            (completed(126), "Authentication cancelled."),
            (completed(127, err="Error executing command as another user: Not authorized"), "Not authorized."),
            (completed(127, err="==== AUTHENTICATION FAILED ===\nNo authentication agent found."), "No polkit"),
            (completed(1, err="  ✘  Could not start the hotspot.\n     hint"), "Could not start the hotspot."),
            (completed(3, out="", err=""), "Unknown error"),
        ]
        for proc, message in cases:
            with mock.patch.object(backend.subprocess, "run", return_value=proc):
                self.assertTrue(self.b.stop().message.startswith(message), (proc, self.b.stop().message))
        with mock.patch.object(backend.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIn("pkexec", self.b.stop().message)
        with mock.patch.object(backend.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            self.assertIn("timed out", self.b.stop().message)

    def test_secrets(self):
        payload = {"settings": {"ssid": "S"}, "password": "p4ssword"}
        with mock.patch.object(backend.subprocess, "run", return_value=completed(out=json.dumps(payload))) as run:
            result = self.b.secrets()
        self.assertEqual(result.data["password"], "p4ssword")
        self.assertEqual(run.call_args[0][0][2:], ["config", "--json", "--show-password"])
        with mock.patch.object(backend.subprocess, "run", return_value=completed(out="not json")):
            self.assertFalse(self.b.secrets().ok)

    def test_unprivileged_reads(self):
        with mock.patch.object(backend.subprocess, "run", return_value=completed(out=json.dumps({"active": True}))):
            self.assertEqual(self.b.status(), {"active": True})
            self.assertEqual(self.b.detect(), {"active": True})
        with mock.patch.object(backend.subprocess, "run", return_value=completed(1)):
            self.assertEqual(self.b.status(), {})
        with mock.patch.object(backend.subprocess, "run", return_value=completed(out="\x1b[1mhi\x1b[0m")):
            self.assertEqual(self.b.text("detect"), "hi")
        with mock.patch.object(backend.subprocess, "run", side_effect=OSError("nope")):
            self.assertEqual(self.b.text("detect"), "nope")
            self.assertEqual(self.b.status(), {})

    def test_command_shapes(self):
        with mock.patch.object(backend.subprocess, "run", return_value=completed()) as run:
            for call, tail in (
                (lambda: self.b.use_profile("travel"), ["profile", "use", "travel"]),
                (lambda: self.b.create_profile("t2"), ["profile", "create", "t2"]),
                (lambda: self.b.disconnect("phone", block=True), ["clients", "disconnect", "phone", "--block"]),
                (lambda: self.b.unblock("aa"), ["clients", "unblock", "aa"]),
                (lambda: self.b.limit("phone", 100), ["clients", "limit", "phone", "100"]),
                (lambda: self.b.unlimit("phone"), ["clients", "unlimit", "phone"]),
                (lambda: self.b.set_autostart(True), ["enable"]),
                (lambda: self.b.set_autostart(False), ["disable"]),
                (lambda: self.b.stop(), ["stop"]),
            ):
                call()
                self.assertEqual(run.call_args[0][0][2:], tail)
        self.assertFalse(backend.ApstaBackend("/nonexistent/apsta").available())


# libadwaita 1.0 / GTK 4.6 is the floor (Ubuntu & Pop!_OS 22.04, Linux Mint 21).
BASELINE_ADW = {
    "ActionRow",
    "Application",
    "ApplicationWindow",
    "Clamp",
    "ComboRow",
    "ExpanderRow",
    "HeaderBar",
    "PreferencesGroup",
    "PreferencesPage",
    "StatusPage",
    "Toast",
    "ToastOverlay",
    "ViewStack",
    "ViewSwitcher",
    "ViewSwitcherPolicy",
    "Window",
    "get_major_version",
    "get_minor_version",
}
NEWER_METHODS = (
    "add_titled_with_icon",  # Adw 1.2
    "set_header_suffix",  # Adw 1.1
    "set_subtitle_selectable",  # Adw 1.3
    "AlertDialog",
    "MessageDialog",
    "SwitchRow",
    "SpinRow",
    "ToolbarView",
    "Banner",
    "Breakpoint",
    "NavigationView",
    "OverlaySplitView",
    "Spinner",  # Adw 1.2+ widgets
)


class PortabilityTests(unittest.TestCase):
    def sources(self):
        return {p: p.read_text() for p in GUI_DIR.rglob("*.py") if p.name != "compat.py"}

    def test_only_baseline_libadwaita_widgets(self):
        for path, text in self.sources().items():
            used = set(re.findall(r"\bAdw\.(\w+)", text))
            self.assertLessEqual(used, BASELINE_ADW, f"{path.name} uses {used - BASELINE_ADW}; go through compat.py")

    def test_no_newer_apis(self):
        for path, text in self.sources().items():
            for name in NEWER_METHODS:
                needle = f"Adw.{name}" if name[0].isupper() else f".{name}("
                self.assertFalse(needle in text, f"{path.name} uses {needle}; go through compat.py")

    def test_desktop_file_matches_app_id(self):
        desktop = (GUI_DIR / "data" / f"{helpers.APP_ID}.desktop").read_text()
        self.assertIn(f"StartupWMClass={helpers.APP_ID}", desktop)


if __name__ == "__main__":
    unittest.main()
