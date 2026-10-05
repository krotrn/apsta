"""Hotspot clients: who is connected, kicking/blocking, and per-client rate limits."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from ..core import paths, shell
from ..core.errors import SetupError, UsageError
from ..state import HotspotState
from . import dnsmasq, hostapd

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
PREF_BASE = 49152  # tc filter priorities owned by apsta start here


@dataclass
class Client:
    mac: str
    ip: str = ""
    hostname: str = ""
    limit_kbps: Optional[int] = None
    blocked: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def parse_station_dump(text: str) -> List[str]:
    return [m.lower() for m in re.findall(r"^Station\s+([0-9a-fA-F:]{17})", text, re.MULTILINE)]


def parse_neigh(text: str) -> Dict[str, str]:
    """mac -> ip from ``ip -4 neigh show dev <iface>``."""
    table = {}
    for line in text.splitlines():
        match = re.match(r"^(\S+)\s.*lladdr\s+([0-9a-fA-F:]{17})", line)
        if match:
            table[match.group(2).lower()] = match.group(1)
    return table


def _stations(state: HotspotState) -> List[str]:
    if state.method == "hostapd":
        macs = hostapd.stations(state.ap_interface)
        if macs is not None:
            return macs
    return parse_station_dump(shell.out(["iw", "dev", state.ap_interface, "station", "dump"]))


def _leases() -> Dict[str, Dict[str, str]]:
    try:
        text = paths.DNSMASQ_LEASES.read_text(encoding="utf-8")
    except OSError:
        return {}
    return {lease["mac"]: lease for lease in dnsmasq.parse_leases(text)}


def list_clients(state: HotspotState) -> List[Client]:
    """Currently associated stations, enriched with DHCP lease / neighbour info.

    Leases alone aren't used for this: they outlive disconnects by hours and
    miss clients with static IPs.
    """
    leases = _leases()
    neigh = parse_neigh(shell.out(["ip", "-4", "neigh", "show", "dev", state.ap_interface]))
    clients = []
    for mac in _stations(state):
        lease = leases.get(mac, {})
        clients.append(
            Client(
                mac=mac,
                ip=lease.get("ip") or neigh.get(mac, ""),
                hostname=lease.get("hostname", ""),
                limit_kbps=state.client_limits.get(mac, {}).get("kbps"),
                blocked=mac in state.blocked,
            )
        )
    return clients


def resolve(clients: List[Client], identifier: str) -> Client:
    """Find a client by exact MAC, then exact IP, then a *unique* hostname.

    Hostnames are client-chosen, so they're matched last and only when they
    identify exactly one client.
    """
    needle = identifier.strip().lower()
    if not needle:
        raise UsageError("Client identifier cannot be empty.")
    for attr in ("mac", "ip"):
        for client in clients:
            if getattr(client, attr).lower() == needle:
                return client
    named = [c for c in clients if c.hostname and c.hostname.lower() == needle]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise UsageError(f"Several clients are named '{identifier}'; use the MAC address instead.")
    if _MAC_RE.match(needle):
        return Client(mac=needle)  # e.g. unblocking a client that is no longer connected
    raise UsageError(f"No connected client matches '{identifier}'.", hints=["List clients with: apsta clients"])


def disconnect(state: HotspotState, mac: str, block: bool) -> None:
    if state.method == "hostapd":
        if block:
            if not hostapd.deny(state.ap_interface, mac):
                raise SetupError(f"hostapd refused to block {mac}.")
            if mac not in state.blocked:
                state.blocked.append(mac)
        if hostapd.deauthenticate(state.ap_interface, mac) or block:
            return
    elif block:
        raise UsageError(
            "Blocking clients needs hostapd mode.", hints=["Start with: sudo apsta start --method hostapd"]
        )
    shell.run(["iw", "dev", state.ap_interface, "station", "del", mac]).check(f"Disconnecting {mac}")


def unblock(state: HotspotState, mac: str) -> None:
    if state.method != "hostapd":
        raise UsageError("Blocking clients needs hostapd mode.")
    hostapd.allow(state.ap_interface, mac, allowlisted=mac in state.allowed_macs)
    if mac in state.blocked:
        state.blocked.remove(mac)


def allocate_pref(limits: Dict[str, Dict[str, int]], mac: str) -> int:
    """A tc filter priority unique to this MAC for the hotspot's lifetime.

    (apsta <= 0.6 derived it from a hash of the MAC, so two clients could
    collide and silently overwrite each other's limits.)
    """
    if mac in limits:
        return limits[mac]["pref"]
    used = {entry["pref"] for entry in limits.values()}
    pref = PREF_BASE
    while pref in used:
        pref += 1
    return pref


def _tc_filters(ap_iface: str, pref: int, mac: str, kbps: int):
    police = ["action", "police", "rate", f"{kbps}kbit", "burst", "64k", "conform-exceed", "drop"]
    # ingress on the AP = client upload; egress = client download.
    return [
        [
            "tc",
            "filter",
            "add",
            "dev",
            ap_iface,
            "ingress",
            "pref",
            str(pref),
            "protocol",
            "all",
            "flower",
            "src_mac",
            mac,
            *police,
        ],
        [
            "tc",
            "filter",
            "add",
            "dev",
            ap_iface,
            "egress",
            "pref",
            str(pref),
            "protocol",
            "all",
            "flower",
            "dst_mac",
            mac,
            *police,
        ],
    ]


def _delete_filters(ap_iface: str, pref: int) -> None:
    for direction in ("ingress", "egress"):
        shell.run(["tc", "filter", "del", "dev", ap_iface, direction, "pref", str(pref)])


def set_limit(state: HotspotState, mac: str, kbps: int) -> None:
    if kbps <= 0:
        raise UsageError("The limit must be a positive number of Kbps.")
    pref = allocate_pref(state.client_limits, mac)
    qdisc = shell.run(["tc", "qdisc", "add", "dev", state.ap_interface, "clsact"])
    if not qdisc.ok and "exists" not in qdisc.message().lower():
        raise SetupError(f"Could not attach a clsact qdisc: {qdisc.message()}")
    _delete_filters(state.ap_interface, pref)
    for argv in _tc_filters(state.ap_interface, pref, mac, kbps):
        result = shell.run(argv)
        if not result.ok:
            _delete_filters(state.ap_interface, pref)
            raise SetupError(
                f"Could not apply the limit: {result.message()}",
                hints=["Needs tc flower + police support in the kernel (cls_flower, act_police)."],
            )
    state.client_limits[mac] = {"pref": pref, "kbps": kbps}


def clear_limit(state: HotspotState, mac: str) -> bool:
    entry = state.client_limits.pop(mac, None)
    if entry is None:
        return False
    _delete_filters(state.ap_interface, entry["pref"])
    return True
