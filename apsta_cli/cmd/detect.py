#!/usr/bin/env python3
"""Hardware detect command implementation."""

import json
import sys

from ..common import C, err, head, info, ok, warn
from ..hardware import get_hardware_capability, get_wifi_interfaces, scan_usb_wifi
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


