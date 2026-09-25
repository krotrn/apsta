import os
import stat
import tempfile
import unittest
from pathlib import Path
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

    def test_ap_iface_name_fits_ifnamsiz(self):
        self.assertEqual(support._ap_iface_name("wlan0"), "wlan0_ap")
        self.assertLessEqual(len(support._ap_iface_name("wlx00c0ca123456")), 15)

    def test_write_private_replaces_planted_file_with_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conf"
            target = Path(tmp) / "victim"
            target.write_text("keep")
            path.symlink_to(target)
            support._write_private(path, "secret")
            self.assertEqual(path.read_text(), "secret")
            self.assertFalse(path.is_symlink())
            self.assertEqual(target.read_text(), "keep")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @patch("apsta_cli.net.support.os.kill")
    def test_kill_pidfile_ignores_foreign_pid(self, mock_kill):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "pid"
            pidfile.write_text(str(os.getpid()))  # a python process, not hostapd
            self.assertFalse(support._kill_pidfile(pidfile, "hostapd"))
            mock_kill.assert_not_called()
            self.assertFalse(pidfile.exists())


if __name__ == "__main__":
    unittest.main()
