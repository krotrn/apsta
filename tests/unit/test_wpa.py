import os
import unittest
import unittest.mock as mock

from apsta_cli.core import paths
from apsta_cli.core.errors import SetupError
from apsta_cli.net import wpa
from tests.support import FakeBus, FakeWpaSupplicant, isolate_paths


class ControlSocketTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)
        self.refuse = set()
        self.wpa = FakeWpaSupplicant(paths.WPA_CTRL_DIR / "p2p-dev-wlo1", self.answer).install(self)
        self.sup = wpa.ControlSocket("wlo1")

    def answer(self, command):
        if any(command.startswith(r) for r in self.refuse):
            return "FAIL\n"
        return self.wpa.default(command)

    def test_available_only_with_the_socket(self):
        self.assertTrue(wpa.socket_available("wlo1"))
        self.assertFalse(wpa.socket_available("wlan9"))

    def test_group_network(self):
        self.assertEqual(self.sup.add_group_network("Café", 'pa"ss word', hidden=True), "0")
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
            self.sup.add_group_network("Cafe", "secret123")
        self.assertIn("psk", raised.exception.message)
        self.assertEqual(self.wpa.commands[-1], "REMOVE_NETWORK 0")

    def test_group_start_and_removal(self):
        network = self.sup.add_group_network("Cafe", "secret123")
        self.wpa.commands.clear()
        self.sup.start_group(network, 2437)
        self.assertTrue(self.sup.remove_group("p2p-wlo1-0"))
        self.assertEqual(self.wpa.commands, ["P2P_GROUP_ADD persistent=0 freq=2437", "P2P_GROUP_REMOVE p2p-wlo1-0"])
        with self.assertRaises(SetupError):
            self.sup.start_group("7", 2437)  # no such network
        self.refuse = {"P2P_GROUP_ADD"}
        with self.assertRaises(SetupError):
            self.sup.start_group(network, 2437)

    def test_forget_removes_wpa_supplicants_copy(self):
        network = self.sup.add_group_network("Cafe", "secret123")
        self.sup.start_group(network, 2437)
        self.sup.add_group_network("Other", "secret123")  # someone else's: must survive
        self.assertEqual(len(self.wpa.networks), 3)
        self.assertEqual(self.sup.persistent_groups("Cafe"), ["0", "1"])
        self.sup.forget(network, "Cafe")
        self.assertEqual([n["ssid"] for n in self.wpa.networks.values()], [b"Other".hex()])
        self.assertEqual(self.sup.persistent_groups("Cafe"), [])

    def test_unprivileged_callers_see_the_directory(self):
        # /run/wpa_supplicant is root-only: `apsta detect` can't stat the socket inside.
        with mock.patch("pathlib.Path.is_socket", side_effect=PermissionError):
            self.assertTrue(wpa.socket_available("wlo1"))  # the directory exists in this test
            with mock.patch.object(paths, "WPA_CTRL_DIR", paths.RUN_DIR / "absent"):
                self.assertFalse(wpa.socket_available("wlo1"))

    def test_unsolicited_events_before_the_reply_are_skipped(self):
        replies = iter(["<3>P2P-DEVICE-FOUND 02:00:00:00:00:01", "<3>CTRL-EVENT-SCAN-STARTED", "OK"])
        self.wpa.handler = lambda command: None  # answer by hand below
        self.wpa.sock.close()
        import socket as socketlib
        import threading

        server = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_DGRAM)
        (paths.WPA_CTRL_DIR / "p2p-dev-wlo1").unlink()
        server.bind(str(paths.WPA_CTRL_DIR / "p2p-dev-wlo1"))
        self.addCleanup(server.close)

        def answer():
            _, addr = server.recvfrom(4096)
            for reply in replies:
                server.sendto(reply.encode(), addr)

        threading.Thread(target=answer, daemon=True).start()
        self.assertEqual(wpa.request("wlo1", "PING"), "OK")

    def test_refused_network_adds_nothing_more(self):
        self.refuse = {"ADD_NETWORK"}
        with self.assertRaises(SetupError) as raised:
            self.sup.add_group_network("Cafe", "secret123")
        self.assertIn("refused to add a network (FAIL)", raised.exception.message)
        self.assertEqual(self.wpa.commands, ["ADD_NETWORK"])

    def test_teardown_survives_a_vanished_supplicant(self):
        gone = wpa.ControlSocket("wlan9")  # no socket at all
        self.assertFalse(gone.remove_group("p2p-wlan9-0"))
        self.assertEqual(gone.persistent_groups("Cafe"), [])
        gone.forget("0", "Cafe")  # must not raise

    def test_ssid_matches(self):
        self.assertTrue(wpa.ssid_matches('"Cafe"', "Cafe"))
        self.assertTrue(wpa.ssid_matches("436166c3a9", "Café"))  # non-ASCII names come back as hex
        self.assertFalse(wpa.ssid_matches('"Cafe2"', "Cafe"))

    def test_no_supplicant(self):
        with self.assertRaises(SetupError):
            wpa.request("wlan9", "PING", timeout=0.5)
        self.assertFalse(wpa.ControlSocket("wlan9").remove_network("0"))  # teardown never raises


