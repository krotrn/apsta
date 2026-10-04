"""hostapd configuration rendering and control-socket helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..core import paths, shell
from .channels import Channel


@dataclass
class HostapdConfig:
    interface: str
    ssid: str
    password: str
    channel: Channel
    country: Optional[str]
    ctrl_dir: str


def render(cfg: HostapdConfig) -> str:
    """hostapd.conf text. Pure function so the output is unit-testable."""
    hw_mode = "a" if cfg.channel.band == "a" else "g"
    psk_line = f"wpa_psk={cfg.password.lower()}" if len(cfg.password) == 64 else f"wpa_passphrase={cfg.password}"
    lines = [
        f"interface={cfg.interface}",
        "driver=nl80211",
        f"ctrl_interface={cfg.ctrl_dir}",
        "ctrl_interface_group=0",
        # Hex-encoded SSID: no escaping pitfalls for any byte the user picked.
        f"ssid2={cfg.ssid.encode('utf-8').hex()}",
        "utf8_ssid=1",
        f"hw_mode={hw_mode}",
        f"channel={cfg.channel.number}",
        # 802.11n/ac + WMM: without these clients are stuck at legacy 54 Mbps.
        "ieee80211n=1",
        "wmm_enabled=1",
    ]
    if hw_mode == "a":
        lines.append("ieee80211ac=1")
    if cfg.country:
        # Without a country code the world domain marks most 5 GHz channels no-IR.
        lines += [f"country_code={cfg.country}", "ieee80211d=1"]
    lines += [
        "auth_algs=1",
        "ignore_broadcast_ssid=0",
        "macaddr_acl=0",
        "wpa=2",
        "wpa_key_mgmt=WPA-PSK",
        "rsn_pairwise=CCMP",
        psk_line,
    ]
    return "\n".join(lines) + "\n"


def _cli(ap_iface: str, *args: str) -> shell.Result:
    return shell.run(["hostapd_cli", "-p", str(paths.HOSTAPD_CTRL_DIR), "-i", ap_iface, *args])


def _ok(result: shell.Result) -> bool:
    return result.ok and "FAIL" not in result.stdout.upper()


def parse_all_sta(text: str) -> List[str]:
    """Station MACs from ``hostapd_cli all_sta`` output."""
    macs = []
    for line in text.splitlines():
        line = line.strip().lower()
        if len(line) == 17 and line.count(":") == 5:
            macs.append(line)
    return macs


def stations(ap_iface: str) -> Optional[List[str]]:
    result = _cli(ap_iface, "all_sta")
    return parse_all_sta(result.stdout) if result.ok else None


def deauthenticate(ap_iface: str, mac: str) -> bool:
    return _ok(_cli(ap_iface, "deauthenticate", mac))


def deny(ap_iface: str, mac: str) -> bool:
    return _ok(_cli(ap_iface, "deny_acl", "ADD_MAC", mac))


def allow(ap_iface: str, mac: str) -> bool:
    return _ok(_cli(ap_iface, "deny_acl", "DEL_MAC", mac))
