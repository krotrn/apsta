"""enable / disable: run twinfi as a boot-time service (systemd, OpenRC, runit)."""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from ..core import fsutil, output, paths, shell
from ..core.errors import TwinfiError
from ..services import autostart, guard, hotspot
from ..services.autostart import detect_init

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PACKAGED_BINARY = "/usr/bin/twinfi"

SYSTEMD_PACKAGED_UNIT = Path("/usr/lib/systemd/system/twinfi.service")
SYSTEMD_LOCAL_UNIT = Path("/etc/systemd/system/twinfi.service")
SLEEP_HOOK = Path("/usr/lib/systemd/system-sleep/twinfi-sleep")
PM_SLEEP_HOOK = Path("/etc/pm/sleep.d/10_twinfi")
OPENRC_SCRIPT = Path("/etc/init.d/twinfi")
RUNIT_DIR = Path("/etc/sv/twinfi")
RUNIT_SERVICE_DIRS = (Path("/etc/runit/runsvdir/default"), Path("/var/service"))


def data_file(name: str) -> str:
    return (DATA_DIR / name).read_text(encoding="utf-8")


def twinfi_binary() -> str:
    """Absolute path of the twinfi being run, so service files point at this install."""
    argv0 = os.path.abspath(sys.argv[0])
    if os.path.basename(argv0) == "twinfi" and os.access(argv0, os.X_OK):
        return argv0
    return shell.which("twinfi") or PACKAGED_BINARY


def render(name: str, binary: str) -> str:
    return data_file(name).replace(PACKAGED_BINARY, binary)


def _run(argv: List[str], what: str) -> None:
    shell.run(argv).check(what)
    output.ok(what)


def _install(path: Path, text: str, mode: int) -> None:
    fsutil.atomic_write(path, text, mode=mode)
    output.ok(f"Installed {path}")


# ── systemd ───────────────────────────────────────────────────────────────────


def _enable_systemd(binary: str, now: bool = True) -> None:
    if not (SYSTEMD_PACKAGED_UNIT.exists() and binary == PACKAGED_BINARY):
        _install(SYSTEMD_LOCAL_UNIT, render("twinfi.service", binary), 0o644)
        _run(["systemctl", "daemon-reload"], "Reloaded systemd")
    if not SLEEP_HOOK.exists():
        _install(SLEEP_HOOK, render("twinfi-sleep", binary), 0o755)
    if now:
        _run(["systemctl", "enable", "--now", "twinfi.service"], "Enabled and started twinfi.service")
    else:
        _run(["systemctl", "enable", "twinfi.service"], "Enabled twinfi.service")


@contextmanager
def keeping_hotspot():
    """Stop the service inside this block without stopping its hotspot.

    The service's watcher sees the marker as it exits and leaves the hotspot
    up. Yields whether a hotspot is running. The stop must be synchronous: the
    marker is removed afterwards in case no watcher was there to read it.
    """
    running = hotspot.current() is not None
    if running:
        fsutil.atomic_write(paths.KEEP_MARKER, "")
    try:
        yield running
    finally:
        fsutil.remove(paths.KEEP_MARKER)


def _kept(running: bool, watched: bool) -> None:
    if running:
        output.ok("The running hotspot stays on; `twinfi stop` stops it.")
        if not watched:
            output.info("It isn't restarted after sleep or Wi-Fi changes until you start it again.")


def _disable_systemd() -> None:
    # Turning off the boot service shouldn't take down the hotspot it started:
    # hand it to the watcher `twinfi start` uses instead.
    with keeping_hotspot() as running:
        shell.run(["systemctl", "disable", "--now", "twinfi.service"])
    output.ok("Disabled twinfi.service")
    _kept(running, running and (guard.active() or guard.launch(twinfi_binary(), hotspot.StartOptions())))
    if SYSTEMD_LOCAL_UNIT.exists():
        fsutil.remove(SYSTEMD_LOCAL_UNIT)
        output.ok(f"Removed {SYSTEMD_LOCAL_UNIT}")
        shell.run(["systemctl", "daemon-reload"])


