"""Wi-Fi Direct group owner (P2P-GO) through wpa_supplicant.

Cards that pin an AP to the WiFi connection's channel often let a P2P group
owner use a channel of its own: the radio switches between the two. Phones and
laptops join a group owner like any WPA2 network, so it works as a hotspot.
See docs/wifi-direct.md for the full picture.

NetworkManager already runs wpa_supplicant with a P2P device per WiFi
interface (``p2p-dev-<iface>``). apsta asks it for four things:

1. add a *persistent group* network block with the hotspot's name and password;
2. start the group from it on a given frequency;
3. later, remove the group (its ``p2p-<iface>-N`` interface goes away);
4. forget the network block, and the copy wpa_supplicant made of it.

About that copy: when a persistent group starts, wpa_supplicant stores its
own record of it (one per name and group owner address, reused on later
starts). It only lives in memory, since NetworkManager gives wpa_supplicant no
config file, but it holds the password, so :meth:`Supplicant.forget` removes
it along with apsta's own block.

There are two ways to reach wpa_supplicant, tried in this order:

* :class:`ControlSocket`: the per-interface control socket in
  ``/run/wpa_supplicant``. Exists when wpa_supplicant runs with
  ``-O /run/wpa_supplicant`` (Arch, Debian, Ubuntu). No dependencies.
* :class:`DBus`: wpa_supplicant's D-Bus API, which every distribution enables
  (``-u``) because NetworkManager uses it. Needs the pure-Python ``jeepney``
  library. Covers Fedora, openSUSE, Alpine and Void, which start
  wpa_supplicant without ``-O``.

Neither puts the password on a command line (``wpa_cli`` or ``busctl`` would).
"""

from __future__ import annotations

import itertools
import os
import socket
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core import fsutil, output, paths
from ..core.errors import SetupError

_counter = itertools.count()


def network_settings(ssid: str, password: str, hidden: bool) -> List[Tuple[str, str]]:
    """The persistent group's network block, as wpa_supplicant config key/values."""
    settings = [
        ("ssid", ssid.encode("utf-8").hex()),  # unquoted hex: any byte, no escaping
        ("psk", _psk(password)),
        ("key_mgmt", "WPA-PSK"),
        ("proto", "RSN"),
        ("pairwise", "CCMP"),
        ("mode", "3"),  # P2P group owner (0 would make it join the group as a client)
        ("disabled", "2"),  # a persistent group, not a network to join
    ]
    if hidden:
        settings.append(("ignore_broadcast_ssid", "1"))
    return settings


def ssid_matches(value: str, ssid: str) -> bool:
    """Does a network's ``ssid`` as wpa_supplicant reports it (quoted text, or hex) name ``ssid``?"""
    value = value.strip()
    return value == f'"{ssid}"' or value.lower() == ssid.encode("utf-8").hex()


def _is_raw_key(password: str) -> bool:
    return len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password)


def _psk(password: str) -> str:
    # A 64-hex-digit key goes in raw; a passphrase is quoted (wpa_supplicant
    # reads up to the *last* quote, so quotes inside it are fine).
    return password.lower() if _is_raw_key(password) else f'"{password}"'


class Supplicant(ABC):
    """One way of talking to wpa_supplicant about ``base_iface``'s P2P device.

    ``network`` values are opaque strings: a network id for the socket, an
    object path for D-Bus. They are stored in the hotspot state so ``stop``
    can clean up with the same backend.
    """

    kind = ""

    def __init__(self, base_iface: str):
        self.base = base_iface

    @abstractmethod
    def add_group_network(self, ssid: str, password: str, hidden: bool = False) -> str:
        """Create the persistent group's network block; returns its handle."""

    @abstractmethod
    def start_group(self, network: str, freq: int) -> None:
        """Start the group (wpa_supplicant creates a ``p2p-<base>-N`` interface)."""

    @abstractmethod
    def remove_group(self, group_iface: str) -> bool:
        """Stop the group on ``group_iface``. Never raises: used during teardown."""

    @abstractmethod
    def remove_network(self, network: str) -> bool:
        """Forget the network block. Never raises: used during teardown."""

    @abstractmethod
    def persistent_groups(self, ssid: str) -> List[str]:
        """Persistent group networks named ``ssid`` (apsta's and wpa_supplicant's copy). Never raises."""

    def forget(self, network: str, ssid: str) -> None:
        """Remove apsta's network block and every persistent copy of the group."""
        self.remove_network(network)
        for other in self.persistent_groups(ssid):
            if other != network:
                self.remove_network(other)


