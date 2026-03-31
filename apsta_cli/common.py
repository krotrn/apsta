#!/usr/bin/env python3
"""Shared constants and utilities for the apsta CLI."""

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import List, Optional
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
    "active_profile": "default",
    "profiles": {
        "default": {
            "ssid": "apsta-hotspot",
            "password": "changeme123",
            "band": "bg",
            "channel": "11",
            "interface": None,
        }
    },
}

PROFILE_KEYS = ("ssid", "password", "band", "channel", "interface")

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


def _normalize_profile_values(values: dict) -> dict:
    profile = {}
    defaults = DEFAULT_CONFIG["profiles"]["default"]
    for key in PROFILE_KEYS:
        val = values.get(key, defaults[key])
        if key == "interface" and isinstance(val, str) and val.lower() in ("", "none", "null"):
            val = None
        profile[key] = val
    return profile


def normalize_config(config: dict) -> dict:
    """Normalize config to include profile-aware keys while preserving compatibility."""
    normalized = deepcopy(DEFAULT_CONFIG)
    normalized.update(config or {})

    # Legacy files (without profiles) are migrated into the default profile.
    provided_profiles = (config or {}).get("profiles") if isinstance(config, dict) else None
    profiles = provided_profiles if isinstance(provided_profiles, dict) else None
    if not profiles:
        profiles = {
            "default": {
                key: normalized.get(key)
                for key in PROFILE_KEYS
            }
        }

    cleaned_profiles = {}
    for name, values in profiles.items():
        if not isinstance(name, str) or not name.strip():
            continue
        source = values if isinstance(values, dict) else {}
        cleaned_profiles[name.strip()] = _normalize_profile_values(source)

    if not cleaned_profiles:
        cleaned_profiles["default"] = _normalize_profile_values({
            key: normalized.get(key)
            for key in PROFILE_KEYS
        })

    active = normalized.get("active_profile")
    if not isinstance(active, str) or active not in cleaned_profiles:
        active = "default" if "default" in cleaned_profiles else sorted(cleaned_profiles.keys())[0]

    normalized["profiles"] = cleaned_profiles
    normalized["active_profile"] = active

    # Keep top-level keys in sync for older code paths and UI readers.
    active_profile = cleaned_profiles[active]
    for key in PROFILE_KEYS:
        normalized[key] = active_profile.get(key)

    return normalized


def list_profile_names(config: dict) -> list:
    return sorted((config.get("profiles") or {}).keys())


def get_active_profile_name(config: dict) -> str:
    return (config.get("active_profile") or "default")


def get_active_profile(config: dict) -> dict:
    active = get_active_profile_name(config)
    return dict((config.get("profiles") or {}).get(active, {}))


def set_active_profile(config: dict, profile_name: str) -> bool:
    profiles = config.get("profiles") or {}
    if profile_name not in profiles:
        return False
    config["active_profile"] = profile_name
    synced = normalize_config(config)
    config.clear()
    config.update(synced)
    return True


def set_profile_field(config: dict, key: str, value):
    active = get_active_profile_name(config)
    profiles = config.get("profiles") or {}
    if active not in profiles:
        profiles[active] = _normalize_profile_values({})
    if key == "interface" and isinstance(value, str) and value.lower() in ("", "none", "null"):
        value = None
    profiles[active][key] = value
    config["profiles"] = profiles
    synced = normalize_config(config)
    config.clear()
    config.update(synced)


def create_profile(config: dict, profile_name: str, from_profile: Optional[str] = None) -> bool:
    profiles = config.get("profiles") or {}
    if profile_name in profiles:
        return False
    source_name = from_profile or get_active_profile_name(config)
    source = profiles.get(source_name)
    if not source:
        return False
    profiles[profile_name] = _normalize_profile_values(source)
    config["profiles"] = profiles
    synced = normalize_config(config)
    config.clear()
    config.update(synced)
    return True


def delete_profile(config: dict, profile_name: str) -> bool:
    profiles = config.get("profiles") or {}
    if profile_name not in profiles:
        return False
    if profile_name == "default":
        return False
    if profile_name == get_active_profile_name(config):
        return False
    del profiles[profile_name]
    config["profiles"] = profiles
    synced = normalize_config(config)
    config.clear()
    config.update(synced)
    return True

# ── Config I/O ─────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
            return normalize_config(saved)
        except json.JSONDecodeError:
            warn(f"Config file {CONFIG_PATH} is corrupted. Using defaults.")
    return normalize_config({})

def save_config(config: dict):
    config = normalize_config(config)
    CONFIG_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    CONFIG_PATH.chmod(0o644)

SCRIPT_DIR = Path(__file__).resolve().parent.parent