# ── OpenRC / runit ────────────────────────────────────────────────────────────


def _enable_openrc(binary: str) -> None:
    _install(OPENRC_SCRIPT, render("twinfi.openrc", binary), 0o755)
    _run(["rc-update", "add", "twinfi", "default"], "Added twinfi to the default runlevel")
    _run(["rc-service", "twinfi", "start"], "Started twinfi")


def _disable_openrc() -> None:
    with keeping_hotspot() as running:
        shell.run(["rc-service", "twinfi", "stop"])
    _kept(running, False)
    shell.run(["rc-update", "del", "twinfi", "default"])
    fsutil.remove(OPENRC_SCRIPT)
    output.ok("Removed the OpenRC service")


def _runit_service_dir() -> Optional[Path]:
    return next((d for d in RUNIT_SERVICE_DIRS if d.is_dir()), None)


def _enable_runit(binary: str) -> None:
    _install(RUNIT_DIR / "run", render("twinfi.runit", binary), 0o755)
    service_dir = _runit_service_dir()
    if service_dir is None:
        raise TwinfiError("Could not find the runit service directory.", hints=[f"Link {RUNIT_DIR} into it manually."])
    link = service_dir / "twinfi"
    if not link.exists():
        link.symlink_to(RUNIT_DIR)
    output.ok(f"Linked {link} -> {RUNIT_DIR}")


def _disable_runit() -> None:
    service_dir = _runit_service_dir()
    if service_dir and (service_dir / "twinfi").is_symlink():
        with keeping_hotspot() as running:
            shell.run(["sv", "-w", "30", "down", str(service_dir / "twinfi")])  # waits, unlike the unlink
        _kept(running, False)
        (service_dir / "twinfi").unlink()
    for path in (RUNIT_DIR / "run",):
        fsutil.remove(path)
    try:
        RUNIT_DIR.rmdir()
    except OSError:
        pass  # not empty: the user keeps their own files there
    output.ok("Removed the runit service")


ENABLE = {"systemd": _enable_systemd, "openrc": _enable_openrc, "runit": _enable_runit}
DISABLE = {"systemd": _disable_systemd, "openrc": _disable_openrc, "runit": _disable_runit}


def cmd_enable(args) -> int:
    hotspot.require_root("Enabling the service")
    output.head("twinfi — Enabling the hotspot service")
    init = detect_init()
    output.info(f"Init system: {init}")
    if init not in ENABLE:
        raise TwinfiError(
            "Unsupported init system.",
            hints=["Start `twinfi run --wait-sta 30` at boot, after NetworkManager, under your supervisor."],
        )
    binary = twinfi_binary()
    ENABLE[init](binary)
    if init != "systemd" and PM_SLEEP_HOOK.parent.is_dir():
        _install(PM_SLEEP_HOOK, render("twinfi-sleep", binary), 0o755)
    output.blank()
    output.ok("The hotspot now starts at boot and recovers after sleep.")
    output.blank()
    return 0


def boot_service_present() -> bool:
    """Whether `twinfi enable` (or a package's enabled unit) set anything up to undo."""
    if any(p.exists() for p in (SYSTEMD_LOCAL_UNIT, OPENRC_SCRIPT, RUNIT_DIR, PM_SLEEP_HOOK)):
        return True
    return autostart.info().get("enabled") is True


def disable_boot_service() -> None:
    init = detect_init()
    if init in DISABLE:
        DISABLE[init]()
    if PM_SLEEP_HOOK.exists():
        fsutil.remove(PM_SLEEP_HOOK)


def cmd_disable(args) -> int:
    hotspot.require_root("Disabling the service")
    output.head("twinfi — Disabling the hotspot service")
    disable_boot_service()
    output.blank()
    return 0
