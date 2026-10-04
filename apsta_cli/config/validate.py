"""Validation for every user-supplied value that ends up in a config file or command."""

from __future__ import annotations

import re
from typing import Optional

from ..core.errors import UsageError

_IFACE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,15}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
BANDS = ("bg", "a")


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
    if not value.isdigit() or not 1 <= int(value) <= 196:
        raise UsageError("channel must be a number between 1 and 196.")
    return str(int(value))


def interface(value: Optional[str]) -> Optional[str]:
    if value is None or value.lower() in ("", "none", "null", "auto"):
        return None
    if not _IFACE_RE.match(value):
        raise UsageError(f"Invalid interface name: {value!r}")
    return value


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
    "interface": interface,
}


def profile_value(key: str, value: Optional[str]):
    if key not in VALIDATORS:
        raise UsageError(f"Unknown profile key: {key}")
    if value is None or (key != "interface" and value.lower() in ("none", "null", "")):
        if key in ("ssid", "band"):
            raise UsageError(f"{key} cannot be empty.")
        return None
    return VALIDATORS[key](value)
