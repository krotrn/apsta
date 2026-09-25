#!/usr/bin/env python3
"""Hotspot stop command implementation."""

import sys

from ..common import command_lock, dbg, err, head, load_config, ok, require_root, run, run_cmd, run_out, save_config, warn
from .support import _stop_hostapd_ap_sta
def cmd_stop(args):
    require_root()
    try:
        with command_lock("stop"):
            _cmd_stop_impl(args)
    except RuntimeError as exc:
        err(str(exc))
        sys.exit(1)


def _cmd_stop_impl(args):
    head("apsta — Stopping Hotspot")
    print()

    config = load_config()
    method = config.get("start_method")
    dbg("Stopping hotspot", method=method, ap_interface=config.get("ap_interface"))

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
        result = run_cmd(["nmcli", "connection", "down", con_name])
        if result.returncode == 0:
            ok(f"Hotspot connection '{con_name}' stopped.")
        else:
            warn(f"Could not bring down '{con_name}', scanning for active hotspot connections...")
            active = run_out("nmcli -t -f NAME,TYPE,DEVICE,STATE con show --active")
            hotspot_cons = [
                line.split(":")[0] for line in active.splitlines()
                if "802-11-wireless" in line and "hotspot" in line.split(":")[0].lower()
            ]
            if hotspot_cons:
                for con in hotspot_cons:
                    run_cmd(["nmcli", "connection", "down", con])
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
