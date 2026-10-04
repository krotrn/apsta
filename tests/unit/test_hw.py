import unittest

from apsta_cli.hw import capability, combinations, interfaces
from tests.support import FakeShell, fixture, isolate_paths


class CombinationParsingTests(unittest.TestCase):
    def test_parses_multiline_entries(self):
        combos = combinations.parse_combinations(fixture("iw/intel_alderlake.txt"))
        self.assertEqual(len(combos), 2)
        self.assertEqual(combos[0].total, 3)
        self.assertEqual(combos[0].channels, 2)
        self.assertEqual(combos[1].channels, 1)
        self.assertIn(frozenset({"AP", "P2P-client", "P2P-GO"}), {g.types for g in combos[1].groups})

    def test_stops_at_next_section(self):
        combos = combinations.parse_combinations(fixture("iw/ath10k_multichannel.txt"))
        self.assertEqual(len(combos), 1)
        self.assertNotIn("MCS", combos[0].raw)

    def test_supported_modes(self):
        modes = combinations.parse_supported_modes(fixture("iw/ath10k_multichannel.txt"))
        self.assertEqual(modes[:3], ["managed", "AP", "AP/VLAN"])

    def test_entry_without_total_is_ignored(self):
        self.assertIsNone(combinations.parse_entry("#{ managed } <= 1"))

    def test_missing_channels_defaults_to_one(self):
        combo = combinations.parse_entry("#{ managed } <= 1, #{ AP } <= 1, total <= 2")
        self.assertEqual(combo.channels, 1)


class CombinationSemanticsTests(unittest.TestCase):
    def combo(self, text):
        return combinations.parse_entry(text)

    def test_separate_groups_allow_ap_sta(self):
        self.assertTrue(self.combo("#{ managed } <= 1, #{ AP } <= 1, total <= 2, #channels <= 1").ap_sta)

    def test_shared_group_limited_to_one_is_either_or(self):
        # apsta <= 0.6 reported this as "concurrent" — it is not.
        self.assertFalse(self.combo("#{ managed, AP } <= 1, #{ monitor } <= 1, total <= 2, #channels <= 1").ap_sta)

    def test_shared_group_with_room_for_two(self):
        self.assertTrue(self.combo("#{ managed, AP } <= 2, total <= 2, #channels <= 1").ap_sta)

    def test_total_limit_applies(self):
        self.assertFalse(self.combo("#{ managed } <= 1, #{ AP } <= 1, total <= 1, #channels <= 1").ap_sta)

    def test_allows_backtracks_across_groups(self):
        combo = self.combo("#{ managed, AP } <= 1, #{ managed } <= 1, total <= 2, #channels <= 1")
        self.assertTrue(combo.allows({"managed": 1, "AP": 1}))
        self.assertFalse(combo.allows({"managed": 1, "AP": 2}))

    def test_evaluate_prefers_multichannel(self):
        combos = [
            self.combo("#{ managed } <= 1, #{ AP } <= 1, total <= 2, #channels <= 1"),
            self.combo("#{ managed } <= 1, #{ AP } <= 1, total <= 2, #channels <= 2"),
        ]
        support = combinations.evaluate(combos)
        self.assertTrue(support.supported)
        self.assertFalse(support.same_channel_required)

    def test_evaluate_without_capable_combination(self):
        support = combinations.evaluate([])
        self.assertFalse(support.supported)
        self.assertIsNone(support.combination)


class CapabilityTests(unittest.TestCase):
    def cap(self, name):
        return capability.from_iw_text("wlan0", "phy0", fixture(f"iw/{name}.txt"))

    def test_intel_same_channel(self):
        cap = self.cap("intel_alderlake")
        self.assertTrue(cap.supports_ap and cap.supports_sta and cap.ap_sta)
        self.assertTrue(cap.same_channel_required)
        self.assertEqual(cap.max_channels, 1)

    def test_ath10k_multichannel(self):
        cap = self.cap("ath10k_multichannel")
        self.assertTrue(cap.ap_sta)
        self.assertFalse(cap.same_channel_required)

    def test_either_or_is_ap_only(self):
        cap = self.cap("either_or")
        self.assertTrue(cap.supports_ap)
        self.assertFalse(cap.ap_sta)

    def test_no_combinations(self):
        cap = self.cap("no_combinations")
        self.assertTrue(cap.supports_ap)
        self.assertFalse(cap.ap_sta)
        self.assertEqual(cap.combinations, [])

    def test_no_ap(self):
        cap = self.cap("no_ap")
        self.assertFalse(cap.supports_ap)
        self.assertFalse(cap.ap_sta)

    def test_probe_scopes_to_phy(self):
        root = isolate_paths(self)
        phy_dir = root / "sys/class/net/wlan0/phy80211"
        phy_dir.mkdir(parents=True)
        (phy_dir / "name").write_text("phy1\n")
        sh = FakeShell().on("iw", "phy", "phy1", "info", stdout=fixture("iw/ath10k_multichannel.txt")).install(self)
        cap = capability.probe("wlan0")
        self.assertEqual(cap.phy, "phy1")
        self.assertTrue(cap.ap_sta)
        self.assertFalse(sh.called("iw", "list"))
        self.assertEqual(cap.chipset, "")  # no device link in the fake sysfs

    def test_to_dict(self):
        self.assertEqual(self.cap("no_ap").to_dict()["interface"], "wlan0")


