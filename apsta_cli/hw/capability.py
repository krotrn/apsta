"""What a given WiFi interface's radio can do."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from ..core import paths, shell
from . import combinations as combos
from .interfaces import phy_of


@dataclass
class HardwareCapability:
    interface: str
    phy: Optional[str]
    supports_ap: bool
    supports_sta: bool
    ap_sta: bool  # AP and STA can run at the same time (on virtual interfaces)
    same_channel_required: bool  # ...but the AP has to use the STA's channel
    max_channels: int
    supported_modes: List[str] = field(default_factory=list)
    combinations: List[str] = field(default_factory=list)
    driver: str = ""
    chipset: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def from_iw_text(iface: str, phy: Optional[str], iw_text: str) -> HardwareCapability:
    """Build a capability report from ``iw phy info`` text (pure, testable)."""
    modes = combos.parse_supported_modes(iw_text)
    parsed = combos.parse_combinations(iw_text)
    support = combos.evaluate(parsed)
    return HardwareCapability(
        interface=iface,
        phy=phy,
        supports_ap="AP" in modes,
        supports_sta="managed" in modes,
        ap_sta=support.supported and "AP" in modes,
        same_channel_required=support.same_channel_required,
        max_channels=support.combination.channels if support.combination else 1,
        supported_modes=modes,
        combinations=[c.raw for c in parsed],
    )


def _driver(iface: str) -> str:
    link = str(paths.SYSFS_NET / iface / "device" / "driver")
    return os.path.basename(os.readlink(link)) if os.path.islink(link) else ""


def _chipset(iface: str) -> str:
    """Human-readable chipset name for *this* interface's device (PCI or USB)."""
    device = paths.SYSFS_NET / iface / "device"
    try:
        real = device.resolve(strict=True)
    except OSError:
        return ""
    if (real / "vendor").exists() and (real / "class").exists():  # PCI
        line = shell.out(["lspci", "-s", real.name])
        return line.split(": ", 1)[1] if ": " in line else line
    for candidate in (real, real.parent):  # USB interface -> USB device
        try:
            vid = (candidate / "idVendor").read_text().strip()
            pid = (candidate / "idProduct").read_text().strip()
        except OSError:
            continue
        line = shell.out(["lsusb", "-d", f"{vid}:{pid}"])
        return line.split(f"{vid}:{pid}", 1)[-1].strip() if line else f"USB {vid}:{pid}"
    return ""


def probe(iface: str) -> HardwareCapability:
    phy = phy_of(iface)
    # Scope to this phy so a second radio (USB dongle) can't leak its capabilities.
    iw_text = (phy and shell.out(["iw", "phy", phy, "info"])) or shell.out(["iw", "list"])
    cap = from_iw_text(iface, phy, iw_text)
    cap.driver = _driver(iface)
    cap.chipset = _chipset(iface)
    return cap
