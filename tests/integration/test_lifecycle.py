"""End-to-end: the real CLI against the simulated network stack."""

import json
import os
import stat
import subprocess
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

from apsta_cli.core import paths
from apsta_cli.core.errors import SetupError
from apsta_cli.net import strategies, wpa
from tests.integration import fakeworld
from tests.integration.base import HOME_24GHZ, FakeWorldTestCase
from tests.support import FakeBus, FakeWpaSupplicant

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
        nm_conf = paths.NM_RUNTIME_CONF_DIR / "90-apsta-unmanaged.conf"
        self.assertIn("unmanaged-devices+=interface-name:wlo1_ap", nm_conf.read_text())
        self.assertLess(
            self.world["calls"].index(["nmcli", "general", "reload", "conf"]),
            self.world["calls"].index(["iw", "dev", "wlo1", "interface", "add", "wlo1_ap", "type", "__ap"]),
        )

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
        self.assertFalse(nm_conf.exists())
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


class HostapdNeverEnablesTests(FakeWorldTestCase):
    """hostapd runs but can't bring the AP up (NetworkManager held the interface on a real
    Intel card). apsta must not report success; it should fall back to NetworkManager."""

    world_extra = {"hostapd_never_enables": True}

    def test_not_reported_live_and_falls_back(self):
        import unittest.mock as mock

        with (
            mock.patch("apsta_cli.net.hostapd.time.sleep"),
            mock.patch("apsta_cli.net.hostapd.time.monotonic", side_effect=[0, 0, 100, 100, 100]),
        ):
            code, out, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        self.assertIn("did not start broadcasting", err)
        self.assertEqual(self.state().method, "nmcli")
        self.assertEqual(self.world["iptables"], [])
        self.assertFalse((paths.NM_RUNTIME_CONF_DIR / "90-apsta-unmanaged.conf").exists())
        self.apsta("stop")

    def test_hostapd_only_fails_cleanly(self):
        import unittest.mock as mock

        with (
            mock.patch("apsta_cli.net.hostapd.time.sleep"),
            mock.patch("apsta_cli.net.hostapd.time.monotonic", side_effect=[0, 0, 100, 100, 100]),
        ):
            code, _, err = self.apsta("start", "--method", "hostapd")
        self.assertEqual(code, 1)
        self.assertNotIn("wlo1_ap", self.world["ifaces"])
        self.assertIsNone(self.state())


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
        self.assertIn("p2p: wpa_supplicant", err)  # no socket, and no D-Bus service either
        self.assertNotIn("wlo1_ap", self.world["ifaces"])


class WifiDirectWorld(FakeWorldTestCase):
    """A fake wpa_supplicant that creates and removes the group's interface in the fake world."""

    link = {"ssid": "NIT-Student", "freq": 5640}

    def setUp(self):
        super().setUp()
        self.refuse = set()
        self.ssid = None
        self.no_group = False  # accept P2P_GROUP_ADD but never create the interface
        self.wpa = FakeWpaSupplicant(paths.WPA_CTRL_DIR / "p2p-dev-wlo1", self.answer).install(self)

    def answer(self, command):
        verb, _, rest = command.partition(" ")
        if verb in self.refuse:
            return "FAIL\n"
        args = rest.split()
        if verb == "SET_NETWORK" and args[1] == "ssid":
            self.ssid = bytes.fromhex(args[2]).decode()
        sysfs = paths.SYSFS_NET / "p2p-wlo1-0"
        if verb == "P2P_GROUP_ADD" and not self.no_group:
            data = self.world
            data["ifaces"]["p2p-wlo1-0"] = {"type": "P2P-GO", "addr": "e4:00:00:00:00:02", "link": None}
            data["ifaces"]["p2p-wlo1-0"]["ssid"] = self.ssid
            (self.root / "world.json").write_text(json.dumps(data))
            sysfs.mkdir(parents=True, exist_ok=True)
            (sysfs / "operstate").write_text("up\n")
        if verb == "P2P_GROUP_REMOVE":
            data = self.world
            data["ifaces"].pop(args[0], None)
            (self.root / "world.json").write_text(json.dumps(data))
            for child in sysfs.glob("*"):
                child.unlink()
            sysfs.rmdir()
        return self.wpa.default(command)


