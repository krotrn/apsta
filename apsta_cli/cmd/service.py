"""enable / disable: run apsta as a boot-time service (systemd, OpenRC, runit)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

from ..core import fsutil, output, shell
from ..core.errors import ApstaError
from ..services import hotspot
from ..services.autostart import detect_init

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PACKAGED_BINARY = "/usr/bin/apsta"

SYSTEMD_PACKAGED_UNIT = Path("/usr/lib/systemd/system/apsta.service")
SYSTEMD_LOCAL_UNIT = Path("/etc/systemd/system/apsta.service")
SLEEP_HOOK = Path("/usr/lib/systemd/system-sleep/apsta-sleep")
PM_SLEEP_HOOK = Path("/etc/pm/sleep.d/10_apsta")
OPENRC_SCRIPT = Path("/etc/init.d/apsta")
RUNIT_DIR = Path("/etc/sv/apsta")
RUNIT_SERVICE_DIRS = (Path("/etc/runit/runsvdir/default"), Path("/var/service"))


def data_file(name: str) -> str:
    return (DATA_DIR / name).read_text(encoding="utf-8")


def apsta_binary() -> str:
    """Absolute path of the apsta being run, so service files point at this install."""
    argv0 = os.path.abspath(sys.argv[0])
    if os.path.basename(argv0) == "apsta" and os.access(argv0, os.X_OK):
        return argv0
    return shell.which("apsta") or PACKAGED_BINARY


def render(name: str, binary: str) -> str:
    return data_file(name).replace(PACKAGED_BINARY, binary)


def _run(argv: List[str], what: str) -> None:
    shell.run(argv).check(what)
    output.ok(what)


def _install(path: Path, text: str, mode: int) -> None:
    fsutil.atomic_write(path, text, mode=mode)
    output.ok(f"Installed {path}")


# ── systemd ───────────────────────────────────────────────────────────────────


def _enable_systemd(binary: str) -> None:
    if not (SYSTEMD_PACKAGED_UNIT.exists() and binary == PACKAGED_BINARY):
        _install(SYSTEMD_LOCAL_UNIT, render("apsta.service", binary), 0o644)
        _run(["systemctl", "daemon-reload"], "Reloaded systemd")
    if not SLEEP_HOOK.exists():
        _install(SLEEP_HOOK, render("apsta-sleep", binary), 0o755)
    _run(["systemctl", "enable", "--now", "apsta.service"], "Enabled and started apsta.service")


def _disable_systemd() -> None:
    shell.run(["systemctl", "disable", "--now", "apsta.service"])
    output.ok("Disabled apsta.service")
    if SYSTEMD_LOCAL_UNIT.exists():
        fsutil.remove(SYSTEMD_LOCAL_UNIT)
        output.ok(f"Removed {SYSTEMD_LOCAL_UNIT}")
        shell.run(["systemctl", "daemon-reload"])


# ── OpenRC / runit ────────────────────────────────────────────────────────────


def _enable_openrc(binary: str) -> None:
    _install(OPENRC_SCRIPT, render("apsta.openrc", binary), 0o755)
    _run(["rc-update", "add", "apsta", "default"], "Added apsta to the default runlevel")
    _run(["rc-service", "apsta", "start"], "Started apsta")


def _disable_openrc() -> None:
    shell.run(["rc-service", "apsta", "stop"])
    shell.run(["rc-update", "del", "apsta", "default"])
    fsutil.remove(OPENRC_SCRIPT)
    output.ok("Removed the OpenRC service")


def _runit_service_dir() -> Optional[Path]:
    return next((d for d in RUNIT_SERVICE_DIRS if d.is_dir()), None)


def _enable_runit(binary: str) -> None:
    _install(RUNIT_DIR / "run", render("apsta.runit", binary), 0o755)
    service_dir = _runit_service_dir()
    if service_dir is None:
        raise ApstaError("Could not find the runit service directory.", hints=[f"Link {RUNIT_DIR} into it manually."])
    link = service_dir / "apsta"
    if not link.exists():
        link.symlink_to(RUNIT_DIR)
    output.ok(f"Linked {link} -> {RUNIT_DIR}")


def _disable_runit() -> None:
    service_dir = _runit_service_dir()
    if service_dir and (service_dir / "apsta").is_symlink():
        (service_dir / "apsta").unlink()
    for path in (RUNIT_DIR / "run",):
        fsutil.remove(path)
    try:
        RUNIT_DIR.rmdir()
    except OSError:
        pass
    output.ok("Removed the runit service")


ENABLE = {"systemd": _enable_systemd, "openrc": _enable_openrc, "runit": _enable_runit}
DISABLE = {"systemd": _disable_systemd, "openrc": _disable_openrc, "runit": _disable_runit}


def cmd_enable(args) -> int:
    hotspot.require_root("Enabling the service")
    output.head("apsta — Enabling the hotspot service")
    init = detect_init()
    output.info(f"Init system: {init}")
    if init not in ENABLE:
        raise ApstaError(
            "Unsupported init system.",
            hints=["Start `apsta run --wait-sta 30` at boot, after NetworkManager, under your supervisor."],
        )
    binary = apsta_binary()
    ENABLE[init](binary)
    if init != "systemd" and PM_SLEEP_HOOK.parent.is_dir():
        _install(PM_SLEEP_HOOK, render("apsta-sleep", binary), 0o755)
    output.blank()
    output.ok("The hotspot now starts at boot and recovers after sleep.")
    output.blank()
    return 0


def cmd_disable(args) -> int:
    hotspot.require_root("Disabling the service")
    output.head("apsta — Disabling the hotspot service")
    init = detect_init()
    if init in DISABLE:
        DISABLE[init]()
    if PM_SLEEP_HOOK.exists():
        fsutil.remove(PM_SLEEP_HOOK)
    output.blank()
    return 0
