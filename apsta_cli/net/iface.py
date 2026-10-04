"""Virtual AP interfaces and addressing."""

from __future__ import annotations

import secrets
import time
from typing import Tuple

from ..core import output, paths, shell
from ..hw.interfaces import iface_type


def ap_name(base_iface: str) -> str:
    # Interface names max out at 15 chars (IFNAMSIZ - 1); USB adapters are
    # often already 15 (e.g. wlx00c0ca123456).
    return f"{base_iface[:12]}_ap"


def random_mac() -> str:
    """Random locally administered, unicast MAC (first octet 0x02)."""
    return "02:" + ":".join(f"{b:02x}" for b in secrets.token_bytes(5))


def exists(name: str) -> bool:
    return (paths.SYSFS_NET / name).exists()


def delete(name: str) -> shell.Result:
    return shell.run(["iw", "dev", name, "del"])


def create_virtual_ap(base_iface: str) -> Tuple[str, str]:
    """Create ``<base>_ap`` with its own MAC; the base keeps its MAC so NM keeps the STA link."""
    name = ap_name(base_iface)
    if exists(name):
        delete(name)
    shell.run(["iw", "dev", base_iface, "interface", "add", name, "type", "__ap"]).check(
        f"Creating virtual interface {name}"
    )
    mac = random_mac()
    shell.run(["ip", "link", "set", name, "down"])
    if not shell.run(["ip", "link", "set", name, "address", mac]).ok:
        output.warn(f"Could not set a separate MAC on {name}; some drivers reject duplicate MACs.")
    return name, mac


def wait_for_ap_mode(name: str, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if iface_type(name) == "AP":
            return True
        time.sleep(0.5)
    return iface_type(name) == "AP"


def assign_address(name: str, cidr: str) -> None:
    shell.run(["ip", "addr", "flush", "dev", name])
    shell.run(["ip", "addr", "add", cidr, "dev", name]).check(f"Assigning {cidr} to {name}")
    shell.run(["ip", "link", "set", name, "up"])
