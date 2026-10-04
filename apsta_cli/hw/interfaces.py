"""WiFi interfaces, their phys, live link state and the regulatory domain."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import List, Optional

from ..core import paths, shell


@dataclass
class WifiInterface:
    name: str
    mac: str
    phy: Optional[str]
    iftype: str
    state: str  # UP / DOWN
    connected_ssid: Optional[str]


def to_json(iface: WifiInterface) -> dict:
    """The interface shape used by every ``--json`` output."""
    return {
        "name": iface.name,
        "mac": iface.mac,
        "phy": iface.phy,
        "type": iface.iftype,
        "state": iface.state,
        "connected_ssid": iface.connected_ssid,
    }


@dataclass
class StaLink:
    ssid: Optional[str]
    freq: int


def _operstate(name: str) -> str:
    try:
        return "UP" if (paths.SYSFS_NET / name / "operstate").read_text().strip() == "up" else "DOWN"
    except OSError:
        return "DOWN"


def parse_iw_dev(text: str) -> List[WifiInterface]:
    """Parse ``iw dev`` output into interfaces (without link state)."""
    ifaces: List[WifiInterface] = []
    phy: Optional[str] = None
    current: Optional[WifiInterface] = None
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^phy#\d+$", stripped):
            phy = "phy" + stripped[4:]
        elif stripped.startswith("Interface "):
            current = WifiInterface(stripped.split()[1], "unknown", phy, "unknown", "DOWN", None)
            ifaces.append(current)
        elif current is not None:
            key, _, value = stripped.partition(" ")
            if key == "addr":
                current.mac = value
            elif key == "type":
                current.iftype = value
            elif key == "ssid":
                current.connected_ssid = value
    return ifaces


def list_wifi_interfaces() -> List[WifiInterface]:
    ifaces = parse_iw_dev(shell.out(["iw", "dev"]))
    for iface in ifaces:
        iface.state = _operstate(iface.name)
        if iface.iftype == "managed":
            link = sta_link(iface.name)
            iface.connected_ssid = link.ssid if link else None
        # Prefer the stable phy name from sysfs (iw dev prints phy#N).
        iface.phy = phy_of(iface.name) or iface.phy
    return ifaces


def client_interfaces() -> List[WifiInterface]:
    """Interfaces that could act as the STA/base for a hotspot (excludes AP vifs)."""
    return [i for i in list_wifi_interfaces() if i.iftype in ("managed", "unknown")]


def phy_of(iface: str) -> Optional[str]:
    try:
        return (paths.SYSFS_NET / iface / "phy80211" / "name").read_text().strip() or None
    except OSError:
        return None


def parse_link(text: str) -> Optional[StaLink]:
    if not text.startswith("Connected"):
        return None
    freq = re.search(r"freq:\s*(\d+)", text)  # iw >= 6 prints "freq: 5180.0"
    ssid = re.search(r"SSID:\s*(.+)", text)
    if not freq:
        return None
    return StaLink(ssid.group(1).strip() if ssid else None, int(freq.group(1)))


def sta_link(iface: str) -> Optional[StaLink]:
    return parse_link(shell.out(["iw", "dev", iface, "link"]))


def wait_for_sta(iface: str, timeout: float, poll: float = 1.0) -> Optional[StaLink]:
    """Wait up to ``timeout`` seconds for the STA to associate."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        link = sta_link(iface)
        if link or time.monotonic() >= deadline:
            return link
        time.sleep(poll)


def iface_type(iface: str) -> Optional[str]:
    match = re.search(r"^\s*type\s+(.+)$", shell.out(["iw", "dev", iface, "info"]), re.MULTILINE)
    return match.group(1).strip() if match else None


def parse_reg_country(text: str) -> Optional[str]:
    """Country of the global regulatory domain from ``iw reg get``, or None for world (00)."""
    match = re.search(r"^country\s+([A-Z0-9]{2}):", text, re.MULTILINE)
    if not match or match.group(1) in ("00", "99"):
        return None
    return match.group(1)


def reg_country() -> Optional[str]:
    return parse_reg_country(shell.out(["iw", "reg", "get"]))
