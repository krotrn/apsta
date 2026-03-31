#!/usr/bin/env python3
"""Hardware and USB WiFi capability detection helpers."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from .common import run, run_out
# ── Hardware detection ─────────────────────────────────────────────────────────

@dataclass
class WifiInterface:
    name: str
    mac: str
    state: str              # UP / DOWN
    connected_ssid: Optional[str]

@dataclass
class HardwareCapability:
    interface: str
    supports_ap: bool
    supports_sta: bool
    supports_ap_sta_concurrent: bool   # nmcli-level: AP+managed in same combo block
    supports_ap_sta_split: bool        # hostapd-level: AP and managed in separate blocks, #channels<=1
    max_interfaces: int
    supported_modes: list
    combinations: list
    driver: str
    chipset: str

def get_wifi_interfaces() -> List[WifiInterface]:
    """Parse ip link output to find WiFi interfaces."""
    ifaces = []
    result = run_out("ip link show")
    for match in re.finditer(r"^\d+: (\w+):.*$", result, re.MULTILINE):
        name = match.group(1)
        check = run(f"iw dev {name} info")
        if check.returncode != 0:
            continue
        mac_match = re.search(r"addr ([\w:]+)", check.stdout)
        mac = mac_match.group(1) if mac_match else "unknown"
        state = "UP" if "UP" in match.group(0) else "DOWN"
        ssid = None
        ssid_result = run_out(f"iw dev {name} link")
        ssid_match = re.search(r"SSID: (.+)", ssid_result)
        if ssid_match:
            ssid = ssid_match.group(1).strip()
        ifaces.append(WifiInterface(name=name, mac=mac, state=state, connected_ssid=ssid))
    return ifaces

def get_hardware_capability(iface: str) -> HardwareCapability:
    iw_output = run_out("iw list")

    modes = re.findall(r"\* (\w[\w/ ]+)", iw_output)
    supported_modes = [m.strip() for m in modes if len(m.strip()) < 30]

    supports_ap  = any("AP" in m and "VLAN" not in m for m in supported_modes)
    supports_sta = any(m.strip() == "managed" for m in supported_modes)

    combinations = []
    ap_sta_concurrent = False
    ap_sta_split      = False
    max_ifaces = 1

    combo_section = re.search(
        r"valid interface combinations:(.*?)(?=\n\t[A-Z]|\n\nAvailable|\Z)",
        iw_output, re.DOTALL
    )
    if combo_section:
        combo_text = combo_section.group(1)

        # Join continuation lines into single combo entries.
        # The format is:
        #   * #{ managed } <= 1, #{ AP } <= 1, #{ P2P-device } <= 1,
        #     total <= 3, #channels <= 1
        # The second line starts with spaces but NOT with "* #{".
        # We join it onto the previous line so the parser sees one string.
        joined_lines = []
        for line in combo_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("* #{"):
                joined_lines.append(stripped)
            elif joined_lines:
                # Continuation line — append to previous entry
                joined_lines[-1] += " " + stripped

        for entry in joined_lines:
            if "total <=" not in entry:
                continue
            combinations.append(entry)

            channels_match = re.search(r"#channels <= (\d+)", entry)
            channels = int(channels_match.group(1)) if channels_match else 99

            total_match = re.search(r"total <= (\d+)", entry)
            total = int(total_match.group(1)) if total_match else 1

            groups = re.findall(r"#\{([^}]+)\}", entry)

            has_ap_group  = any("AP" in g and "VLAN" not in g for g in groups)
            has_sta_group = any("managed" in g for g in groups)

            if has_ap_group and has_sta_group:
                max_ifaces = max(max_ifaces, total)
                same_group = any(
                    "AP" in g and "managed" in g and "VLAN" not in g
                    for g in groups
                )
                if same_group and total >= 2:
                    ap_sta_concurrent = True
                elif not same_group and total >= 2 and channels <= 1:
                    ap_sta_split = True

    driver = ""
    phy_match = re.search(r"Wiphy (\w+)", iw_output)
    if phy_match:
        driver_path = f"/sys/class/net/{iface}/device/driver"
        if os.path.islink(driver_path):
            driver = os.path.basename(os.readlink(driver_path))

    chipset = ""
    lspci = run_out("lspci | grep -i wireless")
    if lspci:
        chipset = lspci.split(":")[-1].strip()
    else:
        lsusb = run_out("lsusb | grep -i wireless")
        if lsusb:
            chipset = lsusb

    return HardwareCapability(
        interface=iface,
        supports_ap=supports_ap,
        supports_sta=supports_sta,
        supports_ap_sta_concurrent=ap_sta_concurrent,
        supports_ap_sta_split=ap_sta_split,
        max_interfaces=max_ifaces,
        supported_modes=supported_modes,
        combinations=combinations,
        driver=driver,
        chipset=chipset,
    )

# ── USB WiFi chipset database ──────────────────────────────────────────────────

@dataclass
class UsbChipset:
    chipset:    str
    driver:     str
    ap_sta:     bool
    min_kernel: str
    wifi_gen:   str
    vid_pids:   List[Tuple[str, str]]
    buy_search: str
    notes:      str

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
        notes="Best overall choice. Avoid adapters with Bluetooth enabled (causes USB3 interference). Kernel 6.6+ recommended.",
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
    vid:        str
    pid:        str
    name:       str
    interface:  Optional[str]
    driver:     Optional[str]
    chipset_db: Optional[UsbChipset]


def scan_usb_wifi() -> List[UsbWifiDevice]:
    lsusb_names: dict = {}
    lsusb_out = run_out("lsusb")
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
            pass

        if not matched_chipset:
            wifi_keywords = ("wireless", "wlan", "wifi", "802.11", "wi-fi", "mediatek", "ralink")
            if not any(k in name.lower() for k in wifi_keywords):
                continue

        iface, driver = _find_usb_iface_by_path(dev_path)
        devices.append(UsbWifiDevice(
            vid=vid, pid=pid, name=name,
            interface=iface, driver=driver,
            chipset_db=matched_chipset,
        ))

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
                        pass
                return iface, driver
    return None, None


