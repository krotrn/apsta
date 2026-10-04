import unittest

from apsta_cli.core.errors import HardwareError
from apsta_cli.net import channels
from apsta_cli.net.channels import Channel


class FrequencyTests(unittest.TestCase):
    def test_24ghz(self):
        self.assertEqual(channels.from_freq(2412), Channel(1, "bg"))
        self.assertEqual(channels.from_freq(2437), Channel(6, "bg"))
        self.assertEqual(channels.from_freq(2484), Channel(14, "bg"))

    def test_5ghz(self):
        self.assertEqual(channels.from_freq(5180), Channel(36, "a"))
        self.assertEqual(channels.from_freq(5825), Channel(165, "a"))
        self.assertEqual(channels.from_freq(5885), Channel(177, "a"))

    def test_6ghz(self):
        self.assertEqual(channels.from_freq(5955), Channel(1, "6g"))
        self.assertEqual(channels.from_freq(6115), Channel(33, "6g"))

    def test_unknown(self):
        self.assertIsNone(channels.from_freq(7000 + 300))
        self.assertIsNone(channels.from_freq(2413))

    def test_dfs(self):
        self.assertTrue(Channel(52, "a").is_dfs)
        self.assertTrue(Channel(144, "a").is_dfs)
        self.assertFalse(Channel(36, "a").is_dfs)
        self.assertFalse(Channel(149, "a").is_dfs)
        self.assertFalse(Channel(100, "6g").is_dfs)

    def test_labels(self):
        self.assertEqual(Channel(1, "bg").label, "2.4 GHz")
        self.assertEqual(Channel(36, "a").label, "5 GHz")
        self.assertEqual(Channel(5, "6g").label, "6 GHz")


class PlanTests(unittest.TestCase):
    def test_same_channel_follows_sta(self):
        plan = channels.plan(Channel(44, "a"), True, "bg", "6")
        self.assertEqual(plan.channel, Channel(44, "a"))

    def test_same_channel_rejects_dfs(self):
        with self.assertRaises(HardwareError) as ctx:
            channels.plan(Channel(100, "a"), True, "bg", "6")
        self.assertIn("DFS", ctx.exception.message)

    def test_same_channel_rejects_6ghz(self):
        with self.assertRaises(HardwareError):
            channels.plan(Channel(37, "6g"), True, "bg", "6")

    def test_multichannel_ignores_sta_and_picks_quiet_channel(self):
        plan = channels.plan(Channel(100, "a"), False, "bg", "6", [(1, 90), (6, 80), (11, 10)])
        self.assertEqual(plan.channel, Channel(11, "bg"))

    def test_no_scan_uses_configured(self):
        self.assertEqual(channels.plan(None, True, "a", "40").channel, Channel(40, "a"))

    def test_bad_configured_channel_uses_default(self):
        self.assertEqual(channels.plan(None, True, "bg", "x").channel, Channel(6, "bg"))
        self.assertEqual(channels.plan(None, True, "a", None).channel, Channel(36, "a"))

    def test_least_congested_ties_pick_lowest(self):
        self.assertEqual(channels.least_congested("a", [(36, 50), (40, 50), (44, 50), (48, 50)]), 36)
        self.assertIsNone(channels.least_congested("bg", [(3, 50)]))

    def test_parse_nmcli_scan(self):
        self.assertEqual(list(channels.parse_nmcli_scan("1:80\n6:\nfoo:1\n11:30")), [(1, 80), (6, 40), (11, 30)])


if __name__ == "__main__":
    unittest.main()
