import unittest

from apsta_cli.core import paths
from apsta_cli.core.errors import SetupError
from apsta_cli.net import wpa
from tests.support import FakeWpaSupplicant, isolate_paths


class WpaTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)
        self.refuse = set()
        self.wpa = FakeWpaSupplicant(paths.WPA_CTRL_DIR / "p2p-dev-wlo1", self.answer).install(self)

    def answer(self, command):
        if any(command.startswith(r) for r in self.refuse):
            return "FAIL\n"
        return self.wpa.default(command)

    def test_available_only_with_the_socket(self):
        self.assertTrue(wpa.available("wlo1"))
        self.assertFalse(wpa.available("wlan9"))

    def test_group_network(self):
        net_id = wpa.add_group_network("wlo1", "Café", 'pa"ss word', hidden=True)
        self.assertEqual(net_id, 0)
        self.assertEqual(
            self.wpa.commands,
            [
                "ADD_NETWORK",
                f"SET_NETWORK 0 ssid {'Café'.encode().hex()}",
                'SET_NETWORK 0 psk "pa"ss word"',
                "SET_NETWORK 0 key_mgmt WPA-PSK",
                "SET_NETWORK 0 proto RSN",
                "SET_NETWORK 0 pairwise CCMP",
                "SET_NETWORK 0 mode 3",
                "SET_NETWORK 0 disabled 2",
                "SET_NETWORK 0 ignore_broadcast_ssid 1",
            ],
        )
        self.assertFalse(list(paths.RUN_DIR.glob("wpa-ctrl-*")))  # client sockets are cleaned up

    def test_raw_key_is_not_quoted(self):
        key = "AB" * 32
        self.assertIn(("psk", key.lower()), wpa.network_settings("x", key, False))

    def test_refused_setting_removes_the_network(self):
        self.refuse = {"SET_NETWORK 0 psk"}
        with self.assertRaises(SetupError) as raised:
            wpa.add_group_network("wlo1", "Cafe", "secret123")
        self.assertIn("psk", raised.exception.message)
        self.assertEqual(self.wpa.commands[-1], "REMOVE_NETWORK 0")

    def test_group_start_and_removal(self):
        wpa.start_group("wlo1", 3, 2437)
        self.assertTrue(wpa.remove_group("wlo1", "p2p-wlo1-0"))
        self.assertEqual(self.wpa.commands, ["P2P_GROUP_ADD persistent=3 freq=2437", "P2P_GROUP_REMOVE p2p-wlo1-0"])
        self.refuse = {"P2P_GROUP_ADD"}
        with self.assertRaises(SetupError):
            wpa.start_group("wlo1", 3, 2437)

    def test_no_supplicant(self):
        with self.assertRaises(SetupError):
            wpa.request("wlan9", "PING", timeout=0.5)
        self.assertFalse(wpa.remove_network("wlan9", 0))  # teardown never raises


if __name__ == "__main__":
    unittest.main()