# ── control socket ────────────────────────────────────────────────────────────


def device_socket(base_iface: str) -> Path:
    return paths.WPA_CTRL_DIR / f"p2p-dev-{base_iface}"


def socket_available(base_iface: str) -> bool:
    try:
        return device_socket(base_iface).is_socket()
    except PermissionError:
        # The directory is root-only; unprivileged callers (``apsta detect``)
        # can't look inside. It only exists when wpa_supplicant runs with -O.
        return paths.WPA_CTRL_DIR.is_dir()
    except OSError:
        return False


def request(base_iface: str, command: str, timeout: float = 10.0) -> str:
    """Send one command to the P2P device's control socket and return the reply.

    The protocol is datagrams over a Unix socket: the client binds its own
    socket (wpa_supplicant replies to that address) and sends the command as
    plain text, e.g. ``ADD_NETWORK``; the reply is ``OK``, ``FAIL`` or data.
    """
    paths.ensure_run_dir()
    local = paths.RUN_DIR / f"wpa-ctrl-{os.getpid()}-{next(_counter)}"
    verb = command.split(" ", 1)[0]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        fsutil.remove(local)
        sock.bind(str(local))
        sock.settimeout(timeout)
        sock.connect(str(device_socket(base_iface)))
        sock.send(command.encode("utf-8"))
        while True:
            reply = sock.recv(4096).decode("utf-8", errors="replace")
            if not reply.startswith("<"):  # "<3>..." lines are unsolicited events
                break
    except OSError as exc:
        raise SetupError(f"wpa_supplicant didn't answer {verb}: {exc}") from exc
    finally:
        sock.close()
        fsutil.remove(local)
    reply = reply.strip()
    output.dbg("wpa_supplicant", command=verb, reply=reply[:40])  # never the arguments: one is the password
    return reply


class ControlSocket(Supplicant):
    kind = "socket"

    def _expect_ok(self, command: str, what: str) -> None:
        reply = request(self.base, command)
        if reply != "OK":
            raise SetupError(f"wpa_supplicant refused {what} ({reply or 'no reply'}).")

    def add_group_network(self, ssid: str, password: str, hidden: bool = False) -> str:
        reply = request(self.base, "ADD_NETWORK")
        if not reply.isdigit():
            raise SetupError(f"wpa_supplicant refused to add a network ({reply or 'no reply'}).")
        try:
            for key, value in network_settings(ssid, password, hidden):
                self._expect_ok(f"SET_NETWORK {reply} {key} {value}", f"the {key} setting")
        except SetupError:
            self.remove_network(reply)
            raise
        return reply

    def start_group(self, network: str, freq: int) -> None:
        self._expect_ok(f"P2P_GROUP_ADD persistent={network} freq={freq}", "to start the Wi-Fi Direct group")

    def remove_group(self, group_iface: str) -> bool:
        try:
            return request(self.base, f"P2P_GROUP_REMOVE {group_iface}") == "OK"
        except SetupError:
            return False

    def remove_network(self, network: str) -> bool:
        try:
            return request(self.base, f"REMOVE_NETWORK {network}") == "OK"
        except SetupError:
            return False

    def persistent_groups(self, ssid: str) -> List[str]:
        # LIST_NETWORKS: a header line, then "id<TAB>ssid<TAB>bssid<TAB>flags".
        # Its ssid column is escaped for display, so ask for each one exactly.
        try:
            listing = request(self.base, "LIST_NETWORKS")
            found = []
            for line in listing.splitlines()[1:]:
                fields = line.split("\t")
                persistent = len(fields) >= 4 and fields[0].isdigit() and "[P2P-PERSISTENT]" in fields[3]
                if persistent and ssid_matches(request(self.base, f"GET_NETWORK {fields[0]} ssid"), ssid):
                    found.append(fields[0])
            return found
        except SetupError:
            return []


