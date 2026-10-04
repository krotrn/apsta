"""End-to-end: the real CLI against the simulated network stack."""

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from apsta_cli.core import paths
from tests.integration import fakeworld
from tests.integration.base import FakeWorldTestCase

PHONE = "aa:bb:cc:dd:ee:ff"


def alive(pidfile: Path) -> bool:
    try:
        os.kill(int(pidfile.read_text()), 0)
        return True
    except (OSError, ValueError):
        return False


class HostapdLifecycleTests(FakeWorldTestCase):
    def test_full_lifecycle(self):
        code, out, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        self.assertIn("is live on", out)
        self.assertIn("Still connected to 'Home'", out)

        st = self.state()
        self.assertEqual((st.method, st.ap_interface, st.channel), ("hostapd", "wlo1_ap", 6))
        self.assertEqual(st.subnet, "192.168.42.0/24")
        world = self.world
        self.assertEqual(world["ifaces"]["wlo1_ap"]["type"], "AP")
        self.assertIn("country_code=IN", world["hostapd_conf"])
        self.assertIn("channel=6", world["hostapd_conf"])
        self.assertEqual(len(world["iptables"]), 6)
        self.assertEqual(paths.IP_FORWARD.read_text().strip(), "1")
        self.assertTrue(alive(paths.HOSTAPD_PID) and alive(paths.DNSMASQ_PID))

        # A random password was generated and stored root-only.
        self.assertEqual(stat.S_IMODE(paths.SECRETS_PATH.stat().st_mode), 0o600)
        self.assertNotIn("changeme", paths.SECRETS_PATH.read_text())
        self.assertFalse(any("password" in " ".join(call) for call in self.world["calls"]))

        status = self.apsta_json("status", "--json")
        self.assertTrue(status["active"])
        self.assertEqual(status["hotspot"]["method"], "hostapd")
        self.assertEqual(self.apsta("status", "--check")[0], 0)

        code, out, _ = self.apsta("status")
        self.assertIn("active on wlo1_ap", out)

        code, out, err = self.apsta("stop")
        self.assertEqual(code, 0, err)
        world = self.world
        self.assertNotIn("wlo1_ap", world["ifaces"])
        self.assertEqual(world["iptables"], [])
        self.assertEqual(paths.IP_FORWARD.read_text().strip(), "0")
        self.assertFalse(alive(paths.HOSTAPD_PID) if paths.HOSTAPD_PID.exists() else False)
        self.assertIsNone(self.state())
        self.assertEqual(self.apsta("status", "--check")[0], 3)
        self.assertIn("No hotspot is running", self.apsta("stop")[1])

    def test_second_start_is_refused(self):
        self.assertEqual(self.apsta("start")[0], 0)
        code, _, err = self.apsta("start")
        self.assertEqual(code, 3)
        self.assertIn("already running", err)
        self.apsta("stop")

    def test_clients(self):
        self.apsta("start")
        self.update_world(stations=[{"mac": PHONE}])
        paths.DNSMASQ_LEASES.write_text(f"0 {PHONE} 192.168.42.50 phone *\n")

        listed = self.apsta_json("clients", "--json")
        self.assertEqual(listed[0]["ip"], "192.168.42.50")

        self.assertEqual(self.apsta("clients", "limit", "phone", "500")[0], 0)
        self.assertEqual(self.apsta_json("clients", "--json")[0]["limit_kbps"], 500)
        self.assertEqual(self.state().client_limits[PHONE]["kbps"], 500)
        self.assertEqual(self.apsta("clients", "unlimit", "phone")[0], 0)

        code, out, _ = self.apsta("clients", "disconnect", "192.168.42.50", "--block")
        self.assertEqual(code, 0)
        self.assertEqual(self.world["denied"], [PHONE])
        self.assertEqual(self.state().blocked, [PHONE])
        self.assertEqual(self.apsta("clients", "unblock", PHONE)[0], 0)
        self.assertIn("No clients connected", self.apsta("clients")[1])
        self.apsta("stop")

    def test_clients_when_not_running(self):
        self.assertIn("not running", self.apsta("clients")[1])
        self.assertEqual(self.apsta("clients", "disconnect", PHONE)[0], 1)

    def test_stale_state_is_cleaned_on_start(self):
        self.apsta("start")
        # Simulate a crash: daemons and interface vanish, state file remains.
        self._kill_daemons()
        data = self.world
        fakeworld.SYSFS = self.root / "sys" / "class" / "net"
        del data["ifaces"]["wlo1_ap"]
        (self.root / "world.json").write_text(__import__("json").dumps(data))
        for child in (paths.SYSFS_NET / "wlo1_ap").glob("*"):
            child.unlink()
        (paths.SYSFS_NET / "wlo1_ap").rmdir()

        self.assertTrue(self.apsta_json("status", "--json")["stale"])
        code, _, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        self.assertIn("stopped unexpectedly", err)
        self.assertEqual(len(self.world["iptables"]), 6)  # old rules removed, new ones added
        self.apsta("stop")


