#!/usr/bin/env python3
"""
apsta - Smart AP+STA (simultaneous hotspot + WiFi client) manager
Phase 4: hostapd-based AP+STA for cards without nmcli concurrent support

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

Phase 4 start strategy (tried in order):
    1. nmcli hotspot on virtual interface  — true concurrent AP+STA (best)
    2. hostapd on virtual interface        — works on Intel AX200 and similar
    3. nmcli hotspot on same interface     — drops WiFi (--force only)
"""

import subprocess
import sys
import os
import json
import argparse
import re
import time
import random
import signal
import textwrap
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

__version__ = "0.5.6"

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
    "start_method": None,     # "nmcli", "hostapd", or "nmcli-force" — set at start
}

# Paths for hostapd runtime files
HOSTAPD_CONF    = Path("/tmp/apsta-hostapd.conf")
HOSTAPD_PID     = Path("/tmp/apsta-hostapd.pid")
DNSMASQ_CONF    = Path("/tmp/apsta-dnsmasq.conf")
DNSMASQ_PID     = Path("/tmp/apsta-dnsmasq.pid")
DNSMASQ_LEASES  = Path("/tmp/apsta-dnsmasq.leases")

# IP address assigned to the AP interface in hostapd mode
AP_IP           = "192.168.42.1"
AP_SUBNET       = "192.168.42.0/24"
DHCP_RANGE      = ("192.168.42.10", "192.168.42.100")

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

def run_cmd(args: List[str], capture=True, check=False) -> subprocess.CompletedProcess:
    """Run a command without shell interpolation."""
    return subprocess.run(args, capture_output=capture, text=True, check=check)

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


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_detect(args):
    ifaces = get_wifi_interfaces()
    if not ifaces:
        if getattr(args, "json", False):
            print(json.dumps({"error": "No WiFi interfaces found."}))
        err("No WiFi interfaces found.")
        sys.exit(1)

    target = next((i for i in ifaces if i.state == "UP"), ifaces[0])
    cap = get_hardware_capability(target.name)

    if cap.supports_ap_sta_concurrent:
        verdict = {
            "level": "ok",
            "mode": "nmcli",
            "messages": [
                "Your hardware supports AP+STA simultaneously (nmcli mode).",
                "apsta can create a hotspot without dropping your WiFi.",
            ],
            "next": "sudo apsta start",
        }
    elif cap.supports_ap_sta_split:
        verdict = {
            "level": "ok",
            "mode": "hostapd",
            "messages": [
                "Your hardware supports AP+STA simultaneously (hostapd mode).",
                "apsta will use hostapd + dnsmasq to share WiFi without disconnecting.",
            ],
            "next": "sudo apsta start",
            "note": "requires hostapd and dnsmasq installed",
        }
    elif cap.supports_ap:
        verdict = {
            "level": "warn",
            "mode": "force-only",
            "messages": [
                "Your hardware supports AP mode but NOT concurrent AP+STA.",
                "Starting a hotspot will disconnect your current WiFi.",
            ],
            "next": "sudo apsta start --force",
        }
    else:
        verdict = {
            "level": "error",
            "mode": "unsupported",
            "messages": [
                "Your hardware does not support AP mode.",
                "A USB WiFi adapter is required.",
            ],
            "next": "apsta recommend",
        }

    if getattr(args, "json", False):
        payload = {
            "interfaces": [
                {
                    "name": i.name,
                    "mac": i.mac,
                    "state": i.state,
                    "connected_ssid": i.connected_ssid,
                }
                for i in ifaces
            ],
            "target_interface": target.name,
            "capability": {
                "interface": cap.interface,
                "supports_ap": cap.supports_ap,
                "supports_sta": cap.supports_sta,
                "supports_ap_sta_concurrent": cap.supports_ap_sta_concurrent,
                "supports_ap_sta_split": cap.supports_ap_sta_split,
                "max_interfaces": cap.max_interfaces,
                "supported_modes": cap.supported_modes,
                "combinations": cap.combinations,
                "driver": cap.driver,
                "chipset": cap.chipset,
            },
            "verdict": verdict,
        }
        print(json.dumps(payload, indent=2))
        return

    head("apsta — Hardware Detection")

    print()
    info(f"Found {len(ifaces)} WiFi interface(s):")
    for iface in ifaces:
        connected = f"connected to {C.GREEN}{iface.connected_ssid}{C.RESET}" if iface.connected_ssid else f"{C.DIM}not connected{C.RESET}"
        print(f"     {C.BOLD}{iface.name}{C.RESET}  [{iface.mac}]  {connected}")

    print()
    info(f"Analysing {C.BOLD}{target.name}{C.RESET} ...")

    head("Capability Report")

    if cap.driver:
        info(f"Driver:   {cap.driver}")
    if cap.chipset:
        info(f"Chipset:  {cap.chipset}")

    print()
    _print_cap("AP mode (hotspot)",             cap.supports_ap)
    _print_cap("STA mode (WiFi client)",         cap.supports_sta)
    _print_cap("AP+STA simultaneous (nmcli)",    cap.supports_ap_sta_concurrent)
    _print_cap("AP+STA simultaneous (hostapd)",  cap.supports_ap_sta_split)

    if cap.combinations:
        print()
        info("Interface combinations from driver:")
        for combo in cap.combinations:
            print(f"     {C.DIM}{combo}{C.RESET}")

    print()
    head("Verdict")
    if verdict["mode"] == "nmcli":
        ok("Your hardware supports AP+STA simultaneously (nmcli mode).")
        ok("apsta can create a hotspot without dropping your WiFi.")
        info("Run:  sudo apsta start")
    elif verdict["mode"] == "hostapd":
        ok("Your hardware supports AP+STA simultaneously (hostapd mode).")
        ok("apsta will use hostapd + dnsmasq to share WiFi without disconnecting.")
        info("Run:  sudo apsta start")
        info("Note: requires hostapd and dnsmasq installed.")
    elif verdict["mode"] == "force-only":
        warn("Your hardware supports AP mode but NOT concurrent AP+STA.")
        warn("Starting a hotspot will disconnect your current WiFi.")
        print()
        info("Options:")
        print(f"     1. Plug ethernet into your wired port → hotspot freely on {target.name}")
        print(f"     2. Use a USB WiFi dongle as the AP interface")
        print(f"     3. Accept the tradeoff: disconnect WiFi, run hotspot")
        print()
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
    print(f"     {icon}  {label:<38} {status}")


