"""Small pure helpers for the GTK UI (no GTK imports, unit-testable)."""

from __future__ import annotations

from typing import List

APP_ID = "com.github.apsta.Gtk"
POLL_INTERVAL = 5  # seconds between status refreshes


def escape_wifi_field(value: str) -> str:
    for ch in ("\\", ";", ",", ":", '"'):
        value = value.replace(ch, "\\" + ch)
    return value


def wifi_share_string(ssid: str, password: str) -> str:
    """The ``WIFI:`` payload phone cameras understand for joining a network."""
    if not ssid or not password:
        return ""
    return f"WIFI:T:WPA;S:{escape_wifi_field(ssid)};P:{escape_wifi_field(password)};;"


def format_clients(clients: List[dict]) -> str:
    if not clients:
        return "No clients connected."
    lines = [f"{'HOSTNAME':<20} {'MAC':<18} {'IP':<16} LIMIT", "-" * 64]
    for c in clients:
        limit = f"{c['limit_kbps']} Kbps" if c.get("limit_kbps") else "-"
        lines.append(f"{(c.get('hostname') or '-')[:20]:<20} {c.get('mac', '-'):<18} {c.get('ip') or '-':<16} {limit}")
    return "\n".join(lines)


def band_label(band: str) -> str:
    return "5 GHz" if band == "a" else "2.4 GHz"
