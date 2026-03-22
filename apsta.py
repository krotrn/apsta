#!/usr/bin/env python3
"""
apsta - Smart AP+STA (simultaneous hotspot + WiFi client) manager
Phase 3: USB dongle detection + adapter recommendations

Usage:
    apsta detect              # Check if your hardware supports AP+STA
    apsta start               # Start hotspot (auto-detects best method)
    apsta stop                # Stop hotspot
    apsta status              # Show current state
    apsta config              # Show/set saved config
    apsta enable              # Install systemd service + sleep hook
    apsta disable             # Uninstall systemd service + sleep hook
    apsta scan-usb            # Detect plugged-in USB WiFi adapters + their AP+STA capability
    apsta recommend           # Suggest USB adapters to buy if built-in card lacks AP+STA
"""

import subprocess
import sys
import os
import json
import argparse
import re
import time
import random
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ── Config ────────────────────────────────────────────────────────────────────
#
# Config lives in /etc/apsta/ not ~/.config/apsta/ because every operation
# that matters (start, stop, enable, the systemd service, the sleep hook)
# runs as root. Path.home() and $HOME evaluate to /root under sudo/systemd,
# so a per-user path creates a split-brain: user writes to /home/user/...,
# service reads from /root/... and silently uses defaults.

CONFIG_PATH = Path("/etc/apsta/config.json")
DEFAULT_CONFIG = {
    "ssid": "apsta-hotspot",
    "password": "changeme123",
    "band": "bg",
    "channel": "11",          # fallback only — overridden at runtime by STA frequency
    "interface": None,        # auto-detect if None
    "ap_interface": None,     # set at runtime, cleared on stop
    "base_interface": None,   # actual STA iface name saved at start, used by stop
    "active_con_name": None,  # NM connection profile name saved at start, used by stop
}

# ── Colors ────────────────────────────────────────────────────────────────────

class C:
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"

def ok(msg):   print(f"  {C.GREEN}✔{C.RESET}  {msg}")
def err(msg):  print(f"  {C.RED}✘{C.RESET}  {msg}")
def warn(msg): print(f"  {C.YELLOW}⚠{C.RESET}  {msg}")
def info(msg): print(f"  {C.CYAN}→{C.RESET}  {msg}")
def head(msg): print(f"\n{C.BOLD}{msg}{C.RESET}")

# ── Shell helpers ──────────────────────────────────────────────────────────────

def run(cmd: str, capture=True, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, shell=True, capture_output=capture,
        text=True, check=check
    )

def run_out(cmd: str) -> str:
    result = run(cmd)
    return result.stdout.strip() if result.returncode == 0 else ""

def require_root():
    if os.geteuid() != 0:
        err("This command requires root. Run with: sudo apsta " + " ".join(sys.argv[1:]))
        sys.exit(1)

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
    supports_ap_sta_concurrent: bool
    max_interfaces: int
    supported_modes: list
    combinations: list      # raw combination strings
    driver: str
    chipset: str

def get_wifi_interfaces() -> List[WifiInterface]:
    """Parse ip link output to find WiFi interfaces."""
    ifaces = []
    result = run_out("ip link show")
    # match lines like: 3: wlo1: <...> ...
    for match in re.finditer(r"^\d+: (\w+):.*$", result, re.MULTILINE):
        name = match.group(1)
        # check if it's a wifi interface via iw
        check = run(f"iw dev {name} info")
        if check.returncode != 0:
            continue
        # get MAC
        mac_match = re.search(r"addr ([\w:]+)", check.stdout)
        mac = mac_match.group(1) if mac_match else "unknown"
        # check if UP
        state = "UP" if "UP" in match.group(0) else "DOWN"
        # get connected SSID
        ssid = None
        ssid_result = run_out(f"iw dev {name} link")
        ssid_match = re.search(r"SSID: (.+)", ssid_result)
        if ssid_match:
            ssid = ssid_match.group(1).strip()
        ifaces.append(WifiInterface(name=name, mac=mac, state=state, connected_ssid=ssid))
    return ifaces

def get_hardware_capability(iface: str) -> HardwareCapability:
    """Parse iw list to determine AP+STA support."""
    iw_output = run_out("iw list")

    # supported modes
    modes = re.findall(r"\* (\w[\w/ ]+)", iw_output)
    supported_modes = [m.strip() for m in modes if len(m.strip()) < 30]

    supports_ap  = any("AP" in m and "VLAN" not in m for m in supported_modes)
    supports_sta = any(m.strip() == "managed" for m in supported_modes)

    # valid interface combinations - this is the key check
    combinations = []
    ap_sta_concurrent = False
    max_ifaces = 1

    combo_section = re.search(
        r"valid interface combinations:(.*?)(?=\n\t[A-Z]|\n\nAvailable|\Z)",
        iw_output, re.DOTALL
    )
    if combo_section:
        combo_text = combo_section.group(1)
        combo_lines = []
        for l in combo_text.splitlines():
            stripped = l.strip()

            if not stripped.startswith("* #{"):
                continue
            if "total <=" not in stripped:
                continue
            combo_lines.append(stripped)

        for line in combo_lines:
            combinations.append(line)
            has_ap  = bool(re.search(r"\bAP\b", line))
            has_sta = bool(re.search(r"\bmanaged\b", line))
            total_match = re.search(r"total <= (\d+)", line)
            total = int(total_match.group(1)) if total_match else 1
            if has_ap and has_sta and total >= 2:
                ap_sta_concurrent = True
                max_ifaces = max(max_ifaces, total)

    # get driver name
    driver = ""
    phy_match = re.search(r"Wiphy (\w+)", iw_output)
    if phy_match:
        phy = phy_match.group(1)
        driver_path = f"/sys/class/net/{iface}/device/driver"
        if os.path.islink(driver_path):
            driver = os.path.basename(os.readlink(driver_path))

    # chipset from lspci or lsusb
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
        max_interfaces=max_ifaces,
        supported_modes=supported_modes,
        combinations=combinations,
        driver=driver,
        chipset=chipset,
    )