# ── Phase 4: hostapd AP+STA ───────────────────────────────────────────────────

def _check_hostapd_deps() -> bool:
    """Return True if hostapd and dnsmasq are both installed."""
    missing = []
    for binary in ("hostapd", "dnsmasq"):
        if not run_out(f"command -v {binary}"):
            missing.append(binary)
    if missing:
        warn(f"hostapd mode requires: {', '.join(missing)}")
        info("Install with:  sudo apt install " + " ".join(missing))
        return False
    return True


def _write_hostapd_conf(ap_iface: str, ssid: str, password: str, channel: str) -> None:
    """Write hostapd configuration file for AP+STA mode."""
    # hw_mode: g = 2.4 GHz, a = 5 GHz
    # hostapd hw_mode must match the selected channel range.
    try:
        hw_mode = "a" if int(channel) > 14 else "g"
    except (TypeError, ValueError):
        hw_mode = "g"

    conf = textwrap.dedent(f"""\
        interface={ap_iface}
        driver=nl80211
        ssid={ssid}
        hw_mode={hw_mode}
        channel={channel}
        wpa=2
        wpa_passphrase={password}
        wpa_key_mgmt=WPA-PSK
        wpa_pairwise=CCMP
        rsn_pairwise=CCMP
        # Allow hostapd to manage the interface directly without
        # requiring it to be brought UP first — needed because
        # `ip link set ap0 up` returns EBUSY when wlo1 is in use.
        ignore_broadcast_ssid=0
    """)
    HOSTAPD_CONF.write_text(conf)


def _write_dnsmasq_conf(ap_iface: str) -> None:
    """Write dnsmasq configuration for DHCP on the AP interface."""
    conf = textwrap.dedent(f"""\
        interface={ap_iface}
        bind-interfaces
        dhcp-range={DHCP_RANGE[0]},{DHCP_RANGE[1]},24h
        dhcp-leasefile={DNSMASQ_LEASES}
        # Don't read /etc/resolv.conf — use Google DNS for hotspot clients
        no-resolv
        server=8.8.8.8
        server=8.8.4.4
    """)
    DNSMASQ_CONF.write_text(conf)


