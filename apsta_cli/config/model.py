"""The configuration document and pure operations on it (no I/O).

Shape on disk (config.json, world-readable)::

    {
      "active_profile": "default",
      "profiles": {"default": {"ssid": ..., "band": ..., "channel": ..., "method": ...,
                               "interface": ..., "hidden": false, "allowed_macs": null}}
    }

Passwords are stored separately (see :mod:`.store`). Runtime facts such as the
AP interface in use are *not* configuration and live in :mod:`apsta_cli.state`.
The top-level ``ssid``/``band``/... keys mirror the active profile so simple
readers don't need to understand profiles.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Optional

from ..core.errors import UsageError
from . import validate

PROFILE_KEYS = ("ssid", "password", "band", "channel", "method", "interface", "hidden", "allowed_macs")

DEFAULT_PROFILE: Dict[str, Any] = {
    "ssid": "apsta-hotspot",
    "password": None,  # generated randomly on first start; never a shared default
    "band": "bg",
    # Used when the hotspot isn't tied to the WiFi's channel; "auto" = least crowded nearby.
    "channel": "auto",
    "method": "auto",  # or force one: hostapd, nmcli, p2p, nmcli-single
    "interface": None,  # auto-detect
    "hidden": False,  # don't broadcast the network name
    "allowed_macs": None,  # None: anyone with the password; else only these devices
}

# Passwords shipped as defaults by apsta <= 0.6. Anyone in radio range could
# know them, so they are treated as "no password set".
INSECURE_PASSWORDS = frozenset({"changeme123"})

# Keys older versions kept in config.json that are runtime state, not config.
_LEGACY_RUNTIME_KEYS = ("ap_interface", "base_interface", "active_con_name", "start_method")


def _normalize_profile(values: dict) -> dict:
    profile = {}
    for key in PROFILE_KEYS:
        val = values.get(key, DEFAULT_PROFILE[key])
        if key == "interface" and isinstance(val, str) and val.lower() in ("", "none", "null"):
            val = None
        if key == "password" and val in INSECURE_PASSWORDS:
            val = None
        if key == "channel" and val == "6" and "method" not in values:
            # apsta <= 0.8 stored "6" as a fallback nobody chose and overrode it with a
            # scan; profiles from then (no "method" key yet) mean "pick for me".
            val = "auto"
        if key in ("hidden", "allowed_macs", "channel", "method"):
            val = _coerce(key, val)
        profile[key] = val
    return profile


def _coerce(key: str, val):
    """Hand-edited files may hold strings for these; fall back to the default if invalid."""
    try:
        if key == "hidden":
            return val if isinstance(val, bool) else validate.flag(str(val))
        if key in ("channel", "method"):
            return validate.VALIDATORS[key](str(val)) if val not in (None, "") else DEFAULT_PROFILE[key]
        if isinstance(val, list):
            val = ",".join(str(v) for v in val)
        return validate.mac_list(val) if isinstance(val, str) else None
    except UsageError:
        return DEFAULT_PROFILE[key]


def normalize(raw: Optional[dict]) -> dict:
    """Return a complete, canonical config from whatever was on disk."""
    raw = raw if isinstance(raw, dict) else {}
    profiles_in = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else None
    if not profiles_in:
        # Pre-profile configs kept the values at the top level.
        profiles_in = {"default": {k: raw[k] for k in PROFILE_KEYS if k in raw}}

    profiles = {}
    for name, values in profiles_in.items():
        if isinstance(name, str) and name.strip():
            profiles[name.strip()] = _normalize_profile(values if isinstance(values, dict) else {})
    if not profiles:
        profiles["default"] = _normalize_profile({})

    active = raw.get("active_profile")
    if not isinstance(active, str) or active not in profiles:
        active = "default" if "default" in profiles else sorted(profiles)[0]

    config = {"active_profile": active, "profiles": profiles}
    config.update(profiles[active])
    return config


def had_legacy_runtime_keys(raw: dict) -> bool:
    return any(raw.get(k) is not None for k in _LEGACY_RUNTIME_KEYS)


def profile_names(config: dict) -> List[str]:
    return sorted(config["profiles"])


def active_name(config: dict) -> str:
    return config["active_profile"]


def active_profile(config: dict) -> dict:
    return dict(config["profiles"][config["active_profile"]])


def _resync(config: dict) -> dict:
    synced = normalize(config)
    config.clear()
    config.update(synced)
    return config


def use_profile(config: dict, name: str) -> None:
    if name not in config["profiles"]:
        raise UsageError(f"Profile not found: {name}", hints=["List profiles with: apsta profile list"])
    config["active_profile"] = name
    _resync(config)


def set_field(config: dict, key: str, value: Optional[str]) -> None:
    """Validate and set ``key`` on the active profile."""
    config["profiles"][config["active_profile"]][key] = validate.profile_value(key, value)
    _resync(config)


def create_profile(config: dict, name: str, from_profile: Optional[str] = None) -> None:
    name = validate.profile_name(name)
    if name in config["profiles"]:
        raise UsageError(f"Profile already exists: {name}")
    source = from_profile or config["active_profile"]
    if source not in config["profiles"]:
        raise UsageError(f"Source profile not found: {source}")
    config["profiles"][name] = deepcopy(config["profiles"][source])
    _resync(config)


def delete_profile(config: dict, name: str) -> None:
    if name not in config["profiles"]:
        raise UsageError(f"Profile not found: {name}")
    if name == "default":
        raise UsageError("The default profile cannot be deleted.")
    if name == config["active_profile"]:
        raise UsageError("Cannot delete the active profile.", hints=["Switch first: sudo apsta profile use default"])
    del config["profiles"][name]
    _resync(config)
