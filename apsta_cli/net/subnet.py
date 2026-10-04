"""Choose a private IPv4 subnet for the hotspot that doesn't collide with existing networks."""

from __future__ import annotations

import ipaddress
import re
from typing import Iterable, List

from ..core import shell
from ..core.errors import SetupError

# Ordered preferences. 192.168.42.0/24 is also Android's USB-tethering default,
# so collisions are real and must be checked rather than assumed away.
CANDIDATES = (
    "192.168.42.0/24",
    "10.42.42.0/24",
    "172.30.42.0/24",
    "192.168.199.0/24",
    "10.211.73.0/24",
)


def pick(in_use: Iterable[ipaddress.IPv4Network]) -> ipaddress.IPv4Network:
    used = list(in_use)
    for cidr in CANDIDATES:
        net = ipaddress.IPv4Network(cidr)
        if not any(net.overlaps(u) for u in used):
            return net
    raise SetupError("Every candidate hotspot subnet overlaps an existing network.")


def parse_networks(text: str) -> List[ipaddress.IPv4Network]:
    nets = []
    for cidr in re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b", text):
        try:
            net = ipaddress.IPv4Network(cidr, strict=False)
        except ValueError:
            continue
        if net.prefixlen > 0:  # a default route overlaps everything; ignore it
            nets.append(net)
    return nets


def networks_in_use() -> List[ipaddress.IPv4Network]:
    addrs = shell.out(["ip", "-4", "-o", "addr", "show"])
    routes = shell.out(["ip", "-4", "route", "show", "table", "all"])
    return parse_networks(addrs + "\n" + routes)


def addresses(net: ipaddress.IPv4Network):
    """(gateway, dhcp_start, dhcp_end) for a /24-or-larger network."""
    hosts = list(net.hosts())
    return str(hosts[0]), str(hosts[9]), str(hosts[min(199, len(hosts) - 1)])