def _start_hostapd_ap_sta(
    base_iface: str,
    ssid: str,
    password: str,
    channel: str,
) -> Optional[str]:
    """
    Start AP+STA using hostapd on a virtual interface.

    Steps:
      1. Create virtual ap0 interface on top of base_iface
      2. Assign randomized locally-administered MAC
      3. Tell NM to ignore ap0 (not base_iface — that stays connected)
      4. Write hostapd + dnsmasq configs
      5. Start hostapd (brings ap0 up itself)
      6. Assign IP to ap0
      7. Start dnsmasq for DHCP
      8. Enable NAT so hotspot clients get internet

    Returns the ap interface name on success, None on failure.
    """
    ap_iface = f"{base_iface}_ap"

    # Remove stale virtual interface if it exists
    run(f"iw dev {ap_iface} del 2>/dev/null")

    # Create virtual AP interface
    result = run(f"iw dev {base_iface} interface add {ap_iface} type __ap")
    if result.returncode != 0:
        warn(f"Could not create virtual interface {ap_iface}: {result.stderr.strip()}")
        return None

    # Assign randomized locally-administered MAC to avoid duplicate MAC rejection.
    # Keep base_iface MAC unchanged — NM uses it to track the STA connection.
    rand_mac = "02:%02x:%02x:%02x:%02x:%02x" % tuple(random.randint(0, 255) for _ in range(5))
    run(f"ip link set {ap_iface} down 2>/dev/null")
    mac_result = run(f"ip link set {ap_iface} address {rand_mac}")
    if mac_result.returncode != 0:
        warn(f"Could not set MAC for {ap_iface} — proceeding anyway.")

    # Tell NM to ignore ap0 ONLY — base_iface stays managed and connected
    run(f"nmcli dev set {ap_iface} managed no")

    # Write configs
    _write_hostapd_conf(ap_iface, ssid, password, channel)
    _write_dnsmasq_conf(ap_iface)

    # Start hostapd in background
    # hostapd brings the interface up itself — ip link set up is not needed
    # and returns EBUSY because the physical radio is in use by base_iface.
    hostapd_result = run(
        f"hostapd -B -P {HOSTAPD_PID} {HOSTAPD_CONF}"
    )
    if hostapd_result.returncode != 0:
        warn(f"hostapd failed to start: {hostapd_result.stderr.strip() or hostapd_result.stdout.strip()}")
        run(f"iw dev {ap_iface} del 2>/dev/null")
        return None

    # Wait briefly for hostapd to initialize
    time.sleep(1)

    iw_info = run_out(f"iw dev {ap_iface} info")
    if not any(t in iw_info for t in ("type AP", "type AP/VLAN")):
        warn("hostapd started but AP interface did not come up.")
        run("pkill -f 'hostapd.*apsta' 2>/dev/null")
        run(f"iw dev {ap_iface} del 2>/dev/null")
        return None

    # Assign IP address to ap_iface
    run(f"ip addr flush dev {ap_iface} 2>/dev/null")
    ip_result = run(f"ip addr add {AP_IP}/24 dev {ap_iface}")
    if ip_result.returncode != 0:
        warn(f"Could not assign IP to {ap_iface}: {ip_result.stderr.strip()}")

    # Start dnsmasq for DHCP
    dnsmasq_result = run(
        f"dnsmasq --conf-file={DNSMASQ_CONF} --pid-file={DNSMASQ_PID}"
    )
    if dnsmasq_result.returncode != 0:
        warn(f"dnsmasq failed: {dnsmasq_result.stderr.strip()} — clients won't get IP addresses.")

    # Enable IP forwarding and NAT so hotspot clients get internet
    # through base_iface's connection.
    run("sysctl -w net.ipv4.ip_forward=1 > /dev/null")

    # Flush any existing MASQUERADE rules for this interface to avoid duplicates
    run(f"iptables -t nat -D POSTROUTING -s {AP_SUBNET} -o {base_iface} -j MASQUERADE 2>/dev/null")
    run(f"iptables -t nat -A POSTROUTING -s {AP_SUBNET} -o {base_iface} -j MASQUERADE")

    # Allow forwarding between ap_iface and base_iface
    run(f"iptables -D FORWARD -i {ap_iface} -o {base_iface} -j ACCEPT 2>/dev/null")
    run(f"iptables -D FORWARD -i {base_iface} -o {ap_iface} -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null")
    run(f"iptables -A FORWARD -i {ap_iface} -o {base_iface} -j ACCEPT")
    run(f"iptables -A FORWARD -i {base_iface} -o {ap_iface} -m state --state RELATED,ESTABLISHED -j ACCEPT")

    return ap_iface