# ── D-Bus ─────────────────────────────────────────────────────────────────────

BUS_NAME = "fi.w1.wpa_supplicant1"
ROOT_PATH = "/fi/w1/wpa_supplicant1"
P2P_INTERFACE = "fi.w1.wpa_supplicant1.Interface.P2PDevice"

# A D-Bus call, before it's turned into a message: (object path, interface,
# method, signature, body). Built by pure functions so tests don't need a bus.
Call = Tuple[str, str, str, str, tuple]


def get_interface_call(ifname: str) -> Call:
    return (ROOT_PATH, BUS_NAME, "GetInterface", "s", (ifname,))


def add_persistent_group_call(iface_path: str, ssid: str, password: str, hidden: bool) -> Call:
    """``P2PDevice.AddPersistentGroup``: wpa_supplicant sets ``disabled=2`` itself.

    Each value is a D-Bus variant (signature, value). wpa_supplicant turns byte
    arrays into hex and quotes strings except for keys such as ``key_mgmt``,
    which matches the control socket's settings above.
    """
    props: Dict[str, Tuple[str, object]] = {
        "ssid": ("ay", ssid.encode("utf-8")),
        "psk": ("ay", bytes.fromhex(password)) if _is_raw_key(password) else ("s", password),
        "key_mgmt": ("s", "WPA-PSK"),
        "proto": ("s", "RSN"),
        "pairwise": ("s", "CCMP"),
        "mode": ("i", 3),
    }
    if hidden:
        props["ignore_broadcast_ssid"] = ("i", 1)
    return (iface_path, P2P_INTERFACE, "AddPersistentGroup", "a{sv}", (props,))


def group_add_call(iface_path: str, group_path: str, freq: int) -> Call:
    props = {"persistent_group_object": ("o", group_path), "frequency": ("i", freq)}
    return (iface_path, P2P_INTERFACE, "GroupAdd", "a{sv}", (props,))


def disconnect_call(group_iface_path: str) -> Call:
    # Called on the *group's* interface object, this removes the group.
    return (group_iface_path, P2P_INTERFACE, "Disconnect", "", ())


def remove_persistent_group_call(iface_path: str, group_path: str) -> Call:
    return (iface_path, P2P_INTERFACE, "RemovePersistentGroup", "o", (group_path,))


PROPERTIES = "org.freedesktop.DBus.Properties"
PERSISTENT_GROUP_INTERFACE = "fi.w1.wpa_supplicant1.PersistentGroup"


def persistent_groups_call(iface_path: str) -> Call:
    return (iface_path, PROPERTIES, "Get", "ss", (P2P_INTERFACE, "PersistentGroups"))


def group_properties_call(group_path: str) -> Call:
    # The network's settings as strings, the same text the control socket returns.
    return (group_path, PROPERTIES, "Get", "ss", (PERSISTENT_GROUP_INTERFACE, "Properties"))


def jeepney_installed() -> bool:
    try:
        import jeepney  # noqa: F401
    except ImportError:
        return False
    return True


def _connect():
    from jeepney.io.blocking import open_dbus_connection

    return open_dbus_connection(bus="SYSTEM")


def dbus_available() -> bool:
    """jeepney is installed and wpa_supplicant owns its D-Bus name (unprivileged check)."""
    if not jeepney_installed():
        return False
    from jeepney.bus_messages import message_bus
    from jeepney.wrappers import unwrap_msg

    try:
        conn = _connect()
    except (OSError, ValueError):
        return False
    try:
        return bool(unwrap_msg(conn.send_and_get_reply(message_bus.NameHasOwner(BUS_NAME), timeout=5))[0])
    except Exception:  # noqa: BLE001 - any bus trouble means "not available"
        return False
    finally:
        conn.close()


