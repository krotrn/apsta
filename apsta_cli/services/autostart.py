"""Which init system runs apsta at boot, and is it enabled."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..core import shell

UNIT = "apsta.service"


def detect_init() -> str:
    if Path("/run/systemd/system").is_dir():
        return "systemd"
    if Path("/run/openrc").is_dir():
        return "openrc"
    if Path("/run/runit").is_dir() or Path("/etc/runit").is_dir():
        return "runit"
    return "unknown"


def info(init: Optional[str] = None) -> dict:
    """Autostart state for status displays; ``enabled`` is None when unknown."""
    init = init or detect_init()
    if init == "systemd" and shell.have("systemctl"):
        return {
            "init": init,
            "enabled": shell.run(["systemctl", "is-enabled", "--quiet", UNIT]).ok,
            "running": shell.run(["systemctl", "is-active", "--quiet", UNIT]).ok,
        }
    if init == "openrc" and shell.have("rc-update"):
        enabled = "apsta" in shell.out(["rc-update", "show", "default"]).split()
        return {"init": init, "enabled": enabled, "running": None}
    return {"init": init, "enabled": None, "running": None}