# ── USB WiFi chipset database ──────────────────────────────────────────────────
#
# Source: morrownr/USB-WiFi (the authoritative Linux USB WiFi reference).
# Only chipsets with confirmed in-kernel drivers and AP+STA concurrent support
# are listed. VID:PID entries cover the most common adapters per chipset.
# Realtek chipsets are intentionally excluded — their out-of-kernel drivers
# are unmaintained and AP+STA support is unreliable across distros.
#
# Fields per entry:
#   chipset        : silicon identifier
#   driver         : in-kernel module name
#   ap_sta         : True if driver exposes concurrent AP+STA interface combinations
#   min_kernel     : minimum kernel version string for AP mode support
#   wifi_gen       : "WiFi 5", "WiFi 6", "WiFi 6E", "WiFi 7"
#   vid_pids       : list of (vendor_id, product_id) hex strings
#   buy_search     : short search term for finding adapters online
#   notes          : important caveats (BT interference, min kernel, etc.)

@dataclass
class UsbChipset:
    chipset:    str
    driver:     str
    ap_sta:     bool
    min_kernel: str
    wifi_gen:   str
    vid_pids:   List[Tuple[str, str]]   # (vid, pid) both lowercase hex, no 0x prefix
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
            ("0e8d", "7961"),  # standard MediaTek VID/PID
            ("3574", "6211"),  # Comfast CF-952AX / CF-953AX
            ("13b1", "0045"),  # Linksys AE6000 (some revisions)
            ("0846", "9060"),  # Netgear A8000
            ("2357", "0138"),  # TP-Link Archer TX20U
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
            ("0846", "9100"),  # Netgear A9000
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
            ("0e8d", "7612"),  # standard MediaTek VID/PID
            ("7392", "b711"),  # Edimax EW-7822UAC
            ("2357", "0103"),  # TP-Link Archer T4U v1
            ("0b05", "17d1"),  # ASUS USB-AC55
            ("0846", "9053"),  # Netgear A6210
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
            ("0e8d", "7610"),  # standard MediaTek VID/PID
            ("7392", "a711"),  # Edimax EW-7711UAC
            ("2357", "0105"),  # TP-Link Archer T1U
        ],
        buy_search="mt7610u USB WiFi Linux",
        notes="AC600 single-band 5GHz. Very stable. Good for hotspot-only dongle use.",
    ),
]

@dataclass
class UsbWifiDevice:
    """A USB WiFi adapter detected on the system."""
    vid:        str
    pid:        str
    name:       str          # from lsusb description
    interface:  Optional[str]   # kernel interface name if assigned (e.g. wlan1)
    driver:     Optional[str]   # kernel module if loaded
    chipset_db: Optional[UsbChipset]  # matched entry from USB_CHIPSET_DB, or None


def scan_usb_wifi() -> List[UsbWifiDevice]:
    """
    Detect USB WiFi adapters by walking /sys/bus/usb/devices/ directly.

    Driving the scan from sysfs (not lsusb) solves the duplicate-dongle problem:
    if a user plugs in two identical mt7921au adapters with the same VID:PID,
    lsusb emits two identical lines and a VID:PID lookup returns the first sysfs
    match for both — assigning wlan1 to both entries and missing wlan2.
    By iterating sysfs entries first we get one UsbWifiDevice per physical port,
    each with the correct interface. lsusb is used only to fetch the human-readable
    device name, keyed by Bus+Device number which is always unique.
    """
    # Build a lookup table: (bus, devnum) -> human-readable name from lsusb
    # lsusb line: "Bus 002 Device 003: ID abcd:1234 TP-Link Corp. Archer T4U"
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

        # Match against chipset DB
        matched_chipset = None
        for cs in USB_CHIPSET_DB:
            if (vid, pid) in cs.vid_pids:
                matched_chipset = cs
                break

        # Get human-readable name from lsusb lookup
        # /sys/.../busnum and devnum files hold the bus/device numbers
        name = ""
        try:
            busnum = (dev_path / "busnum").read_text().strip().zfill(3)
            devnum = (dev_path / "devnum").read_text().strip().zfill(3)
            name = lsusb_names.get((busnum, devnum), "")
        except OSError:
            pass

        # If not in DB, check name for WiFi keywords before including
        if not matched_chipset:
            wifi_keywords = ("wireless", "wlan", "wifi", "802.11", "wi-fi", "mediatek", "ralink")
            if not any(k in name.lower() for k in wifi_keywords):
                continue

        # Find the kernel interface and driver for this specific sysfs entry
        iface, driver = _find_usb_iface_by_path(dev_path)

        devices.append(UsbWifiDevice(
            vid=vid, pid=pid, name=name,
            interface=iface, driver=driver,
            chipset_db=matched_chipset,
        ))

    return devices