class DBus(Supplicant):
    kind = "dbus"

    def call(self, call: Call, what: str) -> tuple:
        from jeepney import DBusAddress, new_method_call
        from jeepney.wrappers import DBusErrorResponse, unwrap_msg

        path, interface, method, signature, body = call
        address = DBusAddress(path, bus_name=BUS_NAME, interface=interface)
        try:
            conn = _connect()
        except (OSError, ValueError) as exc:
            raise SetupError(f"Can't reach the system D-Bus for {what}: {exc}") from exc
        try:
            reply = unwrap_msg(conn.send_and_get_reply(new_method_call(address, method, signature, body), timeout=10))
        except DBusErrorResponse as exc:
            raise SetupError(f"wpa_supplicant refused {what} ({exc.name}: {' '.join(map(str, exc.data))}).") from exc
        except (OSError, TimeoutError) as exc:
            raise SetupError(f"wpa_supplicant didn't answer {method}: {exc}") from exc
        finally:
            conn.close()
        output.dbg("wpa_supplicant (D-Bus)", method=method)
        return reply

    def _iface_path(self, ifname: str) -> str:
        return self.call(get_interface_call(ifname), f"to look up {ifname}")[0]

    def add_group_network(self, ssid: str, password: str, hidden: bool = False) -> str:
        iface = self._iface_path(self.base)
        return self.call(add_persistent_group_call(iface, ssid, password, hidden), "the Wi-Fi Direct network")[0]

    def start_group(self, network: str, freq: int) -> None:
        self.call(group_add_call(self._iface_path(self.base), network, freq), "to start the Wi-Fi Direct group")

    def remove_group(self, group_iface: str) -> bool:
        try:
            self.call(disconnect_call(self._iface_path(group_iface)), "to stop the Wi-Fi Direct group")
        except SetupError:
            return False
        return True

    def remove_network(self, network: str) -> bool:
        try:
            self.call(remove_persistent_group_call(self._iface_path(self.base), network), "to forget the network")
        except SetupError:
            return False
        return True

    def persistent_groups(self, ssid: str) -> List[str]:
        # Properties.Get returns a variant: (signature, value).
        try:
            groups = self.call(persistent_groups_call(self._iface_path(self.base)), "the persistent groups")[0][1]
            found = []
            for path in groups:
                props = self.call(group_properties_call(path), "a persistent group")[0][1]
                value = props.get("ssid", ("s", ""))[1]
                if isinstance(value, str) and ssid_matches(value, ssid):
                    found.append(path)
            return found
        except (SetupError, IndexError, TypeError, AttributeError):
            return []


# ── choosing a backend ────────────────────────────────────────────────────────

BACKENDS = {"socket": ControlSocket, "dbus": DBus}


def connect(base_iface: str, kind: Optional[str] = None) -> Optional[Supplicant]:
    """The way to reach ``base_iface``'s P2P device: ``kind`` if given, else the best available.

    ``APSTA_WPA_BACKEND=socket|dbus`` forces one, to test a backend on a
    machine where the other would win.
    """
    kind = kind or os.environ.get("APSTA_WPA_BACKEND")
    if kind in BACKENDS:
        return BACKENDS[kind](base_iface)
    if socket_available(base_iface):
        return ControlSocket(base_iface)
    if dbus_available():
        return DBus(base_iface)
    return None


def missing_reason(base_iface: str) -> str:
    """Why :func:`connect` found nothing, phrased for the user."""
    if not jeepney_installed():
        return (
            f"wpa_supplicant has no control socket for p2p-dev-{base_iface}, and talking to it "
            "over D-Bus needs the Python jeepney package (python3-jeepney / python-jeepney)"
        )
    return "wpa_supplicant isn't running (NetworkManager may be using iwd, which has no Wi-Fi Direct support)"
