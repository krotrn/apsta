import unittest

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


if __name__ == "__main__":
    unittest.main()