@unittest.skipUnless(wpa.jeepney_installed(), "needs the jeepney D-Bus library")
class DBusTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)
        self.refuse = set()
        self.bus = FakeBus(self.answer).install(self)
        self.sup = wpa.DBus("wlo1")

    def answer(self, path, member, body):
        if member in self.refuse:
            return "error", "fi.w1.wpa_supplicant1.UnknownError"
        return self.bus.default(path, member, body)

    def test_group_lifecycle(self):
        iface = "/fi/w1/wpa_supplicant1/Interfaces/0"
        network = self.sup.add_group_network("Café", "secret123", hidden=True)
        self.assertEqual(network, f"{iface}/PersistentGroups/0")
        self.sup.start_group(network, 2437)
        self.assertTrue(self.sup.remove_group("p2p-wlo1-0"))
        self.assertTrue(self.sup.remove_network(network))
        self.assertEqual(
            self.bus.members(),
            [
                "GetInterface",
                "AddPersistentGroup",
                "GetInterface",
                "GroupAdd",
                "GetInterface",
                "Disconnect",
                "GetInterface",
                "RemovePersistentGroup",
            ],
        )
        props = self.bus.calls[1][2][0]
        self.assertEqual(props["ssid"], ("ay", "Café".encode()))
        self.assertEqual(props["psk"], ("s", "secret123"))  # wpa_supplicant quotes it: a passphrase
        self.assertEqual(props["mode"], ("i", 3))
        self.assertEqual(props["ignore_broadcast_ssid"], ("i", 1))
        self.assertEqual(self.bus.calls[3][2][0], {"persistent_group_object": ("o", network), "frequency": ("i", 2437)})
        self.assertEqual(self.bus.calls[5][0], "/fi/w1/wpa_supplicant1/Interfaces/1")  # the group's own object

    def test_forget_removes_wpa_supplicants_copy(self):
        network = self.sup.add_group_network("Cafe", "secret123")
        self.sup.start_group(network, 2437)
        other = self.sup.add_group_network("Other", "secret123")
        self.assertEqual(len(self.bus.groups), 3)
        self.sup.forget(network, "Cafe")
        self.assertEqual(list(self.bus.groups), [other])

    def test_teardown_survives_refusals(self):
        self.refuse = {"RemovePersistentGroup", "Get"}
        self.assertFalse(self.sup.remove_network("/x"))
        self.assertEqual(self.sup.persistent_groups("Cafe"), [])
        self.sup.forget("/x", "Cafe")  # must not raise

    def test_unreachable_or_silent_bus(self):
        with mock.patch.object(wpa, "_connect", side_effect=OSError("no system bus")):
            with self.assertRaises(SetupError) as raised:
                self.sup.start_group("/x", 2437)
            self.assertIn("Can't reach the system D-Bus", raised.exception.message)
        silent = mock.Mock(send_and_get_reply=mock.Mock(side_effect=TimeoutError("timed out")))
        with mock.patch.object(wpa, "_connect", return_value=silent):
            with self.assertRaises(SetupError) as raised:
                self.sup.start_group("/x", 2437)
            self.assertIn("didn't answer GetInterface", raised.exception.message)
        silent.close.assert_called()  # the connection is closed either way

    def test_raw_key_goes_as_bytes(self):
        call = wpa.add_persistent_group_call("/i", "x", "ab" * 32, False)
        self.assertEqual(call[4][0]["psk"], ("ay", bytes.fromhex("ab" * 32)))

    def test_refusals(self):
        self.refuse = {"GroupAdd", "Disconnect"}
        with self.assertRaises(SetupError) as raised:
            self.sup.start_group("/x", 2437)
        self.assertIn("UnknownError", raised.exception.message)
        self.assertFalse(self.sup.remove_group("p2p-wlo1-0"))  # teardown never raises

    def test_messages_serialise(self):
        from jeepney import DBusAddress, new_method_call

        for path, interface, method, signature, body in (
            wpa.add_persistent_group_call("/fi/w1/wpa_supplicant1/Interfaces/1", "Café", "secret123", True),
            wpa.group_add_call("/a", "/a/PersistentGroups/0", 2437),
            wpa.disconnect_call("/a"),
            wpa.remove_persistent_group_call("/a", "/a/PersistentGroups/0"),
        ):
            address = DBusAddress(path, bus_name=wpa.BUS_NAME, interface=interface)
            self.assertTrue(new_method_call(address, method, signature, body).serialise(serial=1))


