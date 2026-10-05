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

    def test_freq_round_trips(self):
        for freq in (2412, 2437, 2484, 5180, 5640, 5825, 5955):
            self.assertEqual(channels.from_freq(freq).freq, freq)

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

    def test_sta_on_no_ir_channel_is_refused_with_workaround(self):
        allowed = channels.allowed_channels([2437, 5745])
        with self.assertRaises(HardwareError) as ctx:
            channels.plan(Channel(44, "a"), True, "bg", "6", allowed=allowed)
        self.assertIn("5 GHz channel 44", ctx.exception.message)
        self.assertTrue(any("2.4 GHz" in hint for hint in ctx.exception.hints))
        self.assertTrue(any("--allow-disconnect" in hint for hint in ctx.exception.hints))

    def test_free_choice_uses_only_allowed_channels(self):
        allowed = channels.allowed_channels([2412, 2437, 2462, 5745, 5765])
        self.assertEqual(channels.plan(None, True, "a", "36", allowed=allowed).channel, Channel(149, "a"))
        scan = [(1, 10), (6, 90), (11, 90), (36, 1)]
        self.assertEqual(channels.plan(None, True, "bg", "6", scan, allowed).channel, Channel(1, "bg"))

    def test_band_without_allowed_channels_falls_back_to_24ghz(self):
        allowed = channels.allowed_channels([2412, 2437, 2462])
        self.assertEqual(channels.plan(None, False, "a", "36", allowed=allowed).channel.band, "bg")
        self.assertIsNone(channels.allowed_channels([]))

    def test_parse_nmcli_scan(self):
        self.assertEqual(list(channels.parse_nmcli_scan("1:80\n6:\nfoo:1\n11:30")), [(1, 80), (6, 40), (11, 30)])


if __name__ == "__main__":
    unittest.main()
