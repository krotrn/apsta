#!/usr/bin/env python3
"""Hotspot start command and start-specific helpers."""

import sys
from typing import Optional

from ..common import (
    AP_IP,
    C,
    DEFAULT_CONFIG,
    DHCP_RANGE,
    command_lock,
    dbg,
    err,
    info,
    load_config,
    ok,
    run,
    save_config,
    warn,
)
from ..hardware import WifiInterface, get_hardware_capability, get_wifi_interfaces
from .support import (
    _ap_interface_is_up,
    _check_hostapd_deps,
    _create_virtual_ap_iface,
    _get_active_hotspot_con_name,
    _get_connected_ssid,
    _get_sta_channel_band,
    _is_dfs_channel,
    _run_nmcli_hotspot,
    _start_hostapd_ap_sta,
)
def cmd_start(args):
    try:
        with command_lock("start"):
            _cmd_start_impl(args)
    except RuntimeError as exc:
        err(str(exc))
        sys.exit(1)


def _cmd_start_impl(args):
    config = load_config()
    dbg("Loaded start config", active_profile=config.get("active_profile"), interface=config.get("interface"))
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
    dbg(
        "Hardware capability resolved",
        interface=target.name,
        supports_ap=cap.supports_ap,
        supports_ap_sta_concurrent=cap.supports_ap_sta_concurrent,
        supports_ap_sta_split=cap.supports_ap_sta_split,
    )
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
        dbg("Trying strategy", strategy="nmcli-concurrent", interface=target.name)
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
        dbg("Trying strategy", strategy="hostapd-split", interface=target.name)

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
    dbg("Trying strategy", strategy="nmcli-force", interface=target.name)
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