@unittest.skipUnless(wpa.jeepney_installed(), "needs the jeepney D-Bus library")
class DBusAvailableTests(unittest.TestCase):
    """The unprivileged check behind `apsta detect`: does wpa_supplicant own its D-Bus name?"""

    def check(self, owner=True, connect_error=None, call_error=None):
        from jeepney import new_method_return

        conn = mock.Mock()
        conn.send_and_get_reply.side_effect = call_error or (
            lambda msg, timeout=None: new_method_return(msg, "b", (owner,))
        )
        with mock.patch.object(wpa, "_connect", side_effect=connect_error, return_value=conn):
            result = wpa.dbus_available()
        if connect_error is None:
            conn.close.assert_called_once()
        return result

    def test_answers(self):
        self.assertTrue(self.check(owner=True))
        self.assertFalse(self.check(owner=False))  # wpa_supplicant not running (iwd, or stopped)
        self.assertFalse(self.check(connect_error=OSError("no bus")))
        self.assertFalse(self.check(call_error=TimeoutError()))
        with mock.patch.object(wpa, "jeepney_installed", return_value=False):
            self.assertFalse(wpa.dbus_available())


class ChoosingTests(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)

    def test_socket_first_then_dbus(self):
        self.assertIsNone(wpa.connect("wlo1"))
        FakeBus().install(self)
        self.assertEqual(wpa.connect("wlo1").kind, "dbus")
        FakeWpaSupplicant(paths.WPA_CTRL_DIR / "p2p-dev-wlo1").install(self)
        self.assertEqual(wpa.connect("wlo1").kind, "socket")
        with mock.patch.dict(os.environ, {"APSTA_WPA_BACKEND": "dbus"}):
            self.assertEqual(wpa.connect("wlo1").kind, "dbus")
        self.assertEqual(wpa.connect("wlo1", "dbus").kind, "dbus")  # stop uses what start used

    def test_missing_reason(self):
        with mock.patch.object(wpa, "jeepney_installed", return_value=False):
            self.assertIn("jeepney", wpa.missing_reason("wlo1"))
        with mock.patch.object(wpa, "jeepney_installed", return_value=True):
            self.assertIn("iwd", wpa.missing_reason("wlo1"))


if __name__ == "__main__":
    unittest.main()
