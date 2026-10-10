import unittest

from apsta_cli.core import paths
from apsta_cli.core.errors import SetupError
from apsta_cli.net import firewall
from tests.support import FakeShell, isolate_paths

AP, NET = "wlo1_ap", "192.168.42.0/24"


class ForwardingTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)

    def test_enable_and_restore(self):
        self.assertEqual(firewall.enable_forwarding(), "0")
        self.assertEqual(paths.IP_FORWARD.read_text().strip(), "1")
        firewall.restore_forwarding("0")
        self.assertEqual(paths.IP_FORWARD.read_text().strip(), "0")

    def test_restore_noop_when_previously_on(self):
        paths.IP_FORWARD.write_text("1\n")
        firewall.enable_forwarding()
        firewall.restore_forwarding("1")
        self.assertEqual(paths.IP_FORWARD.read_text().strip(), "1")

    def test_enable_failure(self):
        paths.IP_FORWARD.unlink()
        paths.IP_FORWARD.mkdir()
        with self.assertRaises(SetupError):
            firewall.enable_forwarding()


class IptablesTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)

    def test_rules_masquerade_any_uplink(self):
        nat = firewall.IptablesBackend.rules(AP, NET)[0]
        self.assertEqual(nat[:2], ("nat", "POSTROUTING"))
        self.assertNotIn("-o", nat[2])
        self.assertIn("apsta", nat[2])

    def test_apply_inserts_at_top_and_revert_deletes(self):
        sh = FakeShell(["iptables"]).install(self)
        record = firewall.apply(AP, NET)
        self.assertEqual(record["backend"], "iptables")
        self.assertEqual(firewall.caveats(AP, record), [])
        inserts = sh.matching("iptables", "-w", "-t")
        self.assertTrue(all(c[4] == "-I" and c[6] == "1" for c in inserts))
        firewall.revert(AP, NET, record)
        self.assertEqual(len(sh.matching("iptables")), 2 * len(firewall.IptablesBackend.rules(AP, NET)))
        self.assertEqual(paths.IP_FORWARD.read_text().strip(), "0")

    def test_partial_failure_rolls_back_rules_and_forwarding(self):
        sh = FakeShell(["iptables"]).install(self)
        sh.on("iptables", "-w", "-t", "filter", "-I", "INPUT", rc=1, stderr="no xt_comment")
        with self.assertRaises(SetupError):
            firewall.apply(AP, NET)
        self.assertEqual(len(sh.matching("iptables", "-w", "-t", "nat", "-D")), 1)
        self.assertEqual(paths.IP_FORWARD.read_text().strip(), "0")

    def test_revert_empty_record(self):
        firewall.revert(AP, NET, {})


class FirewalldTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)

    def test_selected_when_running_and_only_adds_missing_masquerade(self):
        sh = FakeShell(["firewall-cmd", "iptables"]).install(self)
        sh.on(
            "ip", "-4", "route", "show", "default", stdout="default via 1.1.1.1 dev wlo1\ndefault via 2.2.2.2 dev tun0"
        )
        sh.on("firewall-cmd", "--get-zone-of-interface=wlo1", stdout="public")
        sh.on("firewall-cmd", "--get-zone-of-interface=tun0", rc=2)
        sh.on("firewall-cmd", "--get-default-zone", stdout="home")
        sh.on("firewall-cmd", "--zone=home", "--query-masquerade", rc=0)
        sh.on("firewall-cmd", "--zone=public", "--query-masquerade", rc=1)
        record = firewall.apply(AP, NET)
        self.assertEqual(record["backend"], "firewalld")
        self.assertEqual(record["data"], {"masquerade_zones": ["public"]})
        firewall.revert(AP, NET, record)
        self.assertTrue(sh.called("firewall-cmd", "--zone=public", "--remove-masquerade"))
        self.assertFalse(sh.called("firewall-cmd", "--zone=home", "--remove-masquerade"))
        self.assertTrue(sh.called("firewall-cmd", "--zone=trusted", f"--remove-interface={AP}"))

    def test_failure_reverts(self):
        sh = FakeShell(["firewall-cmd"]).install(self)
        sh.on("ip", "-4", "route", "show", "default", stdout="default via 1.1.1.1 dev wlo1")
        sh.on("firewall-cmd", "--get-zone-of-interface=wlo1", stdout="public")
        sh.on("firewall-cmd", "--zone=public", "--query-masquerade", rc=1)
        sh.on("firewall-cmd", "--zone=public", "--add-masquerade", rc=1, stderr="denied")
        with self.assertRaises(SetupError):
            firewall.apply(AP, NET)
        self.assertTrue(sh.called("firewall-cmd", "--zone=trusted", f"--remove-interface={AP}"))


class NftablesTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)

    def test_used_without_iptables_and_notes_drop_policy(self):
        sh = FakeShell(["nft"]).install(self)
        sh.on(
            "nft",
            "list",
            "ruleset",
            stdout="table inet filter {\n chain forward {\n  type filter hook forward priority 0; policy drop;\n }\n}",
        )
        record = firewall.apply(AP, NET)
        self.assertEqual(record["backend"], "nftables")
        [caveat] = firewall.caveats(AP, record)
        self.assertIn(f"allow forwarding from {AP}", caveat)
        loaded = sh.inputs[sh.calls.index(["nft", "-f", "-"])]
        self.assertIn(f"ip saddr {NET}", loaded)
        firewall.revert(AP, NET, record)
        self.assertTrue(sh.called("nft", "delete", "table", "ip", "apsta"))

    def test_no_note_when_forwarding_is_allowed(self):
        FakeShell(["nft"]).install(self).on("nft", "list", "ruleset", stdout="table ip apsta {\n}")
        self.assertEqual(firewall.caveats(AP, firewall.apply(AP, NET)), [])

    def test_no_backend(self):
        FakeShell([]).install(self)
        with self.assertRaises(SetupError):
            firewall.detect()


if __name__ == "__main__":
    unittest.main()
