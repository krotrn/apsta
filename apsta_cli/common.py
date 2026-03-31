#!/usr/bin/env python3
"""Shared constants and utilities for the apsta CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List
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

SCRIPT_DIR = Path(__file__).resolve().parent.parent