class WifiDirectTests(WifiDirectWorld):
    """Real case: Intel card on a campus network's DFS channel 128; the hotspot gets a channel of its own."""

    def test_hotspot_runs_on_its_own_channel(self):
        code, out, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        self.assertIn("p2p: hostapd and nmcli were skipped (the WiFi connection's channel can't host", out)
        self.assertIn("Channel 6 (2.4 GHz): the least crowded nearby.", out)
        self.assertIn("Your WiFi is connected on DFS channel 128", out)
        self.assertIn("they share its speed", out)
        self.assertTrue(any("p2p: hostapd and nmcli" in n for n in self.state().notes))
        st = self.state()
        self.assertEqual((st.method, st.ap_interface, st.channel, st.band), ("p2p", "p2p-wlo1-0", 6, "bg"))
        self.assertFalse(st.same_channel_required)  # the watcher must not chase the WiFi's channel
        self.assertIn("P2P_GROUP_ADD persistent=0 freq=2437", self.wpa.commands)
        self.assertIn("SET_NETWORK 0 mode 3", self.wpa.commands)
        self.assertEqual(len(self.world["iptables"]), 6)
        password = self.apsta_json("config", "--json", "--show-password")["password"]
        self.assertIn(f'SET_NETWORK 0 psk "{password}"', self.wpa.commands)
        self.assertFalse(any(password in " ".join(call) for call in self.world["calls"]))  # never on a command line
        self.assertTrue(self.apsta_json("status", "--json")["active"])

        code, _, err = self.apsta("stop")
        self.assertEqual(code, 0, err)
        self.assertNotIn("p2p-wlo1-0", self.world["ifaces"])
        self.assertIn("P2P_GROUP_REMOVE p2p-wlo1-0", self.wpa.commands)
        self.assertIn("REMOVE_NETWORK 0", self.wpa.commands)
        self.assertEqual(self.wpa.networks, {})  # wpa_supplicant's own copy of the group is gone too
        self.assertEqual(self.world["iptables"], [])
        self.assertIsNone(self.state())

    def test_refused_group_rolls_back_and_explains(self):
        self.refuse = {"P2P_GROUP_ADD"}
        code, _, err = self.apsta("start")
        self.assertEqual(code, 1)
        self.assertIn("DFS channel 128", err)
        self.assertIn("p2p: wpa_supplicant refused to start the Wi-Fi Direct group", err)
        self.assertIn("REMOVE_NETWORK 0", self.wpa.commands)
        self.assertEqual(self.world["iptables"], [])
        self.assertIsNone(self.state())

    def test_group_that_never_appears_is_rolled_back(self):
        self.no_group = True
        wait = strategies._wait_for_group
        with mock.patch.object(strategies, "_wait_for_group", lambda phy, before: wait(phy, before, timeout=0)):
            code, _, err = self.apsta("start")
        self.assertEqual(code, 1)
        self.assertIn("didn't start the Wi-Fi Direct group", err)
        self.assertEqual(self.wpa.networks, {})  # apsta's network and wpa_supplicant's copy
        self.assertEqual(self.world["iptables"], [])
        self.assertIsNone(self.state())

    def test_failure_after_the_group_started_removes_it(self):
        with mock.patch.object(strategies, "share_connection", side_effect=SetupError("dnsmasq failed")):
            code, _, err = self.apsta("start")
        self.assertEqual(code, 1)
        self.assertIn("P2P_GROUP_REMOVE p2p-wlo1-0", self.wpa.commands)
        self.assertNotIn("p2p-wlo1-0", self.world["ifaces"])
        self.assertEqual(self.wpa.networks, {})
        self.assertIsNone(self.state())

    def test_stop_after_wpa_supplicant_went_away(self):
        self.assertEqual(self.apsta("start")[0], 0)
        self.wpa.close()
        (paths.WPA_CTRL_DIR / "p2p-dev-wlo1").unlink()  # e.g. NetworkManager restarted it
        code, _, err = self.apsta("stop")
        self.assertEqual(code, 0, err)
        self.assertNotIn("p2p-wlo1-0", self.world["ifaces"])  # removed with iw instead
        self.assertEqual(self.world["iptables"], [])
        self.assertIsNone(self.state())

    def test_detect_says_it_works(self):
        data = self.apsta_json("detect", "--json")
        self.assertEqual(data["verdict"]["level"], "ok")
        self.assertEqual(data["methods"]["p2p"], "ready")
        self.assertTrue(data["capability"]["p2p_go_own_channel"])