class FallbackTests(FakeWorldTestCase):
    world_extra = {"hostapd_fails": True}

    def test_hostapd_failure_falls_back_to_networkmanager(self):
        code, out, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        self.assertIn("nl80211: Could not configure driver mode", err)
        st = self.state()
        self.assertEqual(st.method, "nmcli")
        world = self.world
        self.assertEqual(world["iptables"], [])  # hostapd attempt fully rolled back
        self.assertTrue(world["nm"]["apsta-hotspot"]["active"])
        keyfile = paths.NM_RUNTIME_KEYFILE_DIR / "apsta-hotspot.nmconnection"
        self.assertEqual(stat.S_IMODE(keyfile.stat().st_mode), 0o600)

        self.assertEqual(self.apsta("stop")[0], 0)
        self.assertNotIn("apsta-hotspot", self.world["nm"])
        self.assertNotIn("wlo1_ap", self.world["ifaces"])

    def test_method_option(self):
        code, _, err = self.apsta("start", "--method", "hostapd")
        self.assertEqual(code, 1)
        self.assertIn("Could not start", err)


class SingleRadioTests(FakeWorldTestCase):
    phy = "iw/no_combinations.txt"

    def test_requires_allow_disconnect(self):
        code, _, err = self.apsta("start")
        self.assertEqual(code, 1)
        self.assertIn("--allow-disconnect", err)

        code, out, err = self.apsta("start", "--allow-disconnect")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.state().method, "nmcli-single")
        self.assertIn("will drop", err)
        self.assertEqual(self.apsta("stop")[0], 0)
        self.assertEqual(self.world["ifaces"]["wlo1"]["type"], "managed")


class DfsTests(FakeWorldTestCase):
    link = {"ssid": "Office", "freq": 5500}

    def test_dfs_channel_explained(self):
        code, _, err = self.apsta("start")
        self.assertEqual(code, 1)
        self.assertIn("DFS channel 100", err)
        self.assertNotIn("wlo1_ap", self.world["ifaces"])


class OfflineTests(FakeWorldTestCase):
    link = None

    def test_picks_least_congested_channel(self):
        self.assertEqual(self.apsta("start")[0], 0)
        self.assertEqual(self.state().channel, 6)  # fake scan: ch1=80, ch6=30, ch11=75
        self.apsta("stop")


class ConfigCommandTests(FakeWorldTestCase):
    def test_set_show_and_password_stdin(self):
        self.assertEqual(self.apsta("config", "--set", "ssid=Cafe", "--set", "band=a")[0], 0)
        self.assertEqual(self.apsta("config", "--password-stdin", stdin="correct horse\n")[0], 0)
        code, out, _ = self.apsta("config", "--show-password")
        self.assertIn("correct horse", out)
        self.assertIn("Cafe", self.apsta("config")[1])
        data = self.apsta_json("config", "--json")
        self.assertEqual(data["settings"]["band"], "a")
        self.assertNotIn("password", data)
        self.assertEqual(self.apsta_json("config", "--json", "--show-password")["password"], "correct horse")
        self.assertNotIn("correct horse", self.apsta("config")[1])

    def test_validation_errors(self):
        self.assertEqual(self.apsta("config", "--set", "password=short")[0], 2)
        self.assertEqual(self.apsta("config", "--set", "nonsense=1")[0], 2)
        self.assertEqual(self.apsta("config", "--set", "noequals")[0], 2)
        _, _, err = self.apsta("config", "--set", "password=longenough1")
        self.assertIn("shell history", err)

    def test_generate_password(self):
        code, out, _ = self.apsta("config", "--generate-password")
        self.assertEqual(code, 0)
        self.assertIn("New password:", out)

    def test_start_with_overrides(self):
        code, _, err = self.apsta("start", "--ssid", "FromGui", "--password-stdin", stdin="guipassword\n")
        self.assertEqual(code, 0, err)
        self.assertIn("ssid2=" + b"FromGui".hex(), self.world["hostapd_conf"])
        self.assertIn("wpa_passphrase=guipassword", self.world["hostapd_conf"])
        self.assertIn("Restart the hotspot", self.apsta("config", "--set", "ssid=Later")[1])
        self.apsta("stop")

    def test_profiles(self):
        self.assertEqual(self.apsta("profile", "create", "travel")[0], 0)
        self.assertEqual(self.apsta("profile", "use", "travel")[0], 0)
        self.assertIn("* travel", self.apsta("profile", "list")[1].replace("  ", " "))
        self.assertIn("travel", self.apsta("profile", "show")[1])
        self.assertEqual(self.apsta("profile", "delete", "travel")[0], 2)
        self.assertEqual(self.apsta("profile", "show", "nope")[0], 2)
        _, _, err = self.apsta("status", "--use-profile", "default")
        self.assertIn("deprecated", err)
        self.assertEqual(self.apsta("profile", "delete", "travel")[0], 0)


