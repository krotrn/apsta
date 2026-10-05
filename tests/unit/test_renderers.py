"""Pure renderers/parsers for hostapd, dnsmasq, NetworkManager and subnets."""

import ipaddress
import unittest

from apsta_cli.core.errors import SetupError
from apsta_cli.net import dnsmasq, hostapd, nm, subnet
from apsta_cli.net.channels import Channel


def conf(**kw):
    base = dict(
        interface="wlo1_ap",
        ssid="Café; #1",
        password="secret123",
        channel=Channel(6, "bg"),
        country="IN",
        ctrl_dir="/run/apsta/hostapd",
    )
    base.update(kw)
    return hostapd.HostapdConfig(**base)


class HostapdTests(unittest.TestCase):
    def test_render_essentials(self):
        text = hostapd.render(conf())
        self.assertIn("ctrl_interface=/run/apsta/hostapd\n", text)
        self.assertIn("ssid2=" + "Café; #1".encode().hex() + "\n", text)
        self.assertIn("hw_mode=g\n", text)
        self.assertIn("ieee80211n=1\n", text)
        self.assertIn("wmm_enabled=1\n", text)
        self.assertIn("country_code=IN\n", text)
        self.assertIn("wpa_passphrase=secret123\n", text)
        self.assertNotIn("ieee80211ac", text)

    def test_5ghz_enables_ac(self):
        text = hostapd.render(conf(channel=Channel(36, "a"), country=None))
        self.assertIn("hw_mode=a\n", text)
        self.assertIn("ieee80211ac=1\n", text)
        self.assertNotIn("country_code", text)

    def test_open_broadcast_by_default(self):
        text = hostapd.render(conf())
        self.assertIn("ignore_broadcast_ssid=0\n", text)
        self.assertIn("macaddr_acl=0\n", text)
        self.assertNotIn("accept_mac_file", text)

    def test_hidden_and_allowlist(self):
        text = hostapd.render(conf(hidden=True, accept_file="/run/apsta/hostapd.accept"))
        self.assertIn("ignore_broadcast_ssid=1\n", text)
        self.assertIn("macaddr_acl=1\naccept_mac_file=/run/apsta/hostapd.accept\n", text)
        self.assertEqual(
            hostapd.render_accept(["aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66"]), "aa:bb:cc:dd:ee:ff\n11:22:33:44:55:66\n"
        )

    def test_raw_psk(self):
        psk = "AB" * 32
        self.assertIn(f"wpa_psk={psk.lower()}\n", hostapd.render(conf(password=psk)))

    def test_parse_all_sta(self):
        out = "aa:bb:cc:dd:ee:ff\nflags=[AUTH]\nAA:BB:CC:00:11:22\nrx_bytes=1\n"
        self.assertEqual(hostapd.parse_all_sta(out), ["aa:bb:cc:dd:ee:ff", "aa:bb:cc:00:11:22"])


class DnsmasqTests(unittest.TestCase):
    def test_render_uses_host_resolvers(self):
        text = dnsmasq.render(dnsmasq.DnsmasqConfig("ap", "10.0.0.1", "10.0.0.10", "10.0.0.200", "/run/l"))
        self.assertIn("listen-address=10.0.0.1\n", text)
        self.assertIn("dhcp-range=10.0.0.10,10.0.0.200,255.255.255.0,12h\n", text)
        self.assertNotIn("8.8.8.8", text)
        self.assertNotIn("no-resolv", text)

    def test_parse_leases(self):
        leases = dnsmasq.parse_leases("1 AA:BB:CC:DD:EE:FF 10.0.0.20 phone *\n2 11:22:33:44:55:66 10.0.0.21 * *\nbad\n")
        self.assertEqual(leases[0], {"mac": "aa:bb:cc:dd:ee:ff", "ip": "10.0.0.20", "hostname": "phone"})
        self.assertEqual(leases[1]["hostname"], "")
        self.assertEqual(len(leases), 2)


class KeyfileTests(unittest.TestCase):
    def test_render(self):
        text = nm.render_keyfile(
            interface="wlo1_ap",
            ssid="Hi;",
            password=" pa\\ss word",
            channel=Channel(36, "a"),
            cloned_mac="02:00:00:00:00:01",
            connection_uuid="u-1",
        )
        self.assertIn("id=apsta-hotspot\n", text)
        self.assertIn("uuid=u-1\n", text)
        self.assertIn("interface-name=wlo1_ap\n", text)
        self.assertIn("ssid=72;105;59;\n", text)
        self.assertIn("band=a\nchannel=36\n", text)
        self.assertIn("cloned-mac-address=02:00:00:00:00:01\n", text)
        self.assertIn("psk=\\spa\\\\ss word\n", text)
        self.assertIn("[ipv4]\nmethod=shared", text)

    def test_render_without_mac(self):
        text = nm.render_keyfile(interface="wlo1", ssid="a", password="12345678", channel=Channel(1, "bg"))
        self.assertNotIn("cloned-mac-address", text)
        self.assertNotIn("hidden=", text)

    def test_render_hidden(self):
        text = nm.render_keyfile(interface="wlo1", ssid="a", password="12345678", channel=Channel(1, "bg"), hidden=True)
        self.assertIn("hidden=true\n", text)


class UnmanagedConfTests(unittest.TestCase):
    def test_appends_instead_of_replacing_user_settings(self):
        text = nm.render_unmanaged_conf("wlo1_ap")
        self.assertIn("[keyfile]\nunmanaged-devices+=interface-name:wlo1_ap\n", text)


class SubnetTests(unittest.TestCase):
    def test_first_candidate_when_free(self):
        self.assertEqual(str(subnet.pick([])), "192.168.42.0/24")

    def test_skips_overlapping(self):
        used = [ipaddress.IPv4Network("192.168.42.0/24"), ipaddress.IPv4Network("10.0.0.0/8")]
        self.assertEqual(str(subnet.pick(used)), "172.30.42.0/24")

    def test_all_taken(self):
        with self.assertRaises(SetupError):
            subnet.pick([ipaddress.IPv4Network("0.0.0.0/1"), ipaddress.IPv4Network("128.0.0.0/1")])

    def test_parse_networks_ignores_default_routes(self):
        text = "2: wlo1    inet 192.168.1.5/24 brd 192.168.1.255\ndefault via 192.168.1.1 dev wlo1\n0.0.0.0/0 dev x\n"
        self.assertEqual([str(n) for n in subnet.parse_networks(text)], ["192.168.1.0/24"])

    def test_addresses(self):
        self.assertEqual(
            subnet.addresses(ipaddress.IPv4Network("10.42.42.0/24")), ("10.42.42.1", "10.42.42.10", "10.42.42.200")
        )


if __name__ == "__main__":
    unittest.main()