class NoWifiDirectCardTests(FakeWorldTestCase):
    """A card that can't run Wi-Fi Direct on a second channel, on a DFS network: explain, touch nothing."""

    phy = "iw/intel_no_p2p_channel.txt"
    link = {"ssid": "NIT-Student", "freq": 5640}

    def test_refused_with_the_channel_explanation(self):
        code, _, err = self.apsta("start")
        self.assertEqual(code, 1)
        self.assertIn("DFS channel 128", err)
        self.assertIn("channel 36–48 or 149–165", err)
        self.assertEqual(self.world["iptables"], [])
        self.assertIsNone(self.state())

    def test_forcing_wifi_direct_is_refused_too(self):
        self.apsta("config", "--set", "method=p2p")
        code, _, err = self.apsta("start")
        self.assertEqual(code, 1)
        self.assertIn("p2p: the card can't run a Wi-Fi Direct group on a channel of its own", err)

    def test_detect(self):
        data = self.apsta_json("detect", "--json")
        self.assertFalse(data["capability"]["p2p_go_own_channel"])
        self.assertNotIn("p2p", data["methods"])
        self.assertEqual(data["verdict"]["level"], "warn")
        self.assertIn("channel 128, where this card can't host", data["verdict"]["warnings"][0])


class ChoicesTests(WifiDirectWorld):
    """The user's band, channel and method settings, and the notes that explain what was done."""

    link = HOME_24GHZ

    def test_band_that_cant_be_honoured_is_explained(self):
        self.apsta("config", "--set", "band=a")
        code, out, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        st = self.state()
        self.assertEqual((st.method, st.channel, st.band), ("hostapd", 6, "bg"))
        self.assertIn("Your band setting is 5 GHz, but the hotspot is on 2.4 GHz", out)
        self.assertIn("set method to p2p", out)
        self.assertIn("Why it runs this way:", self.apsta("status")[1])
        self.assertIn("Your band setting is 5 GHz", " ".join(self.apsta_json("status", "--json")["hotspot"]["notes"]))
        self.apsta("stop")

    def test_wifi_direct_from_settings_uses_my_band_and_channel(self):
        self.apsta("config", "--set", "method=p2p", "--set", "band=a", "--set", "channel=157")
        code, out, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        st = self.state()
        self.assertEqual((st.method, st.channel, st.band), ("p2p", 157, "a"))
        self.assertIn("P2P_GROUP_ADD persistent=0 freq=5785", self.wpa.commands)
        self.assertIn("Method p2p: chosen in your settings.", out)
        self.assertIn("Channel 157 (5 GHz): your channel setting.", out)
        self.assertIn("they share its speed", out)
        self.apsta("stop")

    def test_wifi_direct_stays_on_the_wifis_channel_when_settings_agree(self):
        self.apsta("config", "--set", "method=p2p")
        code, out, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.state().channel, 6)
        self.assertIn("the same as your WiFi's, so the radio doesn't have to switch", out)
        self.assertNotIn("share its speed", out)
        self.apsta("stop")

    def test_channel_setting_while_sharing_the_wifis_channel_is_explained(self):
        self.apsta("config", "--set", "channel=11")
        code, out, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.state().channel, 6)
        self.assertIn("Your channel setting (11) is not used: the hotspot shares your WiFi's channel.", out)
        self.apsta("stop")

    def test_blocked_channel_setting_is_explained(self):
        self.apsta("config", "--set", "method=p2p", "--set", "band=a", "--set", "channel=44")
        code, out, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.state().band, "a")
        self.assertNotEqual(self.state().channel, 44)
        self.assertIn("isn't allowed to start a network on channel 44", out)
        self.apsta("stop")


