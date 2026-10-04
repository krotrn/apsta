"""NetworkManager integration.

Hotspot connections are written as *volatile keyfiles* under
/run/NetworkManager/system-connections and loaded with ``nmcli connection
load``. Compared with ``nmcli device wifi hotspot ... password X`` this means:

* the password never appears on a command line (visible to every user in ps);
* the connection has a fixed, known name so stop never has to guess;
* nothing accumulates in /etc across reboots.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Optional

from ..core import fsutil, paths, shell
from .channels import Channel

CONNECTION_ID = "apsta-hotspot"


def _escape(value: str) -> str:
    """Escape a GKeyFile string value."""
    out = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if out.startswith(" "):
        out = "\\s" + out[1:]
    return out


def render_keyfile(
    *,
    interface: str,
    ssid: str,
    password: str,
    channel: Channel,
    cloned_mac: Optional[str] = None,
    connection_uuid: Optional[str] = None,
) -> str:
    # SSIDs are written as a byte list, the keyfile form that needs no escaping.
    ssid_bytes = ";".join(str(b) for b in ssid.encode("utf-8")) + ";"
    lines = [
        "[connection]",
        f"id={CONNECTION_ID}",
        f"uuid={connection_uuid or uuid.uuid4()}",
        "type=wifi",
        f"interface-name={interface}",
        "autoconnect=false",
        "",
        "[wifi]",
        "mode=ap",
        f"ssid={ssid_bytes}",
        f"band={channel.band}",
        f"channel={channel.number}",
    ]
    if cloned_mac:
        lines.append(f"cloned-mac-address={cloned_mac}")
    lines += [
        "",
        "[wifi-security]",
        "key-mgmt=wpa-psk",
        "proto=rsn;",
        "pairwise=ccmp;",
        "group=ccmp;",
        f"psk={_escape(password)}",
        "",
        "[ipv4]",
        "method=shared",
        "",
        "[ipv6]",
        "method=ignore",
        "",
    ]
    return "\n".join(lines)


def keyfile_path() -> Path:
    return paths.NM_RUNTIME_KEYFILE_DIR / f"{CONNECTION_ID}.nmconnection"


def install_connection(keyfile: str) -> None:
    path = keyfile_path()
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fsutil.atomic_write(path, keyfile, mode=0o600)  # NM refuses world-readable keyfiles
    shell.run(["nmcli", "connection", "load", str(path)]).check("Loading the NetworkManager connection")


def remove_connection() -> None:
    shell.run(["nmcli", "connection", "delete", "id", CONNECTION_ID])
    fsutil.remove(keyfile_path())


def up(timeout: int = 30) -> None:
    shell.run(["nmcli", "--wait", str(timeout), "connection", "up", "id", CONNECTION_ID], timeout=timeout + 5).check(
        "Activating the hotspot connection"
    )


def down() -> shell.Result:
    return shell.run(["nmcli", "connection", "down", "id", CONNECTION_ID])


def unmanaged_conf_path() -> Path:
    return paths.NM_RUNTIME_CONF_DIR / "90-apsta-unmanaged.conf"


def render_unmanaged_conf(iface: str) -> str:
    # "+=" appends to the user's own unmanaged-devices instead of replacing it.
    return (
        "# Written by apsta while a hotspot runs; removed on stop.\n"
        f"[keyfile]\nunmanaged-devices+=interface-name:{iface}\n"
    )


def keep_away(iface: str) -> None:
    """Stop NetworkManager (and its wpa_supplicant) from touching ``iface``.

    Must run before the interface exists. ``nmcli device set X managed no``
    alone isn't enough: NetworkManager may adopt a new interface first and
    attach wpa_supplicant, which then blocks hostapd ("Match already configured").
    """
    fsutil.atomic_write(unmanaged_conf_path(), render_unmanaged_conf(iface), mode=0o644)
    shell.run(["nmcli", "general", "reload", "conf"])


def release(iface: Optional[str] = None) -> None:
    if unmanaged_conf_path().exists():
        fsutil.remove(unmanaged_conf_path())
        shell.run(["nmcli", "general", "reload", "conf"])


def device_state(iface: str) -> str:
    """NetworkManager's state for ``iface`` (e.g. "30 (disconnected)"), "" if unknown."""
    return shell.out(["nmcli", "-g", "GENERAL.STATE", "device", "show", iface])


def wait_until_available(iface: str, timeout: float = 10.0) -> bool:
    """Wait until NetworkManager manages ``iface`` and could activate a connection on it.

    Right after the interface is created, NetworkManager may still list it as
    unmanaged/unavailable and refuse with "No suitable device found".
    """
    deadline = time.monotonic() + timeout
    while True:
        state = device_state(iface)
        if state and not any(word in state for word in ("unmanaged", "unavailable", "unknown")):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def set_managed(iface: str, managed: bool) -> shell.Result:
    return shell.run(["nmcli", "device", "set", iface, "managed", "yes" if managed else "no"])


def scan(iface: str) -> str:
    return shell.out(["nmcli", "-t", "-f", "CHAN,SIGNAL", "device", "wifi", "list", "ifname", iface])
