"""Moving an apsta install over to twinfi (the project's name until 0.13).

:func:`migrate` runs before every command run as root, and does nothing once
nothing of apsta is left:

- /etc/apsta (profiles and passwords) becomes /etc/twinfi;
- a boot service set up by ``apsta enable`` (or the apsta package) is replaced
  by twinfi's. On systemd the new unit is enabled but not started, since the
  command being run may be about something else; OpenRC and runit services
  are removed, with a hint to run ``twinfi enable``;
- an ``[apsta]`` pacman repository section is renamed ``[twinfi]``.

A hotspot apsta started is only reported: its interfaces, connections and
firewall rules carry the old name, so twinfi can't take it down cleanly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .core import fsutil, log, output, paths, shell
from .services import autostart, installation

SYSTEMD_LOCAL_UNIT = Path("/etc/systemd/system/apsta.service")
# Still there after the apsta package removed its unit, as a dangling link.
SYSTEMD_WANTS_LINK = Path("/etc/systemd/system/multi-user.target.wants/apsta.service")
OPENRC_SCRIPT = Path("/etc/init.d/apsta")
RUNIT_DIR = Path("/etc/sv/apsta")
# Hooks and completions an apsta pip or source install put outside its package.
STRAY_FILES = (
    Path("/usr/lib/systemd/system-sleep/apsta-sleep"),
    Path("/etc/pm/sleep.d/10_apsta"),
    Path("/etc/bash_completion.d/apsta"),
    Path("/usr/local/share/zsh/site-functions/_apsta"),
    Path("/etc/fish/completions/apsta.fish"),
)


def migrate() -> None:
    try:
        _move_config()
        _replace_boot_service()
        _rename_pacman_repo()
        for path in STRAY_FILES:
            if path.exists() and not _packaged(path):
                fsutil.remove(path)
    except OSError as exc:
        # twinfi.service sees /etc read-only; the next `sudo twinfi` finishes the move.
        log.event("info", "legacy_migration_postponed", error=str(exc))
    if (paths.LEGACY_RUN_DIR / "state.json").exists():
        output.warn("A hotspot started by apsta (twinfi's old name) is still running.")
        output.hint("Restart the computer to clear it; twinfi can't stop it cleanly.")


def _packaged(path: Path) -> bool:
    """Whether an (older) apsta package still owns ``path``; the package manager removes those."""
    if shell.have("dpkg") and shell.run(["dpkg", "-S", str(path)]).ok:
        return True
    return shell.have("pacman") and shell.run(["pacman", "-Qo", str(path)]).ok


def _move_config() -> None:
    if not paths.LEGACY_CONFIG_DIR.is_dir():
        return
    if not paths.CONFIG_DIR.exists():
        paths.CONFIG_DIR.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(paths.LEGACY_CONFIG_DIR), str(paths.CONFIG_DIR))
    elif not any(paths.CONFIG_DIR.iterdir()):  # created empty by twinfi.service's ConfigurationDirectory
        for item in paths.LEGACY_CONFIG_DIR.iterdir():
            shutil.move(str(item), str(paths.CONFIG_DIR / item.name))
        paths.LEGACY_CONFIG_DIR.rmdir()
    else:
        return
    output.ok(f"Moved the apsta settings to {paths.CONFIG_DIR} (apsta is now twinfi)")


def _replace_boot_service() -> None:
    from .cmd import service  # imports the hotspot services; only needed when there's something to move

    if SYSTEMD_WANTS_LINK.is_symlink() or SYSTEMD_LOCAL_UNIT.exists():
        enabled = SYSTEMD_WANTS_LINK.is_symlink()
        if os.path.exists(SYSTEMD_WANTS_LINK):  # the unit is still installed
            shell.run(["systemctl", "disable", "apsta.service"])
        fsutil.remove(SYSTEMD_WANTS_LINK)
        fsutil.remove(SYSTEMD_LOCAL_UNIT)
        shell.run(["systemctl", "daemon-reload"])
        if enabled and not autostart.info("systemd").get("enabled"):
            output.info("Moving the apsta boot service to twinfi.service")
            service._enable_systemd(service.twinfi_binary(), now=False)
    if OPENRC_SCRIPT.exists():
        shell.run(["rc-update", "del", "apsta", "default"])
        fsutil.remove(OPENRC_SCRIPT)
        _hint_enable("OpenRC")
    if RUNIT_DIR.exists():
        for service_dir in service.RUNIT_SERVICE_DIRS:
            link = service_dir / "apsta"
            if link.is_symlink():
                link.unlink()
        shutil.rmtree(RUNIT_DIR, ignore_errors=True)
        _hint_enable("runit")


def _hint_enable(init: str) -> None:
    output.warn(f"Removed the apsta {init} service (apsta is now twinfi).")
    output.hint("Start the hotspot at boot again with: sudo twinfi enable")


def _rename_pacman_repo() -> None:
    try:
        text = installation.PACMAN_CONF.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError):
        return
    lines = text.splitlines(keepends=True)
    names = [line.strip() for line in lines]
    if "[apsta]" not in names or "[twinfi]" in names:
        return
    # The repository's address stays the same; it serves twinfi.db next to apsta.db.
    renamed = "".join("[twinfi]\n" if line.strip() == "[apsta]" else line for line in lines)
    fsutil.atomic_write(installation.PACMAN_CONF, renamed, mode=0o644)
    output.ok(f"Renamed the [apsta] repository in {installation.PACMAN_CONF} to [twinfi]")
