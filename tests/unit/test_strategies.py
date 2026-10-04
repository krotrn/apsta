import unittest
from unittest import mock

from apsta_cli.core import paths
from apsta_cli.core.errors import SetupError, UsageError
from apsta_cli.hw.capability import HardwareCapability
from apsta_cli.hw.interfaces import WifiInterface
from apsta_cli.net import strategies
from apsta_cli.net.channels import Channel
from apsta_cli.net.transaction import Transaction
from tests.support import FakeShell, isolate_paths


def ctx(ap_sta=True, supports_ap=True, sta_ssid="Home", allow=False):
    cap = HardwareCapability("wlo1", "phy0", supports_ap, True, ap_sta, True, 1)
    return strategies.StartContext(
        base=WifiInterface("wlo1", "aa", "phy0", "managed", "UP", sta_ssid),
        capability=cap,
        ssid="Cafe",
        password="secret123",
        channel=Channel(6, "bg"),
        country="IN",
        sta_ssid=sta_ssid,
        allow_disconnect=allow,
    )


class FakeSup:
    kind = "fake"

    def __init__(self):
        self.started, self.stopped = [], []

    def start(self, d):
        self.started.append(d.name)

    def stop(self, d):
        self.stopped.append(d.name)

    def running(self, d):
        return True

    def logs(self, d):
        return "hostapd: nl80211 driver init failed\nmore"


def ap_mode_shell(testcase, tools=("hostapd", "dnsmasq", "nmcli", "iptables"), ap_up=True):
    sh = FakeShell(tools)
    # A freshly created AP vif is "type AP" either way; only a running AP has an SSID.
    up_info = "Interface x\n\tssid Cafe\n\ttype AP\n"
    sh.on("iw", "dev", "wlo1_ap", "info", stdout=up_info if ap_up else "Interface x\n\ttype AP\n")
    sh.on("iw", "dev", "wlo1", "info", stdout=up_info)
    sh.on("hostapd_cli", stdout="state=ENABLED\n" if ap_up else "state=DISABLED\n")
    sh.on("ip", "-4", "-o", "addr", "show", stdout="3: wlo1 inet 192.168.42.7/24 brd x")

    def add_iface(argv):
        (paths.SYSFS_NET / argv[5]).mkdir(exist_ok=True)
        return strategies.shell.Result(argv, 0, "", "")

    def del_iface(argv):
        path = paths.SYSFS_NET / argv[2]
        if path.exists():
            path.rmdir()
        return strategies.shell.Result(argv, 0, "", "")

    sh.on("iw", "dev", "wlo1", "interface", "add", fn=add_iface)
    sh.on("iw", "dev", "wlo1_ap", "del", fn=del_iface)
    return sh.install(testcase)


class AvailabilityTests(unittest.TestCase):
    def test_reasons(self):
        FakeShell(["nmcli"]).install(self)
        self.assertIn("not installed", strategies.HostapdStrategy().unavailable(ctx()))
        self.assertIn("cannot run", strategies.HostapdStrategy().unavailable(ctx(ap_sta=False)))
        self.assertIsNone(strategies.NmVirtualStrategy().unavailable(ctx()))
        self.assertIn("cannot run", strategies.NmVirtualStrategy().unavailable(ctx(ap_sta=False)))
        self.assertIn("--allow-disconnect", strategies.NmSingleStrategy().unavailable(ctx()))
        self.assertIsNone(strategies.NmSingleStrategy().unavailable(ctx(allow=True)))
        self.assertIsNone(strategies.NmSingleStrategy().unavailable(ctx(sta_ssid=None)))
        self.assertIn("AP mode", strategies.NmSingleStrategy().unavailable(ctx(supports_ap=False)))

    def test_without_nmcli(self):
        FakeShell([]).install(self)
        self.assertIn("nmcli", strategies.NmVirtualStrategy().unavailable(ctx()))
        self.assertIn("nmcli", strategies.NmSingleStrategy().unavailable(ctx(allow=True)))

    def test_candidates(self):
        self.assertEqual([s.name for s in strategies.candidates("auto")], ["hostapd", "nmcli", "nmcli-single"])
        self.assertEqual([s.name for s in strategies.candidates("nmcli")], ["nmcli"])
        with self.assertRaises(UsageError):
            strategies.candidates("magic")

    def test_for_state_legacy_name(self):
        st = mock.Mock(method="nmcli-force")
        self.assertIsInstance(strategies.for_state(st), strategies.NmSingleStrategy)


class HostapdStrategyTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)
        self.sup = FakeSup()
        patcher = mock.patch("apsta_cli.net.supervisor.get", return_value=self.sup)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_start_builds_everything_and_stop_undoes_it(self):
        sh = ap_mode_shell(self)
        with Transaction() as tx:
            state = strategies.HostapdStrategy().start(ctx(), tx)
            tx.commit()
        self.assertEqual(state.subnet, "10.42.42.0/24")  # 192.168.42.0/24 is taken upstream
        self.assertEqual(state.gateway, "10.42.42.1")
        self.assertEqual(state.firewall["backend"], "iptables")
        self.assertEqual(self.sup.started, ["hostapd", "dnsmasq"])
        conf = paths.HOSTAPD_CONF.read_text()
        self.assertIn("country_code=IN", conf)
        self.assertEqual(oct(paths.HOSTAPD_CONF.stat().st_mode & 0o777), "0o600")
        self.assertIn("listen-address=10.42.42.1", paths.DNSMASQ_CONF.read_text())
        self.assertTrue(sh.called("nmcli", "device", "set", "wlo1_ap", "managed", "no"))
        nm_conf = paths.NM_RUNTIME_CONF_DIR / "90-apsta-unmanaged.conf"
        self.assertIn("interface-name:wlo1_ap", nm_conf.read_text())
        self.assertLess(
            sh.calls.index(["nmcli", "general", "reload", "conf"]),
            sh.calls.index(["iw", "dev", "wlo1", "interface", "add", "wlo1_ap", "type", "__ap"]),
        )
        self.assertTrue(sh.called("ip", "addr", "add", "10.42.42.1/24", "dev", "wlo1_ap"))

        strategies.HostapdStrategy().stop(state)
        self.assertEqual(self.sup.stopped, ["dnsmasq", "hostapd"])
        self.assertFalse((paths.SYSFS_NET / "wlo1_ap").exists())
        self.assertFalse(paths.HOSTAPD_CONF.exists())
        self.assertFalse(nm_conf.exists())
        self.assertEqual(paths.IP_FORWARD.read_text().strip(), "0")

    def test_hostapd_not_coming_up_rolls_back(self):
        ap_mode_shell(self, ap_up=False)
        with mock.patch("apsta_cli.net.hostapd.wait_enabled", return_value=False):
            with self.assertRaises(SetupError) as raised:
                with Transaction() as tx:
                    strategies.HostapdStrategy().start(ctx(), tx)
        self.assertIn("nl80211", " ".join(raised.exception.hints))
        self.assertEqual(self.sup.stopped, ["hostapd"])
        self.assertFalse((paths.SYSFS_NET / "wlo1_ap").exists())
        self.assertFalse(paths.HOSTAPD_CONF.exists())

    def test_daemons_running(self):
        st = mock.Mock(supervisor="fake")
        self.assertTrue(strategies.HostapdStrategy.daemons_running(st))


class NmStrategyTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)

    def test_virtual_start_and_stop(self):
        sh = ap_mode_shell(self)
        with Transaction() as tx:
            state = strategies.NmVirtualStrategy().start(ctx(), tx)
            tx.commit()
        keyfile = paths.NM_RUNTIME_KEYFILE_DIR / "apsta-hotspot.nmconnection"
        self.assertIn("interface-name=wlo1_ap", keyfile.read_text())
        self.assertIn("cloned-mac-address=02:", keyfile.read_text())
        self.assertEqual(oct(keyfile.stat().st_mode & 0o777), "0o600")
        self.assertFalse(any("secret123" in " ".join(c) for c in sh.calls))  # never on a command line
        self.assertEqual(state.connection_id, "apsta-hotspot")

        strategies.NmVirtualStrategy().stop(state)
        self.assertTrue(sh.called("nmcli", "connection", "delete", "id", "apsta-hotspot"))
        self.assertFalse(keyfile.exists())
        self.assertFalse((paths.SYSFS_NET / "wlo1_ap").exists())

    def test_activation_failure_rolls_back(self):
        sh = ap_mode_shell(self)
        sh.on("nmcli", "--wait", rc=4, stderr="Error: Connection activation failed")
        with self.assertRaises(SetupError):
            with Transaction() as tx:
                strategies.NmVirtualStrategy().start(ctx(), tx)
        self.assertTrue(sh.called("nmcli", "connection", "delete", "id", "apsta-hotspot"))
        self.assertFalse((paths.SYSFS_NET / "wlo1_ap").exists())

    def test_single_uses_base_interface(self):
        sh = ap_mode_shell(self)
        with Transaction() as tx:
            state = strategies.NmSingleStrategy().start(ctx(allow=True), tx)
            tx.commit()
        self.assertEqual(state.ap_interface, "wlo1")
        self.assertFalse(sh.called("iw", "dev", "wlo1", "interface", "add"))
        strategies.NmSingleStrategy().stop(state)
        self.assertFalse(sh.called("iw", "dev", "wlo1", "del"))

    def test_not_ap_after_activation(self):
        ap_mode_shell(self, ap_up=False)
        with mock.patch("apsta_cli.net.iface.wait_for_broadcast", return_value=False):
            with self.assertRaises(SetupError):
                with Transaction() as tx:
                    strategies.NmVirtualStrategy().start(ctx(), tx)


if __name__ == "__main__":
    unittest.main()
