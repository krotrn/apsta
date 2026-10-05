"""Pure helpers for the GTK UI (no GTK imports, unit-testable)."""

from __future__ import annotations

from typing import Optional

APP_ID = "com.github.apsta.Gtk"
POLL_INTERVAL = 5  # seconds between status refreshes

BANDS = [("bg", "2.4 GHz"), ("a", "5 GHz")]
# The ``method`` setting: (value, label, what it means).
METHODS = [
    ("auto", "Automatic", "Best for your card and network"),
    ("hostapd", "hostapd", "Same channel; fastest; device control"),
    ("nmcli", "NetworkManager", "Same channel; no blocking or allowlist"),
    ("p2p", "Wi-Fi Direct", "Own channel; shares the radio's speed"),
    ("nmcli-single", "Hotspot only", "Wi-Fi disconnects while it runs"),
]
# Channels people can pick; the card may still forbid some (apsta then says so).
CHANNELS = {
    "bg": [str(n) for n in range(1, 14)],
    "a": ["36", "40", "44", "48", "149", "153", "157", "161", "165"],
}


def channel_options(band: str) -> list:
    """(value, label) choices for the channel setting on ``band``, "auto" first."""
    return [("auto", "Automatic (least crowded)")] + [(c, f"Channel {c}") for c in CHANNELS.get(band, CHANNELS["bg"])]


def index_of(options: list, value) -> int:
    """Position of ``value`` among (value, ...) tuples, or 0 (the first, "automatic") if absent."""
    return next((i for i, option in enumerate(options) if option[0] == value), 0)


def escape_wifi_field(value: str) -> str:
    for ch in ("\\", ";", ",", ":", '"'):
        value = value.replace(ch, "\\" + ch)
    return value


def wifi_share_string(ssid: str, password: str, hidden: bool = False) -> str:
    """The ``WIFI:`` payload phone cameras understand; ``H:true`` for hidden networks."""
    if not ssid or not password:
        return ""
    tail = "H:true;" if hidden else ""
    return f"WIFI:T:WPA;S:{escape_wifi_field(ssid)};P:{escape_wifi_field(password)};{tail};"


def band_label(band: Optional[str]) -> str:
    return "5 GHz" if band == "a" else "2.4 GHz"


def format_rate(kbps: Optional[int]) -> str:
    if not kbps:
        return ""
    return f"{kbps / 1000:g} Mbit/s" if kbps >= 1000 else f"{kbps} kbit/s"


def client_title(client: dict) -> str:
    return client.get("hostname") or "Unknown device"


def client_subtitle(client: dict) -> str:
    parts = [client.get("ip") or "no address yet", client.get("mac", "")]
    if client.get("limit_kbps"):
        parts.append(f"limited to {format_rate(client['limit_kbps'])}")
    return " · ".join(p for p in parts if p)


def hero_text(data: dict) -> tuple:
    """(title, subtitle, icon) for the status hero."""
    hotspot = data.get("hotspot")
    if not data:
        return "Status unavailable", "Could not run apsta.", "dialog-warning-symbolic"
    if hotspot:
        count = len(data.get("clients") or [])
        devices = "1 device" if count == 1 else f"{count} devices"
        return "Hotspot is on", f"{hotspot['ssid']} · {devices} connected", "network-wireless-hotspot-symbolic"
    return "Hotspot is off", "Share this computer's internet over Wi-Fi.", "network-wireless-offline-symbolic"


def uplink(data: dict) -> Optional[str]:
    """SSID of the Wi-Fi connection the hotspot shares, if still connected."""
    hotspot = data.get("hotspot") or {}
    for iface in data.get("interfaces") or []:
        if iface.get("name") == hotspot.get("base_interface") and iface.get("connected_ssid"):
            return iface["connected_ssid"]
    return None


def capability_rows(detect: dict) -> list:
    """(title, subtitle, ok) rows describing the hardware report."""
    cap = detect.get("capability") or {}
    if not cap:
        return []
    rows = [
        ("Hotspot (AP mode)", "The card can act as an access point", cap.get("supports_ap")),
        ("Keep Wi-Fi while hosting", "Hotspot and Wi-Fi connection at the same time", cap.get("ap_sta")),
    ]
    if cap.get("ap_sta"):
        p2p_ready = (detect.get("methods") or {}).get("p2p") == "ready"
        own_channel = not cap.get("same_channel_required") or p2p_ready
        subtitle = "Hotspot may use a different channel than your Wi-Fi"
        if cap.get("same_channel_required") and own_channel:
            subtitle += " (as a Wi-Fi Direct group)"
        rows.append(("Independent channel", subtitle, bool(own_channel)))
    if "ap_frequencies" in cap:
        five_ghz = any(f >= 5000 for f in cap["ap_frequencies"])
        rows.append(("Hotspot on 5 GHz", "Some 5 GHz channels allow starting a network", five_ghz))
    for name, state in (detect.get("methods") or {}).items():
        label = {"hostapd": "hostapd (client management)", "p2p": "Wi-Fi Direct (own channel)"}.get(
            name, "NetworkManager"
        )
        rows.append((label, "Ready" if state == "ready" else state.capitalize(), state == "ready"))
    return rows
