"""dnsmasq configuration for DHCP + DNS forwarding on the hotspot interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class DnsmasqConfig:
    interface: str
    gateway: str
    dhcp_start: str
    dhcp_end: str
    leases_file: str


def render(cfg: DnsmasqConfig) -> str:
    # DNS is forwarded to the host's own resolvers (/etc/resolv.conf, including
    # systemd-resolved's stub). Hard-coding public DNS broke captive portals,
    # corporate networks that block outside DNS, and internal hostnames.
    return (
        "\n".join(
            [
                f"interface={cfg.interface}",
                "bind-interfaces",
                f"listen-address={cfg.gateway}",
                "except-interface=lo",
                f"dhcp-range={cfg.dhcp_start},{cfg.dhcp_end},255.255.255.0,12h",
                f"dhcp-option=option:router,{cfg.gateway}",
                f"dhcp-option=option:dns-server,{cfg.gateway}",
                f"dhcp-leasefile={cfg.leases_file}",
                "dhcp-authoritative",
                "domain-needed",
                "bogus-priv",
            ]
        )
        + "\n"
    )


def parse_leases(text: str) -> List[Dict[str, str]]:
    """Parse a dnsmasq lease file: ``<expiry> <mac> <ip> <hostname> <client-id>``."""
    leases = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        leases.append({"mac": parts[1].lower(), "ip": parts[2], "hostname": "" if parts[3] == "*" else parts[3]})
    return leases
