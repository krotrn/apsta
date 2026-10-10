"""What this machine's WiFi hardware can do, and which methods apsta can use on it.

Used by ``apsta detect`` and ``apsta recommend``; the commands only present it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..core import shell
from ..core.errors import HardwareError
from ..hw import capability, interfaces, usb
from ..hw.capability import HardwareCapability
from ..hw.interfaces import WifiInterface
from ..net import channels, wpa


def target_interface(ifaces: List[WifiInterface]) -> WifiInterface:
    """The interface to report on: the connected one, else one that is up, else the first."""
    connected = next((i for i in ifaces if i.connected_ssid), None)
    return connected or next((i for i in ifaces if i.state == "UP"), ifaces[0])


def verdict(cap: HardwareCapability, sta_freq: Optional[int] = None, p2p_ready: bool = False) -> dict:
    if cap.ap_sta:
        messages = ["Your card can run a hotspot while staying connected to WiFi."]
        if cap.same_channel_required:
            messages.append("The hotspot will use the same channel as your WiFi connection.")
            if p2p_ready:
                messages.append(
                    "Where that channel can't host, it runs as a Wi-Fi Direct group on its own channel instead."
                )
                return {"level": "ok", "mode": "ap+sta", "messages": messages, "next": "sudo apsta start"}
            sta = channels.from_freq(sta_freq) if sta_freq else None
            allowed = channels.allowed_channels(cap.ap_frequencies)
            if sta is not None and allowed is not None and sta not in allowed:
                return {
                    "level": "warn",
                    "mode": "ap+sta",
                    "messages": messages,
                    "warnings": [
                        f"Your WiFi is on {sta.label} channel {sta.number}, where this card can't host.",
                        "Switch that network to 2.4 GHz, or connect to a 2.4 GHz network, then start.",
                        f"Why: {channels.DOCS_5GHZ}",
                    ],
                    "next": "sudo apsta start  (after switching to 2.4 GHz)",
                }
        return {"level": "ok", "mode": "ap+sta", "messages": messages, "next": "sudo apsta start"}
    if cap.supports_ap:
        return {
            "level": "warn",
            "mode": "single",
            "messages": [
                "Your card supports AP mode but not alongside a WiFi connection.",
                "Starting a hotspot will disconnect your WiFi.",
            ],
            "next": "sudo apsta start --allow-disconnect",
        }
    return {
        "level": "error",
        "mode": "unsupported",
        "messages": ["Your card does not support AP mode.", "A USB WiFi adapter is required."],
        "next": "apsta recommend",
    }


def methods(cap: Optional[HardwareCapability] = None) -> Dict[str, str]:
    """Each hotspot method: "ready", or what it still needs."""
    missing = [b for b in ("hostapd", "dnsmasq") if not shell.have(b)]
    found = {
        "hostapd": "ready" if not missing else f"needs {', '.join(missing)}",
        "nmcli": "ready" if shell.have("nmcli") else "needs NetworkManager",
    }
    if cap is not None and cap.p2p_go_own_channel:
        if not shell.have("dnsmasq"):
            found["p2p"] = "needs dnsmasq"
        elif wpa.connect(cap.interface) is None:
            found["p2p"] = (
                "needs python3-jeepney (wpa_supplicant has no control socket here)"
                if not wpa.jeepney_installed()
                else "needs wpa_supplicant (NetworkManager may be using iwd)"
            )
        else:
            found["p2p"] = "ready"
    return found


@dataclass
class Report:
    ifaces: List[WifiInterface]
    target: WifiInterface
    cap: HardwareCapability
    methods: Dict[str, str]
    verdict: dict

    def to_dict(self) -> dict:
        """The ``detect --json`` document (a public interface, see docs/json-output.md)."""
        return {
            "interfaces": [interfaces.to_json(i) for i in self.ifaces],
            "target_interface": self.target.name,
            "capability": self.cap.to_dict(),
            "methods": self.methods,
            "verdict": self.verdict,
        }


def report() -> Report:
    ifaces = interfaces.client_interfaces()
    if not ifaces:
        raise HardwareError("No WiFi interfaces found.")
    target = target_interface(ifaces)
    cap = capability.probe(target.name)
    link = interfaces.sta_link(target.name)
    available = methods(cap)
    result = verdict(cap, link.freq if link else None, available.get("p2p") == "ready")
    return Report(ifaces, target, cap, available, result)


def builtin_supports_ap_sta() -> bool:
    ifaces = interfaces.client_interfaces()
    return bool(ifaces) and capability.probe(target_interface(ifaces).name).ap_sta


def capable_usb_adapters() -> List[Tuple[usb.UsbWifiDevice, usb.UsbChipset]]:
    """Plugged-in USB adapters whose chipset is known to do AP+STA, with that chipset."""
    return [(d, d.chipset_db) for d in usb.scan_usb_wifi() if d.chipset_db and d.chipset_db.ap_sta]
