import unittest

from apsta_cli.cmd.status_config import _find_client


class ClientManagementTests(unittest.TestCase):
    def setUp(self):
        self.clients = [
            {"hostname": "phone", "mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.42.20"},
            {"hostname": "laptop", "mac": "11:22:33:44:55:66", "ip": "192.168.42.21"},
            {"hostname": "", "mac": "77:88:99:aa:bb:cc", "ip": "192.168.42.22"},
        ]

    def test_find_by_mac(self):
        c = _find_client(self.clients, "AA:BB:CC:DD:EE:FF")
        self.assertIsNotNone(c)
        self.assertEqual(c["ip"], "192.168.42.20")

    def test_find_by_ip(self):
        c = _find_client(self.clients, "192.168.42.21")
        self.assertIsNotNone(c)
        self.assertEqual(c["hostname"], "laptop")

    def test_find_by_hostname(self):
        c = _find_client(self.clients, "phone")
        self.assertIsNotNone(c)
        self.assertEqual(c["mac"], "aa:bb:cc:dd:ee:ff")

    def test_find_missing_identifier(self):
        self.assertIsNone(_find_client(self.clients, "tablet"))
        self.assertIsNone(_find_client(self.clients, ""))


if __name__ == "__main__":
    unittest.main()