def _find_usb_iface_by_path(dev_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """
    Given a /sys/bus/usb/devices/<entry> path, find the kernel network interface
    name and loaded driver for that specific physical USB port.

    Taking dev_path directly (instead of VID/PID) means two identical adapters
    on different ports each get their own correct interface, never the same one.
    """
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


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_detect(args):
    head("apsta — Hardware Detection")

    ifaces = get_wifi_interfaces()
    if not ifaces:
        err("No WiFi interfaces found.")
        sys.exit(1)

    print()
    info(f"Found {len(ifaces)} WiFi interface(s):")
    for iface in ifaces:
        connected = f"connected to {C.GREEN}{iface.connected_ssid}{C.RESET}" if iface.connected_ssid else f"{C.DIM}not connected{C.RESET}"
        print(f"     {C.BOLD}{iface.name}{C.RESET}  [{iface.mac}]  {connected}")

    # use first UP interface, or first available
    target = next((i for i in ifaces if i.state == "UP"), ifaces[0])
    print()
    info(f"Analysing {C.BOLD}{target.name}{C.RESET} ...")

    cap = get_hardware_capability(target.name)

    head("Capability Report")

    if cap.driver:
        info(f"Driver:   {cap.driver}")
    if cap.chipset:
        info(f"Chipset:  {cap.chipset}")

    print()
    _print_cap("AP mode (hotspot)",       cap.supports_ap)
    _print_cap("STA mode (WiFi client)",  cap.supports_sta)
    _print_cap("AP+STA simultaneous",     cap.supports_ap_sta_concurrent)

    if cap.combinations:
        print()
        info("Interface combinations from driver:")
        for combo in cap.combinations:
            print(f"     {C.DIM}{combo}{C.RESET}")

    print()
    head("Verdict")
    if cap.supports_ap_sta_concurrent:
        ok("Your hardware supports AP+STA simultaneously.")
        ok("apsta can create a hotspot without dropping your WiFi.")
        info("Run:  sudo apsta start")
    elif cap.supports_ap:
        warn("Your hardware supports AP mode but NOT concurrent AP+STA.")
        warn("Starting a hotspot will disconnect your current WiFi.")
        print()
        info("Options:")
        print(f"     1. Plug ethernet into your wired port → hotspot freely on {target.name}")
        print(f"     2. Use a USB WiFi dongle as the AP interface")
        print(f"     3. Accept the tradeoff: disconnect WiFi, run hotspot")
        print()
        # Phase 3: check if a capable USB dongle is already plugged in
        usb_devices = scan_usb_wifi()
        capable = [d for d in usb_devices if d.chipset_db and d.chipset_db.ap_sta]
        if capable:
            ok("A compatible USB adapter is already plugged in:")
            for dev in capable:
                iface = dev.interface or "not yet assigned"
                print(f"     {C.BOLD}{dev.chipset_db.chipset}{C.RESET}  [{dev.vid}:{dev.pid}]  iface: {iface}")
            info("Configure it:  sudo apsta config --set interface=<iface>")
        else:
            info("Run:  apsta recommend   to see which USB dongle to buy")
        info("Run:  sudo apsta start --force   to proceed without a dongle")
    else:
        err("Your hardware does not support AP mode.")
        err("A USB WiFi adapter is required.")
        print()
        info("Run:  apsta recommend   to see which USB dongle to buy")

    print()

def _print_cap(label: str, value: bool):
    icon = f"{C.GREEN}✔{C.RESET}" if value else f"{C.RED}✘{C.RESET}"
    status = f"{C.GREEN}supported{C.RESET}" if value else f"{C.RED}not supported{C.RESET}"
    print(f"     {icon}  {label:<30} {status}")


def cmd_start(args):
    require_root()
    head("apsta — Starting Hotspot")

    config = load_config()
    ifaces = get_wifi_interfaces()

    if not ifaces:
        err("No WiFi interfaces found.")
        sys.exit(1)

    # pick interface
    iface_name = config.get("interface") or ifaces[0].name
    target = next((i for i in ifaces if i.name == iface_name), None)
    if not target:
        err(f"Configured interface '{iface_name}' not found.")
        info("Run:  apsta config --set interface=none   to reset to auto-detect")
        info("Run:  apsta detect                        to list available interfaces")
        sys.exit(1)
    cap = get_hardware_capability(target.name)

    print()

    if not cap.supports_ap:
        err("AP mode not supported on this interface. Run: apsta detect")
        sys.exit(1)

    if target.connected_ssid and not cap.supports_ap_sta_concurrent and not args.force:
        warn(f"Currently connected to '{target.connected_ssid}'.")
        warn("Concurrent AP+STA not supported — starting hotspot will disconnect WiFi.")
        warn("Use --force to proceed, or connect via ethernet first.")
        sys.exit(1)

    ssid     = config.get("ssid")     or DEFAULT_CONFIG["ssid"]
    password = config.get("password") or DEFAULT_CONFIG["password"]

    # Dynamically derive channel AND band from the STA's current frequency.
    # This solves two problems in one:
    #   1. Single-radio EBUSY: AP must match STA channel exactly.
    #   2. Band mismatch: 5 GHz channel + band=bg is invalid and always fails.
    detected_channel, detected_band = _get_sta_channel_band(target.name)

    if detected_channel and detected_band:
        channel = detected_channel
        band    = detected_band
        info(f"Detected STA frequency → channel {channel}, band {'5 GHz' if band == 'a' else '2.4 GHz'}")

        # Edge case: DFS channels (52–144) are regulatory-blocked for AP use
        # in most countries at the kernel level (mac80211 regulatory domain).
        # Additionally, on single-radio cards (the common case), the AP must
        # share the exact same channel as the STA — so falling back to channel
        # 36 would cause EBUSY because the hardware cannot split across channels.
        # The only correct path is to tell the user clearly and abort.
        if _is_dfs_channel(channel):
            err(f"Channel {channel} is a DFS channel (range 52–144).")
            err("AP mode on DFS channels is blocked by kernel regulatory rules in most countries.")
            print()
            warn("On a single-radio card, falling back to a non-DFS channel would")
            warn("either fail with EBUSY or forcibly disconnect your WiFi client.")
            print()
            info("To fix this, connect your laptop to a non-DFS network first:")
            info("  • 2.4 GHz networks (channels 1–14) are always safe")
            info("  • 5 GHz UNII-1 channels (36, 40, 44, 48) are safe")
            info("  • Ask your router admin to switch from channel 52–144")
            print()
            info("Then run:  sudo apsta start")
            sys.exit(1)
    else:
        # Not currently connected — use config values as-is.
        # Use `or` not .get(key, default) — if a key exists but is None
        # (e.g. user ran: apsta config --set channel=none), .get() returns
        # None and nmcli receives "channel None", which it rejects.
        channel = config.get("channel") or DEFAULT_CONFIG["channel"]
        band    = config.get("band")    or DEFAULT_CONFIG["band"]
        info(f"Not connected to STA — using configured channel {channel}, band {band}")

    if cap.supports_ap_sta_concurrent:
        info("Concurrent AP+STA supported — creating virtual AP interface...")
        ap_iface = _create_virtual_ap_iface(target.name)
        if not ap_iface:
            warn("Virtual interface creation failed, falling back to same interface.")
            ap_iface = target.name
    else:
        ap_iface = target.name

    info(f"Starting hotspot on {C.BOLD}{ap_iface}{C.RESET}")
    info(f"SSID:     {ssid}")
    info(f"Password: {password}")
    info(f"Band:     {'5 GHz' if band == 'a' else '2.4 GHz'}  (channel {channel})")
    print()

    result = run(
        f"nmcli device wifi hotspot ifname {ap_iface} "
        f"ssid '{ssid}' password '{password}' band {band} channel {channel}"
    )

    if result.returncode == 0:
        # extract the actual connection name NM assigned (may differ from ssid)
        # nmcli prints: "Device 'wlo1' successfully activated with '...UUID...'."
        # we need the profile name, not UUID — query NM directly
        active_con_name = _get_active_hotspot_con_name(ap_iface)

        # persist state so cmd_stop can tear down exactly the right things.
        # base_interface is saved explicitly here — cmd_stop must not rely on
        # config.get("interface") which may be None (auto-detect).
        config["ap_interface"]      = ap_iface
        config["base_interface"]    = target.name
        config["active_con_name"]   = active_con_name or ssid
        save_config(config)

        ok(f"Hotspot '{ssid}' is live on {ap_iface}")
        if ap_iface == target.name:
            # Same interface used for AP — the STA connection was replaced.
            # Only print a warning if the user was previously connected to something.
            if target.connected_ssid:
                warn(f"WiFi disconnected from '{target.connected_ssid}' (single interface in use).")
                info("Stop hotspot to reconnect:  sudo apsta stop")
        else:
            # Different interfaces — STA connection should still be alive.
            if target.connected_ssid:
                ok(f"Still connected to '{target.connected_ssid}'")
    else:
        err("Failed to start hotspot.")
        print(f"\n{C.DIM}{result.stderr}{C.RESET}")
        sys.exit(1)

    print()


def cmd_stop(args):
    require_root()
    head("apsta — Stopping Hotspot")
    print()

    config = load_config()

    # use the connection name we saved at start time — not the hardcoded "Hotspot"
    # which breaks for multi-word SSIDs and non-default NM naming
    con_name = config.get("active_con_name") or "Hotspot"

    result = run(f"nmcli connection down '{con_name}'")
    if result.returncode == 0:
        ok(f"Hotspot connection '{con_name}' stopped.")
    else:
        warn(f"Could not bring down '{con_name}', scanning for active hotspot connections...")
        # fallback: find any active 802-11 AP-mode connection
        active = run_out("nmcli -t -f NAME,TYPE,MODE con show --active")
        hotspot_cons = [
            l.split(":")[0] for l in active.splitlines()
            if l.split(":")[-1].strip().lower() in ("ap", "hotspot")
            or "hotspot" in l.lower()
        ]
        if hotspot_cons:
            for con in hotspot_cons:
                run(f"nmcli connection down '{con}'")
                ok(f"Stopped: {con}")
        else:
            warn("No active hotspot connection found.")

    # clean up virtual interface only if we created one.
    # Use base_interface saved at start time — not config["interface"] which
    # may be None (auto-detect setting), causing the _ap suffix check to be
    # the only guard, which is fragile.
    ap_iface   = config.get("ap_interface")
    base_iface = config.get("base_interface") or config.get("interface") or ""
    if ap_iface and ap_iface != base_iface and ap_iface.endswith("_ap"):
        result = run(f"iw dev {ap_iface} del")
        if result.returncode == 0:
            ok(f"Removed virtual interface {ap_iface}")
        else:
            warn(f"Could not remove {ap_iface} (may already be gone)")

    # clear runtime state from config
    config["ap_interface"]    = None
    config["base_interface"]  = None
    config["active_con_name"] = None
    save_config(config)

    print()


def cmd_status(args):
    head("apsta — Status")
    print()

    ifaces = get_wifi_interfaces()
    active_cons = run_out("nmcli -t -f NAME,TYPE,DEVICE,STATE con show --active")

    info("Active network connections:")
    for line in active_cons.splitlines():
        parts = line.split(":")
        if len(parts) >= 4:
            name, con_type, device, state = parts[0], parts[1], parts[2], parts[3]
            if "wireless" in con_type or "802-11" in con_type:
                print(f"     {C.BOLD}{name}{C.RESET} on {device} [{state}]")

    print()
    info("WiFi interfaces:")
    for iface in ifaces:
        connected = f"→ {C.GREEN}{iface.connected_ssid}{C.RESET}" if iface.connected_ssid else f"{C.DIM}idle{C.RESET}"
        print(f"     {C.BOLD}{iface.name}{C.RESET}  {connected}")

    print()


def cmd_config(args):
    head("apsta — Configuration")
    config = load_config()
    print()

    if args.set:
        require_root()  # reading config is fine without root; writing /etc/apsta/ requires it
        key, _, val = args.set.partition("=")
        if key not in DEFAULT_CONFIG:
            err(f"Unknown config key: {key}")
            err(f"Valid keys: {', '.join(DEFAULT_CONFIG.keys())}")
            sys.exit(1)
        # allow clearing nullable keys with "none" or ""
        if val.lower() in ("none", "null", ""):
            config[key] = None
            save_config(config)
            ok(f"Cleared {key} (auto-detect)")
        else:
            config[key] = val
            save_config(config)
            ok(f"Set {key} = {val}")
    else:
        info(f"Config file: {CONFIG_PATH}")
        print()
        for k, v in config.items():
            display = f"{C.YELLOW}{v}{C.RESET}" if v else f"{C.DIM}(auto){C.RESET}"
            print(f"     {k:<20} {display}")
        print()
        info("Change with:  apsta config --set ssid=MyHotspot")

    print()


def cmd_scan_usb(args):
    """Detect plugged-in USB WiFi adapters and report their AP+STA capability."""
    head("apsta — USB WiFi Adapter Scan")
    print()

    devices = scan_usb_wifi()

    if not devices:
        info("No USB WiFi adapters detected.")
        info("Plug in a USB adapter, then run:  apsta scan-usb")
        print()
        info("Don't have one? Run:  apsta recommend")
        print()
        return

    info(f"Found {len(devices)} USB WiFi device(s):")
    print()

    for dev in devices:
        cs = dev.chipset_db
        vid_pid = f"{dev.vid}:{dev.pid}"

        if cs:
            ap_sta_icon = f"{C.GREEN}✔ AP+STA{C.RESET}" if cs.ap_sta else f"{C.RED}✘ no AP+STA{C.RESET}"
            print(f"  {C.BOLD}{cs.chipset}{C.RESET}  [{vid_pid}]  {cs.wifi_gen}  {ap_sta_icon}")
            print(f"       Name:    {dev.name}")
            print(f"       Driver:  {dev.driver or C.DIM + 'not loaded' + C.RESET}")
            iface_display = dev.interface or f"{C.DIM}not assigned{C.RESET}"
            print(f"       Iface:   {iface_display}")
            print(f"       Kernel:  {cs.min_kernel}+ required for AP mode")
            if cs.notes:
                print(f"       Note:    {C.DIM}{cs.notes}{C.RESET}")

            if cs.ap_sta and dev.interface:
                print()
                ok(f"This adapter supports AP+STA. Use it as your hotspot interface:")
                info(f"  sudo apsta config --set interface={dev.interface}")
                info(f"  sudo apsta start")
            elif cs.ap_sta and not dev.interface:
                print()
                warn("Adapter is recognized but has no kernel interface assigned.")
                info("Two likely causes:")
                info("  1. Driver not loaded — check:  lsmod | grep " + (cs.driver or "mt7921u"))
                info("  2. Missing firmware — check:   sudo dmesg | grep firmware")
        else:
            # Unknown chipset — show what we know
            print(f"  {C.DIM}Unknown chipset{C.RESET}  [{vid_pid}]")
            print(f"       Name:    {dev.name}")
            print(f"       Driver:  {dev.driver or C.DIM + 'unknown' + C.RESET}")
            iface_display = dev.interface or f"{C.DIM}not assigned{C.RESET}"
            print(f"       Iface:   {iface_display}")
            warn("This chipset is not in apsta's database.")
            info("Run:  apsta detect   to check AP+STA via iw list")

        print()

    # Check current kernel version against min_kernel requirements
    kernel_ver = run_out("uname -r").split("-")[0]  # e.g. "6.8.0"
    _warn_kernel_if_needed(devices, kernel_ver)


def _warn_kernel_if_needed(devices: List[UsbWifiDevice], kernel_ver: str):
    """Warn if any detected adapter requires a newer kernel than what's running."""
    def parse_ver(v: str) -> Tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split(".")[:2])
        except ValueError:
            return (0, 0)

    running = parse_ver(kernel_ver)
    for dev in devices:
        if dev.chipset_db:
            required = parse_ver(dev.chipset_db.min_kernel)
            if running < required:
                warn(f"{dev.chipset_db.chipset} requires kernel {dev.chipset_db.min_kernel}+, "
                     f"but you're running {kernel_ver}.")
                info("AP mode will not work until you upgrade your kernel.")