def _stop_hostapd_ap_sta(ap_iface: str, base_iface: str) -> None:
    """Tear down hostapd-based AP+STA setup cleanly."""

    # Stop hostapd
    if HOSTAPD_PID.exists():
        try:
            pid = int(HOSTAPD_PID.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
        except (ValueError, ProcessLookupError, OSError):
            pass
        HOSTAPD_PID.unlink(missing_ok=True)
    else:
        run("pkill -f 'hostapd.*apsta' 2>/dev/null")

    # Stop dnsmasq
    if DNSMASQ_PID.exists():
        try:
            pid = int(DNSMASQ_PID.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, OSError):
            pass
        DNSMASQ_PID.unlink(missing_ok=True)

    # Remove iptables rules
    run(f"iptables -t nat -D POSTROUTING -s {AP_SUBNET} -o {base_iface} -j MASQUERADE 2>/dev/null")
    run(f"iptables -D FORWARD -i {ap_iface} -o {base_iface} -j ACCEPT 2>/dev/null")
    run(f"iptables -D FORWARD -i {base_iface} -o {ap_iface} -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null")

    # Remove virtual interface
    if ap_iface and ap_iface != base_iface:
        result = run(f"iw dev {ap_iface} del")
        if result.returncode == 0:
            ok(f"Removed virtual interface {ap_iface}")
        else:
            warn(f"Could not remove {ap_iface} (may already be gone)")

    # Re-enable NM management of base_iface (it was never unmanaged in hostapd mode,
    # but ap_iface was set unmanaged — NM will clean that up on interface removal)
    HOSTAPD_CONF.unlink(missing_ok=True)
    DNSMASQ_CONF.unlink(missing_ok=True)
    DNSMASQ_LEASES.unlink(missing_ok=True)


# ── cmd_start ─────────────────────────────────────────────────────────────────

def cmd_start(args):
    config = load_config()
    ifaces = get_wifi_interfaces()
    if not ifaces:
        err("No WiFi interfaces found. Run: apsta detect")
        sys.exit(1)

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

    ssid     = config.get("ssid")     or DEFAULT_CONFIG["ssid"]
    password = config.get("password") or DEFAULT_CONFIG["password"]

    current_connected_ssid = target.connected_ssid
    detected_channel, detected_band = _get_sta_channel_band(target.name)

    if detected_channel and detected_band:
        channel = detected_channel
        band    = detected_band
        info(f"Detected STA frequency → channel {channel}, band {'5 GHz' if band == 'a' else '2.4 GHz'}")

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
        channel = config.get("channel") or DEFAULT_CONFIG["channel"]
        band    = config.get("band")    or DEFAULT_CONFIG["band"]
        info(f"Not connected to STA — using configured channel {channel}, band {band}")

    info(f"SSID:     {ssid}")
    info(f"Password: {password}")
    info(f"Band:     {'5 GHz' if band == 'a' else '2.4 GHz'}  (channel {channel})")
    print()

    # ── Strategy selection ────────────────────────────────────────────────────
    #
    # Try in order of preference:
    #   1. nmcli with virtual interface  — true concurrent, no hostapd needed
    #   2. hostapd with virtual interface — works on Intel AX200 and similar
    #   3. nmcli --force                 — drops WiFi (only with --force flag)

    if cap.supports_ap_sta_concurrent:
        # Strategy 1: nmcli on virtual interface
        info("Strategy: nmcli concurrent AP+STA (virtual interface)")
        ap_iface = _create_virtual_ap_iface(target.name)
        if not ap_iface:
            warn("Virtual interface creation failed, falling back to hostapd.")
            ap_iface = None

        if ap_iface:
            result = _run_nmcli_hotspot(ap_iface, ssid, password, band, channel)
            if result.returncode == 0:
                if _finalize_nmcli_start(config, target, ap_iface, ssid, current_connected_ssid):
                    config["start_method"] = "nmcli"
                    save_config(config)
                    return
                warn("nmcli returned success but AP did not come up, trying hostapd.")
                run(f"iw dev {ap_iface} del 2>/dev/null")
            else:
                warn("nmcli hotspot failed on virtual interface, trying hostapd.")
                run(f"iw dev {ap_iface} del 2>/dev/null")

            current_connected_ssid = _get_connected_ssid(target.name)

    if cap.supports_ap_sta_split and not args.force:
        # Strategy 2: hostapd on virtual interface
        info("Strategy: hostapd concurrent AP+STA (virtual interface)")

        if not _check_hostapd_deps():
            warn("Install hostapd and dnsmasq to enable this mode.")
            warn("Falling back to --force mode which will disconnect WiFi.")
            current_connected_ssid = _get_connected_ssid(target.name)
            if not current_connected_ssid:
                # Not connected anyway, just proceed
                pass
            else:
                err("Cannot start hotspot without disconnecting WiFi.")
                info("Options:")
                info("  sudo apt install hostapd dnsmasq   then retry")
                info("  sudo apsta start --force           to disconnect WiFi and proceed")
                sys.exit(1)
        else:
            ap_iface = _start_hostapd_ap_sta(target.name, ssid, password, channel)
            if ap_iface:
                config["ap_interface"]    = ap_iface
                config["base_interface"]  = target.name
                config["active_con_name"] = None  # not used in hostapd mode
                config["start_method"]    = "hostapd"
                save_config(config)

                ok(f"Hotspot '{ssid}' is live on {ap_iface}  (hostapd mode)")
                connected_now = _get_connected_ssid(target.name)
                if connected_now:
                    ok(f"Still connected to '{connected_now}'")
                ok(f"Hotspot gateway: {AP_IP}  —  clients get {DHCP_RANGE[0]}–{DHCP_RANGE[1]}")
                info("Stop with:  sudo apsta stop")
                print()
                return
            else:
                warn("hostapd mode failed. Falling back to --force mode.")
            current_connected_ssid = _get_connected_ssid(target.name)

    # Strategy 3: nmcli --force (drops WiFi)
    current_connected_ssid = _get_connected_ssid(target.name)
    if not args.force and current_connected_ssid:
        warn(f"Currently connected to '{current_connected_ssid}'.")
        warn("Concurrent AP+STA not supported — starting hotspot will disconnect WiFi.")
        warn("Use --force to proceed, or install hostapd/dnsmasq for AP+STA mode.")
        sys.exit(1)

    info("Strategy: nmcli hotspot (single interface — WiFi will disconnect)")
    ap_iface = target.name
    result = _run_nmcli_hotspot(ap_iface, ssid, password, band, channel)

    if result.returncode == 0:
        if _finalize_nmcli_start(config, target, ap_iface, ssid, current_connected_ssid):
            config["start_method"] = "nmcli-force"
            save_config(config)
        else:
            err("nmcli reported success but hotspot did not become active.")
            sys.exit(1)
    else:
        err("Failed to start hotspot.")
        print(f"\n{C.DIM}{result.stderr}{C.RESET}")
        sys.exit(1)

    print()


def _finalize_nmcli_start(
    config: dict,
    target: WifiInterface,
    ap_iface: str,
    ssid: str,
    connected_ssid_before: Optional[str] = None,
) -> bool:
    """Save state and print status after a successful nmcli hotspot start."""
    if not _ap_interface_is_up(ap_iface):
        return False

    active_con_name = _get_active_hotspot_con_name(ap_iface)
    config["ap_interface"]    = ap_iface
    config["base_interface"]  = target.name
    config["active_con_name"] = active_con_name or ssid
    save_config(config)

    was_connected = connected_ssid_before if connected_ssid_before is not None else target.connected_ssid

    ok(f"Hotspot '{ssid}' is live on {ap_iface}")
    if ap_iface == target.name:
        if was_connected:
            warn(f"WiFi disconnected from '{was_connected}' (single interface in use).")
            info("Stop hotspot to reconnect:  sudo apsta stop")
    else:
        if was_connected:
            ok(f"Still connected to '{was_connected}'")
    info("Stop with:  sudo apsta stop")
    print()
    return True


# ── cmd_stop ──────────────────────────────────────────────────────────────────

def cmd_stop(args):
    require_root()
    head("apsta — Stopping Hotspot")
    print()

    config = load_config()
    method = config.get("start_method")

    if method == "hostapd":
        ap_iface   = config.get("ap_interface")
        base_iface = config.get("base_interface") or ""
        if ap_iface:
            _stop_hostapd_ap_sta(ap_iface, base_iface)
            ok("Hotspot stopped.")
        else:
            warn("No active hostapd hotspot found in config.")
    else:
        # nmcli or nmcli-force stop
        con_name = config.get("active_con_name") or "Hotspot"
        result = run(f"nmcli connection down '{con_name}'")
        if result.returncode == 0:
            ok(f"Hotspot connection '{con_name}' stopped.")
        else:
            warn(f"Could not bring down '{con_name}', scanning for active hotspot connections...")
            active = run_out("nmcli -t -f NAME,TYPE,DEVICE,STATE con show --active")
            hotspot_cons = [
                l.split(":")[0] for l in active.splitlines()
                if "802-11-wireless" in l and (
                    l.split(":")[1].strip().lower() in ("ap", "hotspot")
                    or "hotspot" in l.split(":")[0].lower()
                )
            ]
            if hotspot_cons:
                for con in hotspot_cons:
                    run(f"nmcli connection down '{con}'")
                    ok(f"Stopped: {con}")
            else:
                warn("No active hotspot connection found.")

        # Clean up virtual interface if one was created
        ap_iface   = config.get("ap_interface")
        base_iface = config.get("base_interface") or config.get("interface") or ""
        if ap_iface and ap_iface != base_iface and ap_iface.endswith("_ap"):
            result = run(f"iw dev {ap_iface} del")
            if result.returncode == 0:
                ok(f"Removed virtual interface {ap_iface}")
            else:
                warn(f"Could not remove {ap_iface} (may already be gone)")

    # Clear runtime state
    config["ap_interface"]    = None
    config["base_interface"]  = None
    config["active_con_name"] = None
    config["start_method"]    = None
    save_config(config)

    print()


def cmd_status(args):
    config = load_config()
    method = config.get("start_method")

    ifaces = get_wifi_interfaces()

    if getattr(args, "json", False):
        active = []
        active_cons = run_out("nmcli -t -f NAME,TYPE,DEVICE,STATE con show --active")
        for line in active_cons.splitlines():
            parts = line.split(":")
            if len(parts) >= 4:
                active.append({
                    "name": parts[0],
                    "type": parts[1],
                    "device": parts[2],
                    "state": parts[3],
                })

        clients = []
        if method == "hostapd" and DNSMASQ_LEASES.exists():
            try:
                leases = DNSMASQ_LEASES.read_text().strip()
                for line in leases.splitlines():
                    parts = line.split()
                    if len(parts) >= 4:
                        clients.append({
                            "hostname": parts[3],
                            "mac": parts[1],
                            "ip": parts[2],
                        })
            except OSError:
                pass

        payload = {
            "method": method,
            "interfaces": [
                {
                    "name": i.name,
                    "mac": i.mac,
                    "state": i.state,
                    "connected_ssid": i.connected_ssid,
                }
                for i in ifaces
            ],
            "active_connections": active,
            "clients": clients,
            "config": {
                "ssid": config.get("ssid"),
                "band": config.get("band"),
                "channel": config.get("channel"),
                "ap_interface": config.get("ap_interface"),
                "base_interface": config.get("base_interface"),
            },
        }
        print(json.dumps(payload, indent=2))
        return

    head("apsta — Status")
    print()

    if method:
        info(f"Active method:  {C.BOLD}{method}{C.RESET}")
        print()

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

    # Show hostapd clients if active
    if method == "hostapd" and DNSMASQ_LEASES.exists():
        print()
        info("Connected clients:")
        try:
            leases = DNSMASQ_LEASES.read_text().strip()
            if leases:
                for line in leases.splitlines():
                    parts = line.split()
                    if len(parts) >= 4:
                        print(f"     {parts[3]}  [{parts[1]}]  {parts[2]}")
            else:
                print(f"     {C.DIM}No clients connected{C.RESET}")
        except OSError:
            pass

    print()


def cmd_config(args):
    head("apsta — Configuration")
    config = load_config()
    print()

    if args.set:
        require_root()
        key, _, val = args.set.partition("=")
        if key not in DEFAULT_CONFIG:
            err(f"Unknown config key: {key}")
            err(f"Valid keys: {', '.join(DEFAULT_CONFIG.keys())}")
            sys.exit(1)
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
            print(f"  {C.DIM}Unknown chipset{C.RESET}  [{vid_pid}]")
            print(f"       Name:    {dev.name}")
            print(f"       Driver:  {dev.driver or C.DIM + 'unknown' + C.RESET}")
            iface_display = dev.interface or f"{C.DIM}not assigned{C.RESET}"
            print(f"       Iface:   {iface_display}")
            warn("This chipset is not in apsta's database.")
            info("Run:  apsta detect   to check AP+STA via iw list")

        print()

    kernel_ver = run_out("uname -r").split("-")[0]
    _warn_kernel_if_needed(devices, kernel_ver)


def _warn_kernel_if_needed(devices: List[UsbWifiDevice], kernel_ver: str):
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
    head("apsta — USB Adapter Recommendations")
    print()

    ifaces = get_wifi_interfaces()
    builtin_has_ap_sta = False
    if ifaces:
        target = next((i for i in ifaces if i.state == "UP"), ifaces[0])
        cap = get_hardware_capability(target.name)
        builtin_has_ap_sta = cap.supports_ap_sta_concurrent or cap.supports_ap_sta_split

    if builtin_has_ap_sta:
        ok("Your built-in card already supports AP+STA simultaneously.")
        ok("You don't need a USB dongle.")
        info("Run:  sudo apsta start")
        print()
        return

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

    warn("Your built-in card does not support concurrent AP+STA.")
    warn("A USB WiFi dongle is needed to run a hotspot without dropping WiFi.")
    print()
    head("Recommended USB Adapters")
    print()

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

    kernel_ver = run_out("uname -r").split("-")[0]
    info(f"Your kernel: {kernel_ver}")
    print()


# ── Channel / band helpers ─────────────────────────────────────────────────────

_DFS_CHANNELS = set(range(52, 145))

def _get_sta_channel_band(iface: str) -> Tuple[Optional[str], Optional[str]]:
    link_info = run_out(f"iw dev {iface} link")
    freq_match = re.search(r"freq:\s*(\d+)", link_info)
    if not freq_match:
        return None, None
    freq_mhz = int(freq_match.group(1))
    channel = _freq_to_channel(freq_mhz)
    band    = "a" if freq_mhz >= 5000 else "bg"
    return channel, band

def _freq_to_channel(freq: int) -> Optional[str]:
    mapping_24 = {
        2412: "1",  2417: "2",  2422: "3",  2427: "4",
        2432: "5",  2437: "6",  2442: "7",  2447: "8",
        2452: "9",  2457: "10", 2462: "11", 2467: "12",
        2472: "13", 2484: "14",
    }
    if freq in mapping_24:
        return mapping_24[freq]
    if 5170 <= freq <= 5825:
        return str((freq - 5000) // 5)
    return None

def _is_dfs_channel(channel: str) -> bool:
    try:
        return int(channel) in _DFS_CHANNELS
    except (ValueError, TypeError):
        return False

def _get_active_hotspot_con_name(ap_iface: str) -> Optional[str]:
    for attempt in range(3):
        active = run_out("nmcli -t -f NAME,TYPE,DEVICE con show --active")
        for line in active.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[2] == ap_iface:
                return parts[0]
        if attempt < 2:
            time.sleep(1)
    return None

def _get_connected_ssid(iface: str) -> Optional[str]:
    """Read current SSID from `iw dev <iface> link`, if connected."""
    ssid_result = run_out(f"iw dev {iface} link")
    ssid_match = re.search(r"SSID: (.+)", ssid_result)
    return ssid_match.group(1).strip() if ssid_match else None

def _ap_interface_is_up(ap_iface: str) -> bool:
    """Verify that a given interface is in AP mode."""
    for attempt in range(3):
        iw_info = run_out(f"iw dev {ap_iface} info")
        if "type AP" in iw_info:
            return True
        if attempt < 2:
            time.sleep(1)
    return False

def _run_nmcli_hotspot(ap_iface: str, ssid: str, password: str, band: str, channel: str) -> subprocess.CompletedProcess:
    """Start hotspot with nmcli using argv mode to avoid shell quoting pitfalls."""
    return run_cmd([
        "nmcli", "device", "wifi", "hotspot",
        "ifname", ap_iface,
        "ssid", ssid,
        "password", password,
        "band", band,
        "channel", str(channel),
    ])

def _create_virtual_ap_iface(base_iface: str) -> Optional[str]:
    ap_iface = f"{base_iface}_ap"
    run(f"iw dev {ap_iface} del 2>/dev/null")

    result = run(f"iw dev {base_iface} interface add {ap_iface} type __ap")
    if result.returncode != 0:
        return None

    run(f"ip link set {ap_iface} down 2>/dev/null")

    rand_mac = "02:%02x:%02x:%02x:%02x:%02x" % tuple(random.randint(0, 255) for _ in range(5))
    mac_result = run(f"ip link set {ap_iface} address {rand_mac}")
    if mac_result.returncode != 0:
        warn(f"Could not randomize MAC for {ap_iface} — proceeding anyway.")

    run(f"ip link set {ap_iface} up")
    run(f"nmcli device set {ap_iface} managed yes wifi.cloned-mac-address {rand_mac}")

    return ap_iface

# ── Config I/O ─────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
            return {**DEFAULT_CONFIG, **saved}
        except json.JSONDecodeError:
            warn(f"Config file {CONFIG_PATH} is corrupted. Using defaults.")
    return dict(DEFAULT_CONFIG)

def save_config(config: dict):
    CONFIG_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    CONFIG_PATH.chmod(0o644)

SCRIPT_DIR = Path(__file__).resolve().parent

SLEEP_HOOK_DEST  = Path("/usr/lib/systemd/system-sleep/apsta-sleep")
SERVICE_DEST     = Path("/etc/systemd/system/apsta.service")

EMBEDDED_SLEEP_HOOK = """#!/usr/bin/env bash
# /usr/lib/systemd/system-sleep/apsta-sleep  (systemd)
# /etc/pm/sleep.d/10_apsta                   (pm-utils / OpenRC)

APSTA=\"/usr/local/bin/apsta\"
STATE_FILE=\"/run/apsta-was-active\"
CONFIG=\"/etc/apsta/config.json\"

case \"$1/$2\" in
    pre/* | suspend/* | hibernate/*)
        ACTION=\"before_sleep\"
        ;;
    post/* | resume/* | thaw/*)
        ACTION=\"after_sleep\"
        ;;
    *)
        exit 0
        ;;
esac

case \"$ACTION\" in
    before_sleep)
        if [ -f \"$CONFIG\" ]; then
            AP_IFACE=$(python3 -c "
import json, sys
try:
    c = json.load(open(sys.argv[1]))
    print(c.get('ap_interface') or '')
except: print('')
" \"$CONFIG\" 2>/dev/null)
            if [ -n \"$AP_IFACE\" ]; then
                touch \"$STATE_FILE\"
                \"$APSTA\" stop
            fi
        fi
        ;;

    after_sleep)
        if [ -f \"$STATE_FILE\" ]; then
            rm -f \"$STATE_FILE\"
            nm-online --timeout 15 -x --quiet 2>/dev/null
            \"$APSTA\" start
        fi
        ;;
esac

exit 0
"""

EMBEDDED_SERVICE_UNIT = """[Unit]
Description=apsta - AP+STA WiFi hotspot manager
After=NetworkManager.service network.target
Wants=NetworkManager.service
PartOf=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStartPre=/usr/bin/nm-online -q --timeout 30
ExecStart=/usr/local/bin/apsta start
ExecStop=/usr/local/bin/apsta stop
TimeoutStopSec=5
SuccessExitStatus=0 1

[Install]
WantedBy=multi-user.target
"""


def _write_embedded_system_files() -> Tuple[Path, Path]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="apsta-system-"))
    sleep_hook_src = tmp_dir / "apsta-sleep"
    service_src = tmp_dir / "apsta.service"

    sleep_hook_src.write_text(EMBEDDED_SLEEP_HOOK)
    sleep_hook_src.chmod(0o755)
    service_src.write_text(EMBEDDED_SERVICE_UNIT)
    service_src.chmod(0o644)

    return sleep_hook_src, service_src