class DetectTests(FakeWorldTestCase):
    def test_detect_json_and_text(self):
        data = self.apsta_json("detect", "--json")
        self.assertEqual(data["verdict"]["mode"], "ap+sta")
        self.assertTrue(data["capability"]["same_channel_required"])
        self.assertEqual(data["methods"], {"hostapd": "ready", "nmcli": "ready"})
        status = self.apsta_json("status", "--json")
        self.assertEqual(set(data["interfaces"][0]), set(status["interfaces"][0]))  # same shape everywhere
        self.assertEqual(data["interfaces"][0]["type"], "managed")
        code, out, _ = self.apsta("detect")
        self.assertIn("same channel", out)


class SubprocessSmokeTests(unittest.TestCase):
    """The installed entry point works as a separate process."""

    def test_version_and_completion(self):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        out = subprocess.run([sys.executable, "-m", "apsta_cli", "--version"], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0)
        self.assertIn("apsta", out.stdout)
        for shell_name in ("bash", "zsh", "fish"):
            out = subprocess.run(
                [sys.executable, "-m", "apsta_cli", "completion", shell_name], capture_output=True, text=True, env=env
            )
            self.assertEqual(out.returncode, 0)
            self.assertIn("clients", out.stdout)

    def test_status_check_without_hotspot(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]), "APSTA_PATH": "/nonexistent"}
        out = subprocess.run([sys.executable, "-m", "apsta_cli", "status", "--check"], capture_output=True, env=env)
        self.assertEqual(out.returncode, 3)


if __name__ == "__main__":
    unittest.main()


class StatusOutputTests(FakeWorldTestCase):
    def test_text_output_with_devices_and_json_start(self):
        code, out, err = self.apsta("start", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["method"], "hostapd")
        self.update_world(stations=[{"mac": PHONE}])
        paths.DNSMASQ_LEASES.write_text(f"0 {PHONE} 192.168.42.50 phone *\n")

        _, out, _ = self.apsta("status")
        self.assertIn("Clients connected: 1", out)
        _, out, _ = self.apsta("status", "--clients")
        self.assertIn("phone", out)
        self.assertIn("192.168.42.50", out)
        _, out, _ = self.apsta("clients")
        self.assertIn(PHONE, out)
        self.apsta("stop")

    def test_stale_hotspot_is_reported(self):
        self.apsta("start")
        self._kill_daemons()
        _, out, err = self.apsta("status")
        self.assertIn("not running", out)
        self.assertIn("stopped unexpectedly", err)


class DeprecatedFlagTests(FakeWorldTestCase):
    def setUp(self):
        super().setUp()
        self.apsta("start")
        self.update_world(stations=[{"mac": PHONE}])
        self.addCleanup(self.apsta, "stop")

    def test_status_disconnect_and_limit_still_work(self):
        code, _, err = self.apsta("status", "--limit-client", PHONE, "--limit-kbps", "300")
        self.assertEqual(code, 0, err)
        self.assertIn("deprecated", err)
        self.assertEqual(self.state().client_limits[PHONE]["kbps"], 300)
        code, _, err = self.apsta("status", "--disconnect", PHONE)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.world["stations"], [])

    def test_limit_kbps_alone_is_a_usage_error_not_a_crash(self):
        code, _, err = self.apsta("status", "--limit-kbps", "500")
        self.assertEqual(code, 2)
        self.assertIn("must be used together", err)