def cmd_recommend(args):
    """
    Suggest USB WiFi adapters to buy based on what the built-in card lacks.
    Checks the built-in card first, then recommends the best-fit chipset.
    """
    head("apsta — USB Adapter Recommendations")
    print()

    # Check what the built-in card can do
    ifaces = get_wifi_interfaces()
    builtin_has_ap_sta = False
    if ifaces:
        target = next((i for i in ifaces if i.state == "UP"), ifaces[0])
        cap = get_hardware_capability(target.name)
        builtin_has_ap_sta = cap.supports_ap_sta_concurrent

    if builtin_has_ap_sta:
        ok("Your built-in card already supports AP+STA simultaneously.")
        ok("You don't need a USB dongle.")
        info("Run:  sudo apsta start")
        print()
        return

    # Check if a compatible USB dongle is already plugged in
    usb_devices = scan_usb_wifi()
    capable_plugged = [d for d in usb_devices if d.chipset_db and d.chipset_db.ap_sta]
    if capable_plugged:
        ok("You already have a compatible USB adapter plugged in:")
        for dev in capable_plugged:
            print(f"     {C.BOLD}{dev.chipset_db.chipset}{C.RESET}  [{dev.vid}:{dev.pid}]"
                  f"  iface: {dev.interface or C.DIM + 'not yet assigned' + C.RESET}")
        print()
        info("Configure it:  sudo apsta config --set interface=<iface>")
        info("Then start:    sudo apsta start")
        print()
        return

    # Built-in card can't do AP+STA, no capable dongle plugged in.
    # Recommend adapters from the DB, best-first.
    warn("Your built-in card does not support concurrent AP+STA.")
    warn("A USB WiFi dongle is needed to run a hotspot without dropping WiFi.")
    print()
    head("Recommended USB Adapters")
    print()

    # Order: WiFi 6 first (mt7921au), then WiFi 5 stable options
    recommended = [cs for cs in USB_CHIPSET_DB if cs.ap_sta]

    for cs in recommended:
        wifi_color = C.CYAN if "6" in cs.wifi_gen or "7" in cs.wifi_gen else C.DIM
        print(f"  {C.BOLD}{cs.chipset}{C.RESET}  {wifi_color}{cs.wifi_gen}{C.RESET}  "
              f"(kernel {cs.min_kernel}+)")
        print(f"       Driver:  {cs.driver}  (in-kernel, plug and play)")
        print(f"       Search:  {C.YELLOW}{cs.buy_search}{C.RESET}")
        if cs.notes:
            print(f"       Notes:   {C.DIM}{cs.notes}{C.RESET}")
        print()

    info("After plugging in an adapter, run:  apsta scan-usb")
    info("to verify it's detected, then:    sudo apsta config --set interface=<iface>")
    print()

    # Also show kernel version for context
    kernel_ver = run_out("uname -r").split("-")[0]
    info(f"Your kernel: {kernel_ver}")
    print()


