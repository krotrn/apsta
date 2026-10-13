"""Validation for every user-supplied value that ends up in a config file or command."""

from __future__ import annotations

import re
from typing import List, Optional

from ..core.errors import UsageError

_IFACE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,15}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_TRUE, _FALSE = ("yes", "true", "on", "1"), ("no", "false", "off", "0")
BANDS = ("bg", "a")
# "auto" picks the best method; the rest name a strategy (see net.strategies).
METHODS = ("auto", "hostapd", "nmcli", "p2p", "nmcli-single")


def ssid(value: str) -> str:
    raw = value.encode("utf-8")
    if not 1 <= len(raw) <= 32:
        raise UsageError("SSID must be 1–32 bytes.")
    if any(ch in value for ch in ("\n", "\r", "\0")):
        raise UsageError("SSID cannot contain newlines or NUL characters.")
    return value


def password(value: str) -> str:
    """WPA2-PSK: 8–63 printable ASCII characters, or a 64-hex-digit raw PSK."""
    if _HEX64_RE.match(value):
        return value
    if not 8 <= len(value) <= 63:
        raise UsageError("WPA2 password must be 8–63 characters.")
    if any(not (0x20 <= ord(ch) <= 0x7E) for ch in value):
        raise UsageError("WPA2 password must use printable ASCII characters only.")
    return value


def band(value: str) -> str:
    if value not in BANDS:
        raise UsageError("band must be 'bg' (2.4 GHz) or 'a' (5 GHz).")
    return value


def channel(value: str) -> str:
    if value.strip().lower() == "auto":
        return "auto"
    if not value.isdigit() or not 1 <= int(value) <= 196:
        raise UsageError("channel must be 'auto' or a number between 1 and 196.")
    return str(int(value))


def method(value: str) -> str:
    value = value.strip().lower()
    if value == "nmcli-force":  # name used by apsta <= 0.6
        value = "nmcli-single"
    if value not in METHODS:
        raise UsageError(f"method must be one of: {', '.join(METHODS)}.")
    return value


def interface(value: Optional[str]) -> Optional[str]:
    if value is None or value.lower() in ("", "none", "null", "auto"):
        return None
    if not _IFACE_RE.match(value):
        raise UsageError(f"Invalid interface name: {value!r}")
    return value


def flag(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise UsageError(f"Expected yes or no, got {value!r}.")


def mac(value: str) -> str:
    normalized = value.strip().lower().replace("-", ":")
    if not _MAC_RE.match(normalized):
        raise UsageError(f"Invalid MAC address: {value!r}", hints=["Use the form aa:bb:cc:dd:ee:ff"])
    return normalized


def mac_list(value: str) -> Optional[List[str]]:
    """Comma- or space-separated MACs; an empty list means "no allowlist"."""
    macs: List[str] = []
    for item in re.split(r"[,\s]+", value.strip()):
        if item and mac(item) not in macs:
            macs.append(mac(item))
    return macs or None


def profile_name(value: str) -> str:
    value = value.strip()
    if not _PROFILE_RE.match(value):
        raise UsageError("Profile names may use letters, digits, '.', '_' and '-' (max 32).")
    return value


VALIDATORS = {
    "ssid": ssid,
    "password": password,
    "band": band,
    "channel": channel,
    "method": method,
    "interface": interface,
    "hidden": flag,
    "allowed_macs": mac_list,
}


def profile_value(key: str, value: Optional[str]):
    if key not in VALIDATORS:
        raise UsageError(f"Unknown profile key: {key}")
    if value is None or (key != "interface" and value.lower() in ("none", "null", "")):
        if key in ("ssid", "band"):
            raise UsageError(f"{key} cannot be empty.")
        return {"hidden": False, "channel": "auto", "method": "auto"}.get(key)
    return VALIDATORS[key](value)
