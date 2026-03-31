#!/usr/bin/env python3
"""Shared helpers for hotspot start/stop implementations."""

import os
import random
import re
import signal
import subprocess
import textwrap
import time
from typing import Optional, Tuple

from ..common import (
    AP_IP,
    AP_SUBNET,
    DHCP_RANGE,
    DNSMASQ_CONF,
    DNSMASQ_LEASES,
    DNSMASQ_PID,
    HOSTAPD_CONF,
    HOSTAPD_PID,
    info,
    ok,
    run,
    run_cmd,
    run_out,
    warn,
)

_DFS_CHANNELS = set(range(52, 145))

_SAFE_24G_CHANNELS = ("1", "6", "11")
_SAFE_5G_CHANNELS = ("36", "40", "44", "48")


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
def _get_sta_channel_band(iface: str) -> Tuple[Optional[str], Optional[str]]:
    link_info = run_out(f"iw dev {iface} link")
    freq_match = re.search(r"freq:\s*(\d+)", link_info)
    if not freq_match:
        return None, None
    freq_mhz = int(freq_match.group(1))
    channel = _freq_to_channel(freq_mhz)
    band    = "a" if freq_mhz >= 5000 else "bg"
    return channel, band


def _pick_least_congested_channel(iface: str, band: str) -> Optional[str]:
    """Pick a safe channel with the lowest observed scan congestion score."""
    candidates = _SAFE_5G_CHANNELS if band == "a" else _SAFE_24G_CHANNELS
    scores = {ch: 0.0 for ch in candidates}

    # CHAN,SIGNAL is enough for lightweight congestion scoring.
    scan = run_out(f"nmcli -t -f CHAN,SIGNAL device wifi list ifname {iface}")
    if not scan:
        return None

    seen = False
    for line in scan.splitlines():
        parts = line.split(":")
        if len(parts) < 2:
            continue
        channel = parts[0].strip()
        if channel not in scores:
            continue

        seen = True
        try:
            signal = int(parts[1].strip())
        except ValueError:
            signal = 40

        # Stronger neighboring APs count more toward channel congestion.
        scores[channel] += max(1.0, signal / 25.0)

    if not seen:
        return None

    return min(scores, key=lambda ch: (scores[ch], int(ch)))

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