# ── Channel / band helpers ─────────────────────────────────────────────────────

# DFS channels in the 5 GHz band (52–144). Acting as AP on these is blocked
# by the kernel's regulatory domain in most countries.
_DFS_CHANNELS = set(range(52, 145))   # channels 52–144 inclusive

def _get_sta_channel_band(iface: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (channel, band) derived from the STA's current operating frequency.

    Band is 'bg' (2.4 GHz) or 'a' (5 GHz) — matching nmcli's expected values.
    Returns (None, None) if the interface is not currently connected.

    Why this matters:
      - Single-radio cards require AP and STA on the exact same channel
        (hardware constraint: #channels <= 1). Mismatching causes EBUSY.
      - Channel 36 is 5 GHz; passing band=bg channel=36 to nmcli is invalid
        and will always fail.
    """
    link_info = run_out(f"iw dev {iface} link")
    freq_match = re.search(r"freq:\s*(\d+)", link_info)
    if not freq_match:
        return None, None
    freq_mhz = int(freq_match.group(1))
    channel = _freq_to_channel(freq_mhz)
    band    = "a" if freq_mhz >= 5000 else "bg"
    return channel, band

def _freq_to_channel(freq: int) -> Optional[str]:
    """Convert MHz frequency to WiFi channel number string."""
    # 2.4 GHz band
    mapping_24 = {
        2412: "1",  2417: "2",  2422: "3",  2427: "4",
        2432: "5",  2437: "6",  2442: "7",  2447: "8",
        2452: "9",  2457: "10", 2462: "11", 2467: "12",
        2472: "13", 2484: "14",
    }
    if freq in mapping_24:
        return mapping_24[freq]
    # 5 GHz band: channel = (freq - 5000) / 5
    if 5170 <= freq <= 5825:
        return str((freq - 5000) // 5)
    return None

def _is_dfs_channel(channel: str) -> bool:
    """Return True if channel is in the DFS range (52–144) where AP mode is
    regulatory-blocked in most countries."""
    try:
        return int(channel) in _DFS_CHANNELS
    except (ValueError, TypeError):
        return False

def _get_active_hotspot_con_name(ap_iface: str) -> Optional[str]:
    """
    After nmcli creates the hotspot, find the actual NM connection profile name
    so cmd_stop can bring it down by name regardless of SSID or NM naming.

    NM registers the profile asynchronously. Slower systems (RPi, old hardware)
    can take 2–3 seconds. We poll up to 3 times with 1s gaps instead of a
    single fixed sleep to avoid both false negatives and unnecessary waiting.
    """
    for attempt in range(3):
        active = run_out("nmcli -t -f NAME,TYPE,DEVICE con show --active")
        for line in active.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[2] == ap_iface:
                return parts[0]
        if attempt < 2:
            time.sleep(1)
    return None

def _create_virtual_ap_iface(base_iface: str) -> Optional[str]:
    """
    Create a virtual AP interface on top of an existing WiFi interface.

    The kernel assigns the virtual interface the same permanent MAC address as
    the base interface. Some drivers (Intel iwlwifi, certain Realtek) and strict
    NetworkManager configs refuse to activate two interfaces with identical MACs.
    We randomize the locally-administered bit after creation to avoid this.
    """
    ap_iface = f"{base_iface}_ap"
    # remove stale instance if it exists
    run(f"iw dev {ap_iface} del")

    result = run(f"iw dev {base_iface} interface add {ap_iface} type __ap")
    if result.returncode != 0:
        return None

    # Force DOWN before MAC change — Linux forbids MAC changes on UP interfaces.
    # The kernel sometimes inherits UP state from the base interface on creation.
    run(f"ip link set {ap_iface} down")

    # Assign a randomized locally-administered MAC to avoid duplicate MAC
    # rejection. Format: keep first octet with LA bit set (02:xx:xx:xx:xx:xx).
    rand_mac = "02:%02x:%02x:%02x:%02x:%02x" % tuple(random.randint(0, 255) for _ in range(5))
    mac_result = run(f"ip link set {ap_iface} address {rand_mac}")
    if mac_result.returncode != 0:
        # non-fatal: some drivers handle it fine without randomization
        warn(f"Could not randomize MAC for {ap_iface} — proceeding anyway.")

    run(f"ip link set {ap_iface} up")

    # Edge case: privacy-focused distros configure NetworkManager to globally
    # randomize MACs on all WiFi interfaces. NM might re-randomize ap_iface
    # immediately after we bring it up, causing a brief collision race condition.
    # Fix: tell NM to preserve our chosen MAC for this interface specifically.
    run(
        f"nmcli device set {ap_iface} "
        f"managed yes wifi.cloned-mac-address {rand_mac}"
    )

    return ap_iface

# ── Config I/O ─────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
            return {**DEFAULT_CONFIG, **saved}
        except json.JSONDecodeError:
            warn(f"Config file {CONFIG_PATH} is corrupted (possibly from power loss). Using defaults.")
    return dict(DEFAULT_CONFIG)

def save_config(config: dict):
    # /etc/apsta/ needs to exist and be root-writable.
    # Mode 755: root can write, others can read (so non-root `apsta status`
    # can still read the config to display current state).
    CONFIG_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    CONFIG_PATH.chmod(0o644)

# Path to the directory containing apsta.py — used to locate bundled system files
SCRIPT_DIR = Path(__file__).resolve().parent

# Paths where Phase 2 system files are installed (systemd only)
SLEEP_HOOK_DEST  = Path("/usr/lib/systemd/system-sleep/apsta-sleep")
SERVICE_DEST     = Path("/etc/systemd/system/apsta.service")

# ── Init system detection ──────────────────────────────────────────────────────

def _detect_init() -> str:
    """
    Return the running init system: 'systemd', 'openrc', 'runit', or 'unknown'.

    Checks in order of reliability:
      1. /run/systemd/private  — directory only present when systemd is PID 1
      2. /run/openrc/softlevel — created by OpenRC on boot
      3. /run/runit/stopit     — runit control directory
      4. readlink /proc/1/exe  — last resort, parse the binary name
    """
    if Path("/run/systemd/private").exists():
        return "systemd"
    if Path("/run/openrc/softlevel").exists():
        return "openrc"
    if Path("/run/runit").exists():
        return "runit"
    # fallback: check what PID 1 actually is
    pid1 = run_out("readlink -f /proc/1/exe")
    if "systemd" in pid1:
        return "systemd"
    if "openrc" in pid1:
        return "openrc"
    if "runit" in pid1:
        return "runit"
    return "unknown"

# ── CLI ────────────────────────────────────────────────────────────────────────

def cmd_enable(args):
    """Install auto-start and sleep/wake persistence for the detected init system."""
    require_root()
    head("apsta — Enabling auto-start and sleep/wake persistence")
    print()

    import shutil

    # Self-install the binary unconditionally — before any init-specific logic.
    # This must happen regardless of init system so that the manual instructions
    # printed for OpenRC/runit users reference a path that actually exists.
    # Without this, non-systemd users see "run /usr/local/bin/apsta start"
    # but the binary was never copied there.
    binary_dest    = Path("/usr/local/bin/apsta")
    running_script = Path(sys.argv[0]).resolve()
    if running_script != binary_dest:
        if binary_dest.is_symlink():
            warn(f"{binary_dest} is a symlink — overwriting it with a regular file.")
            warn("If apsta was installed via a package manager, use that to update instead.")
            binary_dest.unlink()
        shutil.copy2(running_script, binary_dest)
        binary_dest.chmod(0o755)
        ok(f"Binary installed → {binary_dest}")
    else:
        ok(f"Binary already at {binary_dest}")

    # Locate bundled system files relative to the running script.
    # Edge case: if someone copied only apsta.py to /usr/local/bin/ without
    # the repo, SCRIPT_DIR = /usr/local/bin and system/ won't exist there.
    # Detect this early and explain what's needed rather than crashing later.
    system_dir     = SCRIPT_DIR / "system"
    sleep_hook_src = system_dir / "apsta-sleep"
    service_src    = system_dir / "apsta.service"

    if not system_dir.exists():
        print()
        warn(f"Bundled system/ directory not found at: {system_dir}")
        warn("apsta was likely installed as a standalone file, not from the full repo.")
        info("To get the system files, clone the full repo:")
        print(f"     {C.DIM}git clone https://github.com/yourusername/apsta")
        print(f"     cd apsta && sudo ./install.sh{C.RESET}")
        print()
        info("The binary has been installed to /usr/local/bin/apsta.")
        info("Re-run 'sudo apsta enable' from the cloned repo to finish setup.")
        print()
        sys.exit(1)

    init = _detect_init()
    info(f"Detected init system: {C.BOLD}{init}{C.RESET}")
    print()

    if init != "systemd":
        _enable_non_systemd(init, sleep_hook_src)
        return

    _enable_systemd(sleep_hook_src, service_src)


def _enable_systemd(sleep_hook_src: Path, service_src: Path):
    """Install the systemd service unit and sleep hook."""
    import shutil

    for src, label in [(sleep_hook_src, "sleep hook"), (service_src, "service unit")]:
        if not src.exists():
            err(f"Bundled {label} not found: {src}")
            sys.exit(1)

    shutil.copy2(sleep_hook_src, SLEEP_HOOK_DEST)
    SLEEP_HOOK_DEST.chmod(0o755)
    ok(f"Sleep hook installed → {SLEEP_HOOK_DEST}")

    shutil.copy2(service_src, SERVICE_DEST)
    ok(f"Service unit installed → {SERVICE_DEST}")

    _run_sys("systemctl daemon-reload",          "Reloading systemd")
    _run_sys("systemctl enable apsta.service",   "Enabling apsta.service")
    _run_sys("systemctl start apsta.service",    "Starting apsta.service now")

    print()
    ok("apsta will now start automatically on boot and resume from sleep.")
    info("Check service status:  systemctl status apsta")
    print()


def _enable_non_systemd(init: str, sleep_hook_src: Path):
    """
    Explain manual setup steps for non-systemd init systems.

    We cannot safely automate OpenRC/runit service installation because:
      - Service script locations vary by distro (Gentoo, Alpine, Artix, Devuan)
      - OpenRC service scripts require specific syntax and cannot be auto-generated
        without knowing the exact distro conventions
      - runit service directories vary (/etc/sv, /service, /var/service)
    The correct approach is to tell the user exactly what to do rather than
    guess wrong and leave a broken service behind.

    sleep_hook_src is passed in from cmd_enable, which already validated that
    the system/ directory exists before reaching this function.
    """
    # The sleep hook is init-agnostic — install via pm-utils if available.
    import shutil

    if init == "openrc":
        warn("OpenRC detected — automated service installation is not supported.")
        print()
        info("To auto-start apsta on boot, add to /etc/local.d/apsta.start:")
        print(f"     {C.DIM}#!/bin/sh")
        print(f"     nm-online -q && /usr/local/bin/apsta start{C.RESET}")
        print()
        info("Make it executable:")
        print(f"     {C.DIM}chmod +x /etc/local.d/apsta.start{C.RESET}")
        print()
        info("For sleep/wake persistence, install the hook manually:")
        print(f"     {C.DIM}# Add to /etc/pm/sleep.d/apsta or use acpid{C.RESET}")

    elif init == "runit":
        warn("runit detected — automated service installation is not supported.")
        print()
        info("To create a runit service:")
        print(f"     {C.DIM}mkdir -p /etc/sv/apsta")
        print(f"     echo '#!/bin/sh' > /etc/sv/apsta/run")
        print(f"     echo 'exec /usr/local/bin/apsta start' >> /etc/sv/apsta/run")
        print(f"     chmod +x /etc/sv/apsta/run")
        print(f"     ln -s /etc/sv/apsta /var/service/{C.RESET}")
        print()
        info("Check your distro's runit service directory — it may be /service instead of /var/service.")

    else:
        warn(f"Unknown init system — cannot automate service installation.")
        print()
        info("Manual setup: run the following at startup (after NetworkManager):")
        print(f"     {C.DIM}nm-online -q && /usr/local/bin/apsta start{C.RESET}")

    # Sleep hook is init-agnostic — install it regardless
    print()
    hook_installed = False
    if sleep_hook_src.exists():
        # systemd-sleep directory won't exist on non-systemd — use pm-utils if present.
        pm_sleep_dir = Path("/etc/pm/sleep.d")
        if pm_sleep_dir.exists():
            dest = pm_sleep_dir / "10_apsta"
            shutil.copy2(sleep_hook_src, dest)
            dest.chmod(0o755)
            ok(f"Sleep/wake hook installed → {dest}  (pm-utils)")
            hook_installed = True
        else:
            warn("pm-utils not found (/etc/pm/sleep.d missing).")
            warn("Sleep/wake persistence requires manual setup.")
            info("Options: acpid, elogind, or add to your suspend/resume scripts.")
            info(f"Hook script is at: {sleep_hook_src}")
    print()
    # Clearly separate what's done vs what still needs the user's attention
    print(f"  {C.BOLD}Summary{C.RESET}")
    if hook_installed:
        ok("Sleep/wake persistence: installed (see above)")
    else:
        warn("Sleep/wake persistence: NOT installed — requires manual setup (see above)")
    warn("Auto-start on boot:    NOT installed — requires manual setup (see above)")
    info("Once configured, test with:  sudo apsta start")
    print()


def cmd_disable(args):
    """Uninstall auto-start and sleep/wake persistence."""
    require_root()
    head("apsta — Disabling auto-start and sleep/wake persistence")
    print()

    init = _detect_init()
    info(f"Detected init system: {C.BOLD}{init}{C.RESET}")
    print()

    if init != "systemd":
        warn(f"Automated disable is only supported on systemd.")
        warn(f"Remove the service/startup entry you created manually for {init}.")
        # Still remove the sleep hook if we installed one via pm-utils
        pm_hook = Path("/etc/pm/sleep.d/10_apsta")
        if pm_hook.exists():
            pm_hook.unlink()
            ok(f"Removed sleep hook: {pm_hook}")
        print()
        return

    # Use run() directly — _run_sys() exits on failure, but systemctl stop
    # and disable return non-zero if the service is already stopped or the unit
    # never existed. We must not abort before the file deletion steps below.
    r = run("systemctl stop apsta.service")
    ok("Stopped apsta.service") if r.returncode == 0 else info("apsta.service was not running")

    r = run("systemctl disable apsta.service")
    ok("Disabled apsta.service") if r.returncode == 0 else info("apsta.service was not enabled")

    for path, label in [(SERVICE_DEST, "service unit"), (SLEEP_HOOK_DEST, "sleep hook")]:
        if path.exists():
            path.unlink()
            ok(f"Removed {label}: {path}")
        else:
            info(f"{label} not found (already removed?): {path}")

    _run_sys("systemctl daemon-reload", "Reloading systemd")

    print()
    ok("Auto-start and sleep/wake persistence disabled.")
    info("Hotspot is still running if it was active. Stop with:  sudo apsta stop")
    print()


def _run_sys(cmd: str, label: str):
    """Run a system command, printing status. Exit on failure."""
    result = run(cmd)
    if result.returncode == 0:
        ok(label)
    else:
        err(f"{label} failed")
        if result.stderr:
            print(f"     {C.DIM}{result.stderr.strip()}{C.RESET}")
        sys.exit(1)


def _check_dependencies():
    """
    Verify required system binaries are present before any command runs.
    Fails fast with a clear install hint rather than a cryptic FileNotFoundError
    buried inside a subprocess call.
    """
    deps = {
        "nmcli": "network-manager",
        "iw":    "iw",
        "ip":    "iproute2",
        "lsusb": "usbutils",
        "lspci": "pciutils",
    }
    missing = []
    for binary, package in deps.items():
        result = run(f"command -v {binary}")
        if result.returncode != 0:
            missing.append((binary, package))

    if missing:
        err("Missing required dependencies:")
        for binary, package in missing:
            print(f"     {C.BOLD}{binary}{C.RESET}  →  sudo apt install {package}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="apsta",
        description="Smart AP+STA WiFi hotspot manager for Linux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
commands:
  detect        Check hardware AP+STA capabilities
  start         Start hotspot (auto-detects best method)
  stop          Stop active hotspot
  status        Show current WiFi and hotspot state
  config        View or edit saved configuration
  enable        Install systemd service + sleep hook (auto-start on boot)
  disable       Uninstall systemd service + sleep hook
  scan-usb      Detect plugged-in USB WiFi adapters + AP+STA capability
  recommend     Suggest USB adapters to buy if built-in card lacks AP+STA

examples:
  apsta detect
  apsta scan-usb
  apsta recommend
  sudo apsta start
  sudo apsta start --force
  apsta config --set ssid=MyHotspot
  sudo apsta enable
        """
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("detect",    help="Detect hardware AP+STA capability")

    p_start = sub.add_parser("start", help="Start hotspot")
    p_start.add_argument("--force", action="store_true",
                         help="Start even if concurrent AP+STA not supported")

    sub.add_parser("stop",      help="Stop hotspot")
    sub.add_parser("status",    help="Show status")
    sub.add_parser("enable",    help="Install systemd service + sleep hook")
    sub.add_parser("disable",   help="Uninstall systemd service + sleep hook")
    sub.add_parser("scan-usb",  help="Detect USB WiFi adapters + AP+STA capability")
    sub.add_parser("recommend", help="Suggest USB adapters to buy")

    p_config = sub.add_parser("config", help="View/edit config")
    p_config.add_argument("--set", metavar="KEY=VALUE",
                          help="Set a config value (e.g. --set ssid=MyHotspot)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    # Run after arg parsing so `apsta --help` works even without deps installed
    _check_dependencies()

    dispatch = {
        "detect":    cmd_detect,
        "start":     cmd_start,
        "stop":      cmd_stop,
        "status":    cmd_status,
        "config":    cmd_config,
        "enable":    cmd_enable,
        "disable":   cmd_disable,
        "scan-usb":  cmd_scan_usb,
        "recommend": cmd_recommend,
    }

    try:
        dispatch[args.command](args)
    except KeyboardInterrupt:
        print(f"\n{C.DIM}Interrupted.{C.RESET}")
        sys.exit(130)

if __name__ == "__main__":
    main()