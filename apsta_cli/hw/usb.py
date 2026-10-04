"""USB WiFi adapters: a curated chipset database and a sysfs scanner.

To add a chipset, append a :class:`UsbChipset` to ``USB_CHIPSET_DB`` with the
USB vendor/product IDs of adapters known to use it. Only chipsets with
in-kernel drivers that support AP+STA belong here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from ..core import shell

# ── USB WiFi chipset database ──────────────────────────────────────────────────


@dataclass
class UsbChipset:
    chipset: str
    driver: str
    ap_sta: bool
    min_kernel: str
    wifi_gen: str
    vid_pids: List[Tuple[str, str]]
    buy_search: str
    notes: str


USB_CHIPSET_DB: List[UsbChipset] = [
    UsbChipset(
        chipset="mt7921au",
        driver="mt7921u",
        ap_sta=True,
        min_kernel="5.19",
        wifi_gen="WiFi 6",
        vid_pids=[
            ("0e8d", "7961"),
            ("3574", "6211"),
            ("13b1", "0045"),
            ("0846", "9060"),
            ("2357", "0138"),
        ],
        buy_search="mt7921au USB WiFi Linux",
        notes=(
            "Best overall choice. Avoid adapters with Bluetooth enabled (causes USB3 interference). "
            "Kernel 6.6+ recommended."
        ),
    ),
    UsbChipset(
        chipset="mt7925u",
        driver="mt7925u",
        ap_sta=True,
        min_kernel="6.7",
        wifi_gen="WiFi 7",
        vid_pids=[
            ("0846", "9100"),
        ],
        buy_search="mt7925u USB WiFi 7 Linux",
        notes="WiFi 7. Requires kernel 6.7+. Limited adapter availability as of 2025.",
    ),
    UsbChipset(
        chipset="mt7612u",
        driver="mt76x2u",
        ap_sta=True,
        min_kernel="4.19",
        wifi_gen="WiFi 5",
        vid_pids=[
            ("0e8d", "7612"),
            ("7392", "b711"),
            ("2357", "0103"),
            ("0b05", "17d1"),
            ("0846", "9053"),
        ],
        buy_search="mt7612u USB WiFi Linux",
        notes="Mature, rock-solid driver. AC1200 dual-band. Plug and play on almost any Linux distro.",
    ),
    UsbChipset(
        chipset="mt7610u",
        driver="mt76x0u",
        ap_sta=True,
        min_kernel="4.19",
        wifi_gen="WiFi 5",
        vid_pids=[
            ("0e8d", "7610"),
            ("7392", "a711"),
            ("2357", "0105"),
        ],
        buy_search="mt7610u USB WiFi Linux",
        notes="AC600 single-band 5GHz. Very stable. Good for hotspot-only dongle use.",
    ),
]


@dataclass
class UsbWifiDevice:
    vid: str
    pid: str
    name: str
    interface: Optional[str]
    driver: Optional[str]
    chipset_db: Optional[UsbChipset]


def scan_usb_wifi() -> List[UsbWifiDevice]:
    lsusb_names: dict = {}
    lsusb_out = shell.out(["lsusb"])
    for line in lsusb_out.splitlines():
        m = re.match(r"Bus (\d+) Device (\d+): ID [0-9a-f]{4}:[0-9a-f]{4}\s+(.*)", line, re.IGNORECASE)
        if m:
            key = (m.group(1).zfill(3), m.group(2).zfill(3))
            lsusb_names[key] = m.group(3).strip()

    usb_root = Path("/sys/bus/usb/devices")
    if not usb_root.exists():
        return []

    devices: List[UsbWifiDevice] = []

    for dev_path in sorted(usb_root.iterdir()):
        vid_file = dev_path / "idVendor"
        pid_file = dev_path / "idProduct"
        if not (vid_file.exists() and pid_file.exists()):
            continue
        try:
            vid = vid_file.read_text().strip().lower()
            pid = pid_file.read_text().strip().lower()
        except OSError:
            continue

        matched_chipset = None
        for cs in USB_CHIPSET_DB:
            if (vid, pid) in cs.vid_pids:
                matched_chipset = cs
                break

        name = ""
        try:
            busnum = (dev_path / "busnum").read_text().strip().zfill(3)
            devnum = (dev_path / "devnum").read_text().strip().zfill(3)
            name = lsusb_names.get((busnum, devnum), "")
        except OSError:
            pass  # device vanished or sysfs entry unreadable; keep the name empty

        if not matched_chipset:
            wifi_keywords = ("wireless", "wlan", "wifi", "802.11", "wi-fi", "mediatek", "ralink")
            if not any(k in name.lower() for k in wifi_keywords):
                continue

        iface, driver = _find_usb_iface_by_path(dev_path)
        devices.append(
            UsbWifiDevice(
                vid=vid,
                pid=pid,
                name=name,
                interface=iface,
                driver=driver,
                chipset_db=matched_chipset,
            )
        )

    return devices


def _find_usb_iface_by_path(dev_path: Path) -> Tuple[Optional[str], Optional[str]]:
    for subdir in dev_path.iterdir():
        if not subdir.is_dir():
            continue
        net_dir = subdir / "net"
        if net_dir.exists():
            try:
                ifaces = [p.name for p in net_dir.iterdir()]
            except OSError:
                continue
            if ifaces:
                iface = ifaces[0]
                driver = None
                driver_link = subdir / "driver"
                if driver_link.is_symlink():
                    try:
                        driver = os.path.basename(os.readlink(str(driver_link)))
                    except OSError:
                        pass  # driver link vanished; report it as unknown
                return iface, driver
    return None, None
