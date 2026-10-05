"""hostapd configuration rendering and control-socket helpers."""

from __future__ import annotations

import time
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
    hidden: bool = False
    accept_file: Optional[str] = None  # set: only the MACs listed there may join


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
        # 1: beacons carry an empty SSID; clients must know the name to join.
        f"ignore_broadcast_ssid={1 if cfg.hidden else 0}",
    ]
    if cfg.accept_file:
        lines += ["macaddr_acl=1", f"accept_mac_file={cfg.accept_file}"]
    else:
        lines.append("macaddr_acl=0")
    lines += [
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


def parse_state(text: str) -> Optional[str]:
    for line in text.splitlines():
        if line.startswith("state="):
            return line.split("=", 1)[1].strip()
    return None


def wait_enabled(ap_iface: str, timeout: float = 10.0) -> bool:
    """Wait until hostapd reports ``state=ENABLED`` (it is beaconing)."""
    deadline = time.monotonic() + timeout
    while True:
        if parse_state(_cli(ap_iface, "status").stdout) == "ENABLED":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


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


def render_accept(macs: List[str]) -> str:
    return "".join(f"{m}\n" for m in macs)


def deny(ap_iface: str, mac: str) -> bool:
    # hostapd checks the accept list before the deny list, so an allowlisted
    # device must leave the accept list too or the block wouldn't hold.
    _cli(ap_iface, "accept_acl", "DEL_MAC", mac)
    return _ok(_cli(ap_iface, "deny_acl", "ADD_MAC", mac))


def allow(ap_iface: str, mac: str, allowlisted: bool = False) -> bool:
    if allowlisted:
        _cli(ap_iface, "accept_acl", "ADD_MAC", mac)
    return _ok(_cli(ap_iface, "deny_acl", "DEL_MAC", mac))
