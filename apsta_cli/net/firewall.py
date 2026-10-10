"""NAT/forwarding for hostapd mode, via whichever firewall the system uses.

Each backend's ``apply`` returns the data its ``revert`` needs, which is
stored in the runtime state, so teardown removes exactly what was added and
nothing the user configured.

* firewalld: rules added with iptables/nft are overridden by firewalld's own
  drop policies, so use firewalld itself (runtime-only changes).
* iptables (legacy or nft shim): rules inserted at the top of each chain,
  tagged ``apsta``, so they come before ufw/docker policies.
* nftables: a private ``ip apsta`` table.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Tuple

from ..core import paths, shell
from ..core.errors import SetupError

logger = logging.getLogger(__name__)

COMMENT = "apsta"


# ── IP forwarding ─────────────────────────────────────────────────────────────


def enable_forwarding() -> str:
    """Turn on IPv4 forwarding and return the previous value so stop can restore it."""
    try:
        previous = paths.IP_FORWARD.read_text().strip()
        if previous != "1":
            paths.IP_FORWARD.write_text("1\n")
        return previous
    except OSError as exc:
        raise SetupError(f"Could not enable IPv4 forwarding: {exc}") from exc


def restore_forwarding(previous: str) -> None:
    if previous and previous != "1":
        try:
            paths.IP_FORWARD.write_text(previous + "\n")
        except OSError as exc:
            logger.warning("Could not restore ip_forward=%s: %s", previous, exc)


# ── Backends ──────────────────────────────────────────────────────────────────


class IptablesBackend:
    name = "iptables"

    @staticmethod
    def caveats(ap_iface: str) -> List[str]:
        return []

    @staticmethod
    def rules(ap_iface: str, subnet: str) -> List[Tuple[str, str, List[str]]]:
        tag = ["-m", "comment", "--comment", COMMENT]
        return [
            # No "-o <iface>": traffic may leave via ethernet, a VPN, or WiFi.
            ("nat", "POSTROUTING", ["-s", subnet, "!", "-d", subnet, *tag, "-j", "MASQUERADE"]),
            ("filter", "FORWARD", ["-i", ap_iface, *tag, "-j", "ACCEPT"]),
            (
                "filter",
                "FORWARD",
                ["-o", ap_iface, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", *tag, "-j", "ACCEPT"],
            ),
            # DHCP and DNS from clients, in case INPUT defaults to DROP (ufw).
            ("filter", "INPUT", ["-i", ap_iface, "-p", "udp", "--dport", "67", *tag, "-j", "ACCEPT"]),
            ("filter", "INPUT", ["-i", ap_iface, "-p", "udp", "--dport", "53", *tag, "-j", "ACCEPT"]),
            ("filter", "INPUT", ["-i", ap_iface, "-p", "tcp", "--dport", "53", *tag, "-j", "ACCEPT"]),
        ]

    def apply(self, ap_iface: str, subnet: str) -> Dict:
        applied = []
        try:
            for table, chain, spec in self.rules(ap_iface, subnet):
                shell.run(["iptables", "-w", "-t", table, "-I", chain, "1", *spec]).check(f"iptables {chain} rule")
                applied.append((table, chain, spec))
        except SetupError:
            for table, chain, spec in reversed(applied):
                shell.run(["iptables", "-w", "-t", table, "-D", chain, *spec])
            raise
        return {}

    def revert(self, ap_iface: str, subnet: str, data: Dict) -> None:
        for table, chain, spec in self.rules(ap_iface, subnet):
            shell.run(["iptables", "-w", "-t", table, "-D", chain, *spec])


class NftablesBackend:
    name = "nftables"
    TABLE = "apsta"

    @classmethod
    def ruleset(cls, ap_iface: str, subnet: str) -> str:
        return f"""table ip {cls.TABLE} {{
    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
        ip saddr {subnet} ip daddr != {subnet} masquerade
    }}
    chain forward {{
        type filter hook forward priority filter - 1; policy accept;
        iifname "{ap_iface}" accept
        oifname "{ap_iface}" ct state established,related accept
    }}
}}
"""

    def apply(self, ap_iface: str, subnet: str) -> Dict:
        shell.run(["nft", "-f", "-"], input=self.ruleset(ap_iface, subnet)).check("Loading nftables rules")
        return {}

    @staticmethod
    def caveats(ap_iface: str) -> List[str]:
        ruleset = shell.out(["nft", "list", "ruleset"])
        if re.search(r"hook forward[^\n]*\n?[^}]*policy drop", ruleset):
            return [
                "Another nftables forward chain has 'policy drop', so clients may get no internet: "
                f"allow forwarding from {ap_iface} in your nftables config."
            ]
        return []

    def revert(self, ap_iface: str, subnet: str, data: Dict) -> None:
        shell.run(["nft", "delete", "table", "ip", self.TABLE])


class FirewalldBackend:
    name = "firewalld"

    @staticmethod
    def caveats(ap_iface: str) -> List[str]:
        return []

    @staticmethod
    def _fw(*args: str) -> shell.Result:
        return shell.run(["firewall-cmd", *args])

    @staticmethod
    def uplink_interfaces() -> List[str]:
        text = shell.out(["ip", "-4", "route", "show", "default"])
        return sorted(set(re.findall(r"\bdev\s+(\S+)", text)))

    def apply(self, ap_iface: str, subnet: str) -> Dict:
        self._fw("--zone=trusted", f"--add-interface={ap_iface}").check("firewalld: trusting the hotspot interface")
        added: List[str] = []
        try:
            zones = set()
            for dev in self.uplink_interfaces():
                zone = shell.out(["firewall-cmd", f"--get-zone-of-interface={dev}"])
                zones.add(zone or shell.out(["firewall-cmd", "--get-default-zone"]))
            for zone in sorted(z for z in zones if z):
                if not self._fw(f"--zone={zone}", "--query-masquerade").ok:
                    self._fw(f"--zone={zone}", "--add-masquerade").check(f"firewalld: masquerade on {zone}")
                    added.append(zone)
        except SetupError:
            self.revert(ap_iface, subnet, {"masquerade_zones": added})
            raise
        return {"masquerade_zones": added}

    def revert(self, ap_iface: str, subnet: str, data: Dict) -> None:
        self._fw("--zone=trusted", f"--remove-interface={ap_iface}")
        for zone in data.get("masquerade_zones", []):
            self._fw(f"--zone={zone}", "--remove-masquerade")


BACKENDS = {b.name: b for b in (FirewalldBackend, IptablesBackend, NftablesBackend)}


def detect():
    if shell.have("firewall-cmd") and shell.run(["firewall-cmd", "--state"]).ok:
        return FirewalldBackend()
    if shell.have("iptables"):
        return IptablesBackend()
    if shell.have("nft"):
        return NftablesBackend()
    raise SetupError(
        "No firewall tool found to share the internet connection.",
        hints=["Install iptables or nftables."],
    )


def by_name(name: str):
    return BACKENDS[name]()


def apply(ap_iface: str, subnet: str) -> Dict:
    """Enable forwarding + NAT. Returns undo data for :func:`revert`."""
    backend = detect()
    previous = enable_forwarding()
    try:
        data = backend.apply(ap_iface, subnet)
    except SetupError:
        restore_forwarding(previous)
        raise
    return {"backend": backend.name, "ip_forward_prev": previous, "data": data}


def caveats(ap_iface: str, record: Dict) -> List[str]:
    """Problems outside apsta's rules that may stop clients reaching the internet, in words for the user."""
    return by_name(record["backend"]).caveats(ap_iface)


def revert(ap_iface: str, subnet: str, record: Dict) -> None:
    if not record:
        return
    by_name(record["backend"]).revert(ap_iface, subnet, record.get("data", {}))
    restore_forwarding(record.get("ip_forward_prev", ""))
