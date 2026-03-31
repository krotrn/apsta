#!/usr/bin/env python3
"""Status and config command implementations."""

import json
import sys
from typing import Dict, List, Optional, Tuple

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
    run_cmd,
    run_out,
    save_config,
    set_active_profile,
    set_profile_field,
    warn,
)
from ..hardware import get_wifi_interfaces


def _read_hostapd_clients() -> List[Dict[str, str]]:
    clients: List[Dict[str, str]] = []
    if not DNSMASQ_LEASES.exists():
        return clients

    try:
        leases = DNSMASQ_LEASES.read_text().strip()
    except OSError:
        return clients

    for line in leases.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        hostname = "" if parts[3] == "*" else parts[3]
        clients.append(
            {
                "hostname": hostname,
                "mac": parts[1],
                "ip": parts[2],
            }
        )
    return clients


def _find_client(clients: List[Dict[str, str]], identifier: str) -> Optional[Dict[str, str]]:
    needle = identifier.strip().lower()
    if not needle:
        return None

    for client in clients:
        mac = (client.get("mac") or "").strip().lower()
        ip = (client.get("ip") or "").strip().lower()
        hostname = (client.get("hostname") or "").strip().lower()
        if needle in {mac, ip, hostname}:
            return client
    return None


def _hostapd_ok(result) -> bool:
    output = f"{result.stdout or ''}\n{result.stderr or ''}".upper()
    return result.returncode == 0 and "FAIL" not in output


def _disconnect_client(ap_iface: str, mac: str) -> bool:
    # Prefer hostapd_cli in hostapd mode; fallback to iw station delete.
    cmd_sets = [
        ["hostapd_cli", "-i", ap_iface, "disassociate", mac],
        ["hostapd_cli", "-i", ap_iface, "deauthenticate", mac],
    ]
    for cmd in cmd_sets:
        result = run_cmd(cmd)
        if _hostapd_ok(result):
            return True

    return run_cmd(["iw", "dev", ap_iface, "station", "del", mac]).returncode == 0


def _set_client_bandwidth_limit(ap_iface: str, mac: str, kbps: int) -> Tuple[bool, str]:
    if kbps <= 0:
        return False, "limit-kbps must be greater than 0"

    pref = 50000 + (sum(int(part, 16) for part in mac.split(":")) % 10000)

    # Ensure clsact exists; ignore if already configured.
    run_cmd(["tc", "qdisc", "add", "dev", ap_iface, "clsact"])

    # Replace existing filters for this client rule id.
    run_cmd(["tc", "filter", "del", "dev", ap_iface, "ingress", "pref", str(pref)])
    run_cmd(["tc", "filter", "del", "dev", ap_iface, "egress", "pref", str(pref)])

    ingress = run_cmd(
        [
            "tc", "filter", "add", "dev", ap_iface, "ingress", "pref", str(pref), "protocol", "all",
            "flower", "src_mac", mac,
            "action", "police", "rate", f"{kbps}kbit", "burst", "64k", "conform-exceed", "drop",
        ]
    )
    if ingress.returncode != 0:
        msg = (ingress.stderr or ingress.stdout or "tc ingress filter failed").strip()
        return False, msg

    egress = run_cmd(
        [
            "tc", "filter", "add", "dev", ap_iface, "egress", "pref", str(pref), "protocol", "all",
            "flower", "dst_mac", mac,
            "action", "police", "rate", f"{kbps}kbit", "burst", "64k", "conform-exceed", "drop",
        ]
    )
    if egress.returncode != 0:
        msg = (egress.stderr or egress.stdout or "tc egress filter failed").strip()
        return False, msg

    return True, ""


def cmd_status(args):
    config = load_config()
    method = config.get("start_method")

    if getattr(args, "use_profile", None):
        require_root()
        name = args.use_profile.strip()
        if not name:
            err("Profile name cannot be empty.")
            sys.exit(1)
        if not set_active_profile(config, name):
            err(f"Profile not found: {name}")
            info("See profiles with:  apsta profile list")
            sys.exit(1)
        save_config(config)
        ok(f"Switched active profile to: {name}")
        print()
        return

    if getattr(args, "limit_client", None) or getattr(args, "limit_kbps", None):
        require_root()
        if not args.limit_client or args.limit_kbps is None:
            err("Both --limit-client and --limit-kbps are required together.")
            sys.exit(1)
        if method != "hostapd":
            err("Client bandwidth limits are available only in hostapd mode.")
            sys.exit(1)

        ap_iface = config.get("ap_interface")
        if not ap_iface:
            err("No active AP interface found.")
            sys.exit(1)

        clients = _read_hostapd_clients()
        target = _find_client(clients, args.limit_client)
        if not target:
            err(f"Client not found: {args.limit_client}")
            sys.exit(1)

        mac = target["mac"]
        success, message = _set_client_bandwidth_limit(ap_iface, mac, int(args.limit_kbps))
        if success:
            ok(f"Applied {args.limit_kbps} Kbps limit for {mac}")
            print()
            return

        err(f"Failed to apply client limit: {message}")
        info("This operation requires tc flower support in your kernel and driver.")
        sys.exit(1)

    if getattr(args, "disconnect", None):
        require_root()
        if method != "hostapd":
            err("Client disconnect is available only in hostapd mode.")
            sys.exit(1)

        ap_iface = config.get("ap_interface")
        if not ap_iface:
            err("No active AP interface found.")
            sys.exit(1)

        clients = _read_hostapd_clients()
        target = _find_client(clients, args.disconnect)
        if not target:
            err(f"Client not found: {args.disconnect}")
            if clients:
                info("Connected clients:")
                for client in clients:
                    host = client.get("hostname") or "(no hostname)"
                    print(f"     {host:<20} {client['mac']}  {client['ip']}")
            sys.exit(1)

        mac = target["mac"]
        if _disconnect_client(ap_iface, mac):
            ok(f"Disconnected client {mac}")
            print()
            return

        err(f"Failed to disconnect client {mac}")
        sys.exit(1)

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

        clients = _read_hostapd_clients() if method == "hostapd" else []

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

    if getattr(args, "clients", False):
        head("apsta — Connected Clients")
        print()
        if method != "hostapd":
            warn("Client listing is available only in hostapd mode.")
            print()
            return
        clients = _read_hostapd_clients()
        if not clients:
            info("No clients connected.")
            print()
            return
        info("Connected clients:")
        for client in clients:
            host = client.get("hostname") or "(no hostname)"
            print(f"     {host:<20} [{client['mac']}]  {client['ip']}")
        print()
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
    if method == "hostapd":
        print()
        info("Connected clients:")
        clients = _read_hostapd_clients()
        if clients:
            for client in clients:
                host = client.get("hostname") or "(no hostname)"
                print(f"     {host}  [{client['mac']}]  {client['ip']}")
        else:
            print(f"     {C.DIM}No clients connected{C.RESET}")

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