# ── Init system detection ──────────────────────────────────────────────────────

def _detect_init() -> str:
    if Path("/run/systemd/private").exists():
        return "systemd"
    if Path("/run/openrc/softlevel").exists():
        return "openrc"
    if Path("/run/runit").exists():
        return "runit"
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
    require_root()
    head("apsta — Enabling auto-start and sleep/wake persistence")
    print()

    import shutil

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

    system_dir     = SCRIPT_DIR / "system"
    sleep_hook_src = system_dir / "apsta-sleep"
    service_src    = system_dir / "apsta.service"

    if not system_dir.exists():
        print()
        warn(f"Bundled system/ directory not found at: {system_dir}")
        info("Using embedded default service/hook templates from this apsta package.")
        sleep_hook_src, service_src = _write_embedded_system_files()

    init = _detect_init()
    info(f"Detected init system: {C.BOLD}{init}{C.RESET}")
    print()

    if init != "systemd":
        _enable_non_systemd(init, sleep_hook_src)
        return

    _enable_systemd(sleep_hook_src, service_src)


def _enable_systemd(sleep_hook_src: Path, service_src: Path):
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

    elif init == "runit":
        warn("runit detected — automated service installation is not supported.")
        print()
        info("To create a runit service:")
        print(f"     {C.DIM}mkdir -p /etc/sv/apsta")
        print(f"     echo '#!/bin/sh' > /etc/sv/apsta/run")
        print(f"     echo 'exec /usr/local/bin/apsta start' >> /etc/sv/apsta/run")
        print(f"     chmod +x /etc/sv/apsta/run")
        print(f"     ln -s /etc/sv/apsta /var/service/{C.RESET}")

    else:
        warn(f"Unknown init system — cannot automate service installation.")
        print()
        info("Manual setup: run the following at startup (after NetworkManager):")
        print(f"     {C.DIM}nm-online -q && /usr/local/bin/apsta start{C.RESET}")

    print()
    hook_installed = False
    if sleep_hook_src.exists():
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
            info(f"Hook script is at: {sleep_hook_src}")
    print()
    print(f"  {C.BOLD}Summary{C.RESET}")
    if hook_installed:
        ok("Sleep/wake persistence: installed (see above)")
    else:
        warn("Sleep/wake persistence: NOT installed — requires manual setup (see above)")
    warn("Auto-start on boot:    NOT installed — requires manual setup (see above)")
    info("Once configured, test with:  sudo apsta start")
    print()