IW_DEV = """phy#0
\tInterface wlo1_ap
\t\tifindex 7
\t\taddr 02:11:22:33:44:55
\t\ttype AP
\tInterface wlo1
\t\tifindex 3
\t\taddr e4:c7:67:00:00:01
\t\tssid Home
\t\ttype managed
\t\tchannel 6 (2437 MHz), width: 20 MHz
"""


class InterfaceTests(unittest.TestCase):
    def test_parse_iw_dev(self):
        ifaces = interfaces.parse_iw_dev(IW_DEV)
        self.assertEqual([i.name for i in ifaces], ["wlo1_ap", "wlo1"])
        self.assertEqual(ifaces[0].iftype, "AP")
        self.assertEqual(ifaces[1].phy, "phy0")
        self.assertEqual(ifaces[1].connected_ssid, "Home")

    def test_parse_iw_dev_skips_non_netdev_blocks(self):
        # Real Intel AX201 output: the P2P-device wdev follows the AP vif and must not
        # overwrite its type or MAC.
        text = (
            "phy#0\n\tInterface wlo1_ap\n\t\taddr 02:aa:bb:cc:dd:ee\n\t\ttype AP\n"
            "\tUnnamed/non-netdev interface\n\t\twdev 0x2\n\t\taddr e4:c7:67:e4:30:ae\n\t\ttype P2P-device\n"
            "\tInterface wlo1\n\t\taddr e4:c7:67:e4:30:ae\n\t\tssid kk_spot\n\t\ttype managed\n"
        )
        ifaces = interfaces.parse_iw_dev(text)
        self.assertEqual(
            [(i.name, i.iftype, i.mac) for i in ifaces],
            [("wlo1_ap", "AP", "02:aa:bb:cc:dd:ee"), ("wlo1", "managed", "e4:c7:67:e4:30:ae")],
        )

    def test_parse_link_new_iw_float_freq(self):
        link = interfaces.parse_link("Connected to aa:bb:cc:dd:ee:ff (on wlo1)\n\tSSID: My Net\n\tfreq: 5180.0\n")
        self.assertEqual(link.freq, 5180)
        self.assertEqual(link.ssid, "My Net")

    def test_parse_link_not_connected(self):
        self.assertIsNone(interfaces.parse_link("Not connected."))
        self.assertIsNone(interfaces.parse_link("Connected to x\n\tSSID: a\n"))

    def test_reg_country(self):
        self.assertEqual(interfaces.parse_reg_country("global\ncountry IN: DFS-JP\n\t(2402 - 2482 @ 40)"), "IN")
        self.assertIsNone(interfaces.parse_reg_country("global\ncountry 00: DFS-UNSET\n"))
        self.assertIsNone(interfaces.parse_reg_country(""))

    def test_reg_country_prefers_self_managed_phy(self):
        # Real output from an Intel AX201: global domain is world (00), the card reports IN.
        text = fixture("iw/reg_self_managed.txt")
        self.assertEqual(interfaces.parse_reg_country(text, "phy0"), "IN")
        self.assertIsNone(interfaces.parse_reg_country(text))  # global is 00
        self.assertIsNone(interfaces.parse_reg_country(text, "phy1"))
        self.assertEqual(
            interfaces.parse_reg_country("global\ncountry DE: DFS-ETSI\nphy#0 (self-managed)\ncountry 00: x\n", "phy0"),
            "DE",
        )

    def test_client_interfaces_exclude_ap_vifs(self):
        root = isolate_paths(self)
        (root / "sys/class/net/wlo1").mkdir(parents=True)
        (root / "sys/class/net/wlo1/operstate").write_text("up\n")
        FakeShell().on("iw", "dev", stdout=IW_DEV).on(
            "iw", "dev", "wlo1", "link", stdout="Connected to x\n\tSSID: Home\n\tfreq: 2437\n"
        ).install(self)
        everything = interfaces.list_wifi_interfaces()
        self.assertIsNone(next(i for i in everything if i.name == "wlo1_ap").connected_ssid)
        ifaces = interfaces.client_interfaces()
        self.assertEqual([i.name for i in ifaces], ["wlo1"])
        self.assertEqual(ifaces[0].state, "UP")
        self.assertEqual(ifaces[0].connected_ssid, "Home")

    def test_wait_for_sta_times_out(self):
        FakeShell().on("iw", "dev", "wlo1", "link", stdout="Not connected.").install(self)
        self.assertIsNone(interfaces.wait_for_sta("wlo1", timeout=0.05, poll=0.01))

    def test_iface_type(self):
        FakeShell().on("iw", "dev", "x", "info", stdout="Interface x\n\ttype AP\n").install(self)
        self.assertEqual(interfaces.iface_type("x"), "AP")


if __name__ == "__main__":
    unittest.main()
