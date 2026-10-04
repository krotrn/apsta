import unittest

from apsta_cli.core import paths
from apsta_cli.core.errors import SetupError, UsageError
from apsta_cli.net import clients
from apsta_cli.net.clients import Client
from apsta_cli.state import HotspotState
from tests.support import FakeShell, isolate_paths

PHONE = "aa:bb:cc:dd:ee:ff"
LAPTOP = "11:22:33:44:55:66"


def st(method="hostapd"):
    return HotspotState(
        method=method,
        base_interface="wlo1",
        ap_interface="wlo1_ap",
        ssid="S",
        channel=6,
        band="bg",
        same_channel_required=True,
    )


class ResolveTests(unittest.TestCase):
    clients_ = [
        Client(PHONE, "10.0.0.20", "phone"),
        Client(LAPTOP, "10.0.0.21", "10.0.0.20"),  # hostname chosen to look like another IP
        Client("77:88:99:aa:bb:cc", "10.0.0.22", "dup"),
        Client("77:88:99:aa:bb:cd", "10.0.0.23", "dup"),
    ]

    def test_mac_ip_hostname_precedence(self):
        self.assertEqual(clients.resolve(self.clients_, PHONE.upper()).mac, PHONE)
        self.assertEqual(clients.resolve(self.clients_, "10.0.0.20").mac, PHONE)  # IP beats spoofed hostname
        self.assertEqual(clients.resolve(self.clients_, "phone").mac, PHONE)

    def test_ambiguous_hostname(self):
        with self.assertRaises(UsageError):
            clients.resolve(self.clients_, "dup")

    def test_unknown_and_empty(self):
        with self.assertRaises(UsageError):
            clients.resolve(self.clients_, "tablet")
        with self.assertRaises(UsageError):
            clients.resolve(self.clients_, " ")

    def test_disconnected_mac_still_resolves(self):
        self.assertEqual(clients.resolve([], "de:ad:be:ef:00:01").mac, "de:ad:be:ef:00:01")


class ParsingTests(unittest.TestCase):
    def test_station_dump(self):
        text = f"Station {PHONE.upper()} (on wlo1_ap)\n\tinactive time: 10 ms\nStation {LAPTOP} (on wlo1_ap)\n"
        self.assertEqual(clients.parse_station_dump(text), [PHONE, LAPTOP])

    def test_neigh(self):
        text = f"10.0.0.5 lladdr {PHONE.upper()} REACHABLE\n10.0.0.6 FAILED\n"
        self.assertEqual(clients.parse_neigh(text), {PHONE: "10.0.0.5"})


class ListTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)
        paths.RUN_DIR.mkdir()
        paths.DNSMASQ_LEASES.write_text(f"1 {PHONE} 10.0.0.20 phone *\n1 {LAPTOP} 10.0.0.21 gone *\n")

    def test_only_associated_stations_listed(self):
        FakeShell().on("hostapd_cli", stdout=f"{PHONE}\nflags=[AUTH]\n").install(self)
        state = st()
        state.client_limits = {PHONE: {"pref": 49152, "kbps": 500}}
        state.blocked = [PHONE]
        result = clients.list_clients(state)
        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0].to_dict(),
            {"mac": PHONE, "ip": "10.0.0.20", "hostname": "phone", "limit_kbps": 500, "blocked": True},
        )

    def test_falls_back_to_iw_and_neigh(self):
        sh = FakeShell().install(self)
        sh.on("hostapd_cli", rc=1)
        sh.on("iw", "dev", "wlo1_ap", "station", "dump", stdout="Station de:ad:be:ef:00:01 (on wlo1_ap)\n")
        sh.on("ip", "-4", "neigh", stdout="10.0.0.99 dev wlo1_ap lladdr de:ad:be:ef:00:01 STALE\n")
        result = clients.list_clients(st())
        self.assertEqual((result[0].mac, result[0].ip), ("de:ad:be:ef:00:01", "10.0.0.99"))

    def test_missing_leases_file(self):
        paths.DNSMASQ_LEASES.unlink()
        FakeShell().on("iw", "dev", "wlo1_ap", "station", "dump", stdout=f"Station {PHONE}\n").install(self)
        self.assertEqual(clients.list_clients(st("nmcli"))[0].ip, "")


class ActionTests(unittest.TestCase):
    def test_disconnect_hostapd(self):
        sh = FakeShell().on("hostapd_cli", stdout="OK").install(self)
        clients.disconnect(st(), PHONE, block=False)
        self.assertTrue(any("deauthenticate" in c for c in sh.calls))
        self.assertFalse(sh.called("iw"))

    def test_block_records_and_unblock(self):
        FakeShell().on("hostapd_cli", stdout="OK").install(self)
        state = st()
        clients.disconnect(state, PHONE, block=True)
        self.assertEqual(state.blocked, [PHONE])
        clients.unblock(state, PHONE)
        self.assertEqual(state.blocked, [])

    def test_block_failure(self):
        FakeShell().on("hostapd_cli", stdout="FAIL").install(self)
        with self.assertRaises(SetupError):
            clients.disconnect(st(), PHONE, block=True)

    def test_disconnect_falls_back_to_iw(self):
        sh = FakeShell().on("hostapd_cli", stdout="FAIL").install(self)
        clients.disconnect(st(), PHONE, block=False)
        self.assertTrue(sh.called("iw", "dev", "wlo1_ap", "station", "del", PHONE))

    def test_block_needs_hostapd(self):
        FakeShell().install(self)
        with self.assertRaises(UsageError):
            clients.disconnect(st("nmcli"), PHONE, block=True)
        with self.assertRaises(UsageError):
            clients.unblock(st("nmcli"), PHONE)

    def test_allocate_pref_is_unique(self):
        limits = {}
        for i in range(20):
            mac = f"aa:bb:cc:dd:ee:{i:02x}"
            limits[mac] = {"pref": clients.allocate_pref(limits, mac), "kbps": 1}
        self.assertEqual(len({v["pref"] for v in limits.values()}), 20)
        self.assertEqual(clients.allocate_pref(limits, "aa:bb:cc:dd:ee:00"), clients.PREF_BASE)

    def test_set_and_clear_limit(self):
        sh = FakeShell().on("tc", "qdisc", rc=2, stderr="RTNETLINK answers: File exists").install(self)
        state = st()
        clients.set_limit(state, PHONE, 800)
        clients.set_limit(state, LAPTOP, 400)
        self.assertEqual(state.client_limits[PHONE], {"pref": 49152, "kbps": 800})
        self.assertEqual(state.client_limits[LAPTOP]["pref"], 49153)
        added = sh.matching("tc", "filter", "add")
        self.assertIn("800kbit", added[0])
        self.assertTrue(clients.clear_limit(state, PHONE))
        self.assertFalse(clients.clear_limit(state, PHONE))

    def test_limit_errors(self):
        FakeShell().on("tc", "qdisc", rc=2, stderr="no clsact").install(self)
        with self.assertRaises(UsageError):
            clients.set_limit(st(), PHONE, 0)
        with self.assertRaises(SetupError):
            clients.set_limit(st(), PHONE, 10)

    def test_limit_filter_failure_cleans_up(self):
        sh = FakeShell().on("tc", "filter", "add", rc=2, stderr="unknown filter flower").install(self)
        state = st()
        with self.assertRaises(SetupError):
            clients.set_limit(state, PHONE, 10)
        self.assertEqual(state.client_limits, {})
        self.assertTrue(sh.called("tc", "filter", "del"))


if __name__ == "__main__":
    unittest.main()