def cmd_disable(args):
    require_root()
    head("apsta — Disabling auto-start and sleep/wake persistence")
    print()

    init = _detect_init()
    info(f"Detected init system: {C.BOLD}{init}{C.RESET}")
    print()

    if init != "systemd":
        warn(f"Automated disable is only supported on systemd.")
        warn(f"Remove the service/startup entry you created manually for {init}.")
        pm_hook = Path("/etc/pm/sleep.d/10_apsta")
        if pm_hook.exists():
            pm_hook.unlink()
            ok(f"Removed sleep hook: {pm_hook}")
        print()
        return

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
    result = run(cmd)
    if result.returncode == 0:
        ok(label)
    else:
        err(f"{label} failed")
        if result.stderr:
            print(f"     {C.DIM}{result.stderr.strip()}{C.RESET}")
        sys.exit(1)


def _check_dependencies():
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


def cmd_completion(args):
    shell = args.shell
    if shell == "bash":
        print(_completion_bash())
    elif shell == "zsh":
        print(_completion_zsh())
    elif shell == "fish":
        print(_completion_fish())
    else:
        err(f"Unsupported shell: {shell}")
        sys.exit(2)


def _completion_bash() -> str:
    return r'''_apsta_complete() {
    local cur prev
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local commands="detect start stop status config enable disable scan-usb recommend completion"

    if [[ ${COMP_CWORD} -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "${commands}" -- "${cur}") )
    return 0
    fi

    case "${COMP_WORDS[1]}" in
    start)
        COMPREPLY=( $(compgen -W "--force --json" -- "${cur}") )
        ;;
    detect|status)
        COMPREPLY=( $(compgen -W "--json" -- "${cur}") )
        ;;
    config)
        COMPREPLY=( $(compgen -W "--set" -- "${cur}") )
        ;;
    completion)
        COMPREPLY=( $(compgen -W "bash zsh fish" -- "${cur}") )
        ;;
    esac
}

complete -F _apsta_complete apsta'''


