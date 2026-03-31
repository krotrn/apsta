import unittest
from unittest.mock import patch

from apsta_cli.net import support


class HotspotHelperTests(unittest.TestCase):
    def test_freq_to_channel(self):
        self.assertEqual(support._freq_to_channel(2412), "1")
        self.assertEqual(support._freq_to_channel(5180), "36")
        self.assertIsNone(support._freq_to_channel(7000))

    def test_is_dfs_channel(self):
        self.assertTrue(support._is_dfs_channel("52"))
        self.assertTrue(support._is_dfs_channel("100"))
        self.assertFalse(support._is_dfs_channel("36"))
        self.assertFalse(support._is_dfs_channel("invalid"))

    @patch("apsta_cli.net.support.run_out")
    def test_pick_least_congested_channel_24g(self, mock_run_out):
        mock_run_out.return_value = "1:80\n6:35\n6:40\n11:70\n"
        self.assertEqual(support._pick_least_congested_channel("wlan0", "bg"), "11")

    @patch("apsta_cli.net.support.run_out")
    def test_pick_least_congested_channel_returns_none_when_scan_missing(self, mock_run_out):
        mock_run_out.return_value = ""
        self.assertIsNone(support._pick_least_congested_channel("wlan0", "a"))


if __name__ == "__main__":
    unittest.main()