@unittest.skipUnless(wpa.jeepney_installed(), "needs the jeepney D-Bus library")
class WifiDirectOverDBusTests(FakeWorldTestCase):
    """Same case on a distribution without the control socket (Fedora, openSUSE, Alpine, Void)."""

    link = {"ssid": "NIT-Student", "freq": 5640}

    def setUp(self):
        super().setUp()
        self.bus = FakeBus(self.answer).install(self)
        self.ssid = None

    def answer(self, path, member, body):
        sysfs = paths.SYSFS_NET / "p2p-wlo1-0"
        data = self.world
        if member == "AddPersistentGroup":
            self.ssid = body[0]["ssid"][1].decode()
        if member == "GroupAdd":
            data["ifaces"]["p2p-wlo1-0"] = {"type": "P2P-GO", "addr": "e4:00:00:00:00:02", "link": None}
            data["ifaces"]["p2p-wlo1-0"]["ssid"] = self.ssid
            sysfs.mkdir(parents=True, exist_ok=True)
            (sysfs / "operstate").write_text("up\n")
        if member == "Disconnect" and path == self.bus.iface_path("p2p-wlo1-0"):
            data["ifaces"].pop("p2p-wlo1-0", None)
            for child in sysfs.glob("*"):
                child.unlink()
            sysfs.rmdir()
        (self.root / "world.json").write_text(json.dumps(data))
        return self.bus.default(path, member, body)

    def test_hotspot_runs_on_its_own_channel(self):
        code, _, err = self.apsta("start")
        self.assertEqual(code, 0, err)
        st = self.state()
        self.assertEqual((st.method, st.p2p_backend, st.ap_interface, st.channel), ("p2p", "dbus", "p2p-wlo1-0", 6))
        self.assertEqual(st.p2p_network, "/fi/w1/wpa_supplicant1/Interfaces/0/PersistentGroups/0")
        self.assertEqual(self.bus.members(), ["GetInterface", "AddPersistentGroup", "GetInterface", "GroupAdd"])

        code, _, err = self.apsta("stop")
        self.assertEqual(code, 0, err)
        self.assertNotIn("p2p-wlo1-0", self.world["ifaces"])
        self.assertIn("Disconnect", self.bus.members())
        self.assertEqual(self.bus.groups, {})  # apsta's group and wpa_supplicant's copy
        self.assertEqual(self.world["iptables"], [])


class NoIrChannelTests(FakeWorldTestCase):
    """Real case: Intel card, WiFi (a phone hotspot) on 5 GHz channel 44, which the card marks no-IR."""

    link = {"ssid": "Phone", "freq": 5220}

    def test_refused_up_front_with_workaround(self):
        code, _, err = self.apsta("start")
        self.assertEqual(code, 1)
        self.assertIn("5 GHz channel 44", err)
        self.assertIn("2.4 GHz", err)
        self.assertNotIn("wlo1_ap", self.world["ifaces"])  # nothing was touched
        self.assertEqual(self.world["iptables"], [])

    def test_detect_warns(self):
        data = self.apsta_json("detect", "--json")
        self.assertEqual(data["verdict"]["level"], "warn")

    def test_allow_disconnect_hosts_on_an_allowed_channel(self):
        code, _, err = self.apsta("start", "--allow-disconnect")
        self.assertEqual(code, 0, err)
        st = self.state()
        self.assertEqual(st.method, "nmcli-single")
        self.assertEqual((st.band, st.channel), ("bg", 6))  # config band, least congested allowed channel
        self.apsta("stop")


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
        self.assertEqual(data["methods"]["hostapd"], "ready")
        self.assertEqual(data["methods"]["nmcli"], "ready")
        self.assertIn("wpa_supplicant", data["methods"]["p2p"])  # no control socket in this world
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