def _completion_zsh() -> str:
    return r'''#compdef apsta

_apsta() {
    local -a commands
    commands=(
        'detect:Detect hardware AP+STA capability'
        'start:Start hotspot'
        'stop:Stop hotspot'
        'status:Show status'
        'config:View/edit config'
        'enable:Install auto-start hooks'
        'disable:Disable auto-start hooks'
        'scan-usb:Detect USB WiFi adapters'
        'recommend:Suggest USB adapters to buy'
        'completion:Print shell completion script'
    )

    _arguments -C \
        '1:command:->cmds' \
        '*::arg:->args'

    case $state in
        cmds)
            _describe 'command' commands
            ;;
        args)
            case $words[2] in
                start)
                    _values 'options' --force --json
                    ;;
                detect|status)
                    _values 'options' --json
                    ;;
                config)
                    _values 'options' --set
                    ;;
                completion)
                    _values 'shell' bash zsh fish
                    ;;
            esac
            ;;
    esac
}

_apsta "$@"'''


def _completion_fish() -> str:
    return r'''complete -c apsta -f
complete -c apsta -n "__fish_use_subcommand" -a "detect" -d "Detect hardware AP+STA capability"
complete -c apsta -n "__fish_use_subcommand" -a "start" -d "Start hotspot"
complete -c apsta -n "__fish_use_subcommand" -a "stop" -d "Stop hotspot"
complete -c apsta -n "__fish_use_subcommand" -a "status" -d "Show status"
complete -c apsta -n "__fish_use_subcommand" -a "config" -d "View/edit config"
complete -c apsta -n "__fish_use_subcommand" -a "enable" -d "Install auto-start hooks"
complete -c apsta -n "__fish_use_subcommand" -a "disable" -d "Disable auto-start hooks"
complete -c apsta -n "__fish_use_subcommand" -a "scan-usb" -d "Detect USB WiFi adapters"
complete -c apsta -n "__fish_use_subcommand" -a "recommend" -d "Suggest USB adapters"
complete -c apsta -n "__fish_use_subcommand" -a "completion" -d "Print shell completion script"

complete -c apsta -n "__fish_seen_subcommand_from start" -l force -d "Force single-interface mode"
complete -c apsta -n "__fish_seen_subcommand_from start detect status" -l json -d "Output JSON"
complete -c apsta -n "__fish_seen_subcommand_from config" -l set -d "Set config key"
complete -c apsta -n "__fish_seen_subcommand_from completion" -a "bash zsh fish"'''


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
  completion    Print shell completion script

