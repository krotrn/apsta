"""Wi-Fi Direct group owner (P2P-GO) through wpa_supplicant's control socket.

Cards that pin an AP to the WiFi connection's channel often let a P2P group
owner use a channel of its own: the radio switches between the two. Phones and
laptops join a group owner like any WPA2 network, so it works as a hotspot.

NetworkManager already runs wpa_supplicant with a P2P device per WiFi
interface (``p2p-dev-<iface>``); apsta adds a persistent group network block
with the hotspot's name and password and starts the group from it. The socket
is spoken to directly instead of through ``wpa_cli`` so the password never
appears on a command line.
"""

from __future__ import annotations

import itertools
import os
import socket
from pathlib import Path
from typing import List, Tuple

from ..core import fsutil, output, paths
from ..core.errors import SetupError

_counter = itertools.count()


def device_socket(base_iface: str) -> Path:
    return paths.WPA_CTRL_DIR / f"p2p-dev-{base_iface}"


def available(base_iface: str) -> bool:
    try:
        return device_socket(base_iface).is_socket()
    except OSError:
        return False


def request(base_iface: str, command: str, timeout: float = 10.0) -> str:
    """Send one command to the P2P device's control socket and return the reply."""
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
    output.dbg("wpa_supplicant", command=verb, reply=reply[:40])
    return reply


def _expect_ok(base_iface: str, command: str, what: str) -> None:
    reply = request(base_iface, command)
    if reply != "OK":
        raise SetupError(f"wpa_supplicant refused {what} ({reply or 'no reply'}).")


def _psk(password: str) -> str:
    # A 64-hex-digit key goes in raw; a passphrase is quoted (wpa_supplicant
    # reads up to the *last* quote, so quotes inside it are fine).
    if len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password):
        return password.lower()
    return f'"{password}"'


def network_settings(ssid: str, password: str, hidden: bool) -> List[Tuple[str, str]]:
    settings = [
        ("ssid", ssid.encode("utf-8").hex()),  # unquoted hex: any byte, no escaping
        ("psk", _psk(password)),
        ("key_mgmt", "WPA-PSK"),
        ("proto", "RSN"),
        ("pairwise", "CCMP"),
        ("mode", "3"),  # P2P group owner
        ("disabled", "2"),  # a persistent group, not a network to join
    ]
    if hidden:
        settings.append(("ignore_broadcast_ssid", "1"))
    return settings


def add_group_network(base_iface: str, ssid: str, password: str, hidden: bool = False) -> int:
    """Create the persistent group's network block; returns its id."""
    reply = request(base_iface, "ADD_NETWORK")
    if not reply.isdigit():
        raise SetupError(f"wpa_supplicant refused to add a network ({reply or 'no reply'}).")
    net_id = int(reply)
    try:
        for key, value in network_settings(ssid, password, hidden):
            _expect_ok(base_iface, f"SET_NETWORK {net_id} {key} {value}", f"the {key} setting")
    except SetupError:
        remove_network(base_iface, net_id)
        raise
    return net_id


def start_group(base_iface: str, net_id: int, freq: int) -> None:
    _expect_ok(base_iface, f"P2P_GROUP_ADD persistent={net_id} freq={freq}", "to start the Wi-Fi Direct group")


def remove_group(base_iface: str, group_iface: str) -> bool:
    try:
        return request(base_iface, f"P2P_GROUP_REMOVE {group_iface}") == "OK"
    except SetupError:
        return False


def remove_network(base_iface: str, net_id: int) -> bool:
    try:
        return request(base_iface, f"REMOVE_NETWORK {net_id}") == "OK"
    except SetupError:
        return False
