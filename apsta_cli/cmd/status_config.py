#!/usr/bin/env python3
"""Status and config command implementations."""

import json
import sys

from ..common import (
    C,
    CONFIG_PATH,
    DEFAULT_CONFIG,
    DNSMASQ_LEASES,
    PROFILE_KEYS,
    create_profile,
    delete_profile,
    err,
    get_active_profile,
    get_active_profile_name,
    head,
    info,
    list_profile_names,
    load_config,
    ok,
    require_root,
    run_out,
    save_config,
    set_active_profile,
    set_profile_field,
)
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
                "active_profile": get_active_profile_name(config),
                "profiles": list_profile_names(config),
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

        if key == "profiles":
            err("Direct edits to 'profiles' are not supported.")
            info("Use: apsta profile list|show|use|create|delete")
            sys.exit(1)

        if key == "active_profile":
            if not val:
                err("Profile name cannot be empty.")
                sys.exit(1)
            if not set_active_profile(config, val):
                err(f"Profile not found: {val}")
                info("See profiles with:  apsta profile list")
                sys.exit(1)
            save_config(config)
            ok(f"Switched active profile to: {val}")
        elif key in PROFILE_KEYS:
            if val.lower() in ("none", "null", ""):
                set_profile_field(config, key, None)
                save_config(config)
                ok(f"Cleared {key} for profile '{get_active_profile_name(config)}'")
            else:
                set_profile_field(config, key, val)
                save_config(config)
                ok(f"Set {key} = {val} for profile '{get_active_profile_name(config)}'")
        else:
            if val.lower() in ("none", "null", ""):
                config[key] = None
            else:
                config[key] = val
            save_config(config)
            ok(f"Set {key} = {val}")
    else:
        active_profile = get_active_profile_name(config)
        active_values = get_active_profile(config)
        info(f"Config file: {CONFIG_PATH}")
        info(f"Active profile: {C.BOLD}{active_profile}{C.RESET}")
        info(f"Profiles: {', '.join(list_profile_names(config))}")
        print()
        for k in PROFILE_KEYS:
            v = active_values.get(k)
            display = f"{C.YELLOW}{v}{C.RESET}" if v else f"{C.DIM}(auto){C.RESET}"
            print(f"     {k:<20} {display}")
        print()
        info("Change with:  apsta config --set ssid=MyHotspot")

    print()


def cmd_profile(args):
    head("apsta — Profiles")
    config = load_config()
    action = getattr(args, "action", None) or "list"

    if action == "list":
        active = get_active_profile_name(config)
        print()
        info("Available profiles:")
        for name in list_profile_names(config):
            marker = f"{C.GREEN}*{C.RESET}" if name == active else " "
            print(f"   {marker} {name}")
        print()
        info("Use:  apsta profile use <name>")
        print()
        return

    if action == "show":
        name = args.name or get_active_profile_name(config)
        profiles = config.get("profiles") or {}
        if name not in profiles:
            err(f"Profile not found: {name}")
            sys.exit(1)
        print()
        info(f"Profile: {C.BOLD}{name}{C.RESET}")
        values = profiles[name]
        for key in PROFILE_KEYS:
            value = values.get(key)
            display = f"{C.YELLOW}{value}{C.RESET}" if value else f"{C.DIM}(auto){C.RESET}"
            print(f"     {key:<20} {display}")
        print()
        return

    require_root()

    if action == "use":
        if not set_active_profile(config, args.name):
            err(f"Profile not found: {args.name}")
            sys.exit(1)
        save_config(config)
        ok(f"Switched active profile to: {args.name}")
        print()
        return

    if action == "create":
        name = args.name.strip()
        if not name:
            err("Profile name cannot be empty.")
            sys.exit(1)
        if not create_profile(config, name, args.from_profile):
            err(f"Could not create profile '{name}'.")
            info("Possible causes: name already exists or source profile is invalid.")
            sys.exit(1)
        save_config(config)
        ok(f"Created profile: {name}")
        print()
        return

    if action == "delete":
        if not delete_profile(config, args.name):
            err(f"Cannot delete profile: {args.name}")
            info("You cannot delete the default or currently active profile.")
            sys.exit(1)
        save_config(config)
        ok(f"Deleted profile: {args.name}")
        print()
        return

    err(f"Unsupported profile action: {action}")
    sys.exit(2)


