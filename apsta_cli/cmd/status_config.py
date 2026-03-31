#!/usr/bin/env python3
"""Status and config command implementations."""

import json
import sys

from ..common import C, CONFIG_PATH, DEFAULT_CONFIG, DNSMASQ_LEASES, err, head, info, load_config, ok, require_root, run_out, save_config
from ..hardware import get_wifi_interfaces
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