start methods (tried in order):
  1. nmcli virtual interface   — true concurrent AP+STA
  2. hostapd virtual interface — Intel AX200 and similar (needs hostapd + dnsmasq)
  3. nmcli --force             — disconnects WiFi

examples:
  apsta detect
  apsta detect --json
  sudo apsta start
  sudo apsta start --force
  apsta status --json
  apsta config --set ssid=MyHotspot
  apsta completion zsh > ~/.zsh/completions/_apsta
  sudo apsta enable
        """
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_detect = sub.add_parser("detect", help="Detect hardware AP+STA capability")
    p_detect.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    p_start = sub.add_parser("start", help="Start hotspot")
    p_start.add_argument(
        "--force",
        action="store_true",
        help="Skip AP+STA attempts and force single-interface mode (drops WiFi)",
    )
    p_start.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    sub.add_parser("stop", help="Stop hotspot")

    p_status = sub.add_parser("status", help="Show status")
    p_status.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    sub.add_parser("enable", help="Install systemd service + sleep hook")
    sub.add_parser("disable", help="Uninstall systemd service + sleep hook")
    sub.add_parser("scan-usb", help="Detect USB WiFi adapters + AP+STA capability")
    sub.add_parser("recommend", help="Suggest USB adapters to buy")

    p_completion = sub.add_parser("completion", help="Print shell completion script")
    p_completion.add_argument("shell", choices=["bash", "zsh", "fish"], help="Target shell")

    p_config = sub.add_parser("config", help="View/edit config")
    p_config.add_argument("--set", metavar="KEY=VALUE", help="Set a config value (e.g. --set ssid=MyHotspot)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands_without_deps = {"completion"}
    if args.command not in commands_without_deps:
        _check_dependencies()

    dispatch = {
        "detect": cmd_detect,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "config": cmd_config,
        "enable": cmd_enable,
        "disable": cmd_disable,
        "scan-usb": cmd_scan_usb,
        "recommend": cmd_recommend,
        "completion": cmd_completion,
    }

    try:
        dispatch[args.command](args)
    except KeyboardInterrupt:
        print(f"\n{C.DIM}Interrupted.{C.RESET}")
        sys.exit(130)

if __name__ == "__main__":
    main()