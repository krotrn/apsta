"""update / uninstall: apsta updating and removing itself, the way it was installed.

Everything is imported up front: `uninstall` deletes apsta's own files
part-way through, after which nothing more can be imported.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional

from .. import __version__
from ..core import log, output, paths, shell
from ..core.errors import ApstaError, UsageError
from ..services import guard, hotspot, installation
from ..services.installation import Installation, Release
from .service import apsta_binary, boot_service_present, disable_boot_service

HOW = {
    "pacman": "from the apsta pacman repository",
    "deb": "from the .deb package",
    "source": "with install.sh",
    "checkout": "from a git checkout",
    "pip": "with pip",
}
PIP_HINTS = {
    "update": ["sudo pipx upgrade --global apsta", "or: pip install --upgrade apsta"],
    "uninstall": ["sudo pipx uninstall --global apsta", "or: pip uninstall apsta"],
}


def _run(argv, what: str, env: Optional[dict] = None) -> None:
    shell.run_visible(argv, env=env).check(what)


# ── update ────────────────────────────────────────────────────────────────────


def _update_pacman(inst: Installation, release: Release, tmp: Path) -> None:
    _run(["pacman", "-Sy", "--needed", "--noconfirm", "apsta"], "Updating the package")


def _update_deb(inst: Installation, release: Release, tmp: Path) -> None:
    if installation.uses_ppa():
        _run(["apt-get", "update"], "Refreshing the package lists")
        _run(["apt-get", "install", "-y", "--only-upgrade", "apsta"], "Updating the package")
        return
    name = release.asset("_all.deb")
    if name is None:
        raise ApstaError(f"apsta {release.version} has no .deb package.")
    output.info(f"Downloading {name}")
    deb = installation.download_asset(release, name, tmp)
    _run(["apt-get", "install", "-y", str(deb)], "Installing the package")


def _update_source(inst: Installation, release: Release, tmp: Path) -> None:
    output.info(f"Downloading the apsta {release.version} source")
    tree = installation.fetch_source(release.version, tmp)
    _run(["bash", tree / "install.sh"], "Running install.sh", env={"PREFIX": str(inst.prefix)})


UPDATE: Dict[str, Callable[[Installation, Release, Path], None]] = {
    "pacman": _update_pacman,
    "deb": _update_deb,
    "source": _update_source,
}


def _installed_version() -> str:
    words = shell.out([apsta_binary(), "--version"]).split()
    return words[-1] if words else ""


def cmd_update(args) -> int:
    inst = installation.detect()
    if inst.method == "checkout":
        raise ApstaError("apsta runs from a git checkout; update it with git.", hints=[f"git -C {inst.root} pull"])
    if inst.method == "pip":
        raise ApstaError("apsta was installed with pip; update it the same way.", hints=PIP_HINTS["update"])
    hotspot.require_root("Updating apsta")
    output.head("apsta — Update")
    release = installation.latest_release()
    if not installation.is_newer(release.version, __version__):
        output.ok(f"apsta {__version__} is the latest version.")
        output.blank()
        return 0
    output.info(f"Updating apsta {__version__} → {release.version} (installed {HOW[inst.method]})")
    output.blank()
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp).chmod(0o755)  # apt reads a downloaded .deb as its own user
        UPDATE[inst.method](inst, release, Path(tmp))
    output.blank()
    now = _installed_version()
    if now and installation.is_newer(release.version, now):
        output.warn(f"Your package source still has apsta {now}; {release.version} should reach it soon.")
    else:
        output.ok(f"apsta {release.version} installed.")
    if hotspot.current():
        output.info("The running hotspot uses the new version after a restart: sudo apsta stop && sudo apsta start")
    output.blank()
    return 0


# ── uninstall ─────────────────────────────────────────────────────────────────


def _remove_pacman(inst: Installation, tmp: Path) -> None:
    _run(["pacman", "-Rns", "--noconfirm", "apsta"], "Removing the package")
    if installation.remove_pacman_repo():
        output.ok(f"Removed the apsta repository from {installation.PACMAN_CONF}")


def _remove_deb(inst: Installation, tmp: Path) -> None:
    ppa = installation.uses_ppa()
    _run(["apt-get", "purge", "-y", "apsta"], "Removing the package")
    drop_ppa = ppa and shell.have("add-apt-repository")
    if drop_ppa and shell.run_visible(["add-apt-repository", "--remove", "-y", installation.PPA]).ok:
        output.ok(f"Removed {installation.PPA}")


def _remove_source(inst: Installation, tmp: Path) -> None:
    script = installation.install_script(inst, __version__, tmp)
    _run(["bash", script, "--uninstall"], "Running install.sh --uninstall", env={"PREFIX": str(inst.prefix)})


def _remove_pip(inst: Installation, tmp: Path) -> None:
    output.warn("apsta was installed with pip; remove the Python package the same way:")
    for hint in PIP_HINTS["uninstall"]:
        output.hint(hint)


def _remove_checkout(inst: Installation, tmp: Path) -> None:
    output.info(f"apsta runs from the git checkout in {inst.root}; delete it yourself when you no longer need it.")


REMOVE: Dict[str, Callable[[Installation, Path], None]] = {
    "pacman": _remove_pacman,
    "deb": _remove_deb,
    "source": _remove_source,
    "checkout": _remove_checkout,
    "pip": _remove_pip,
}


def _confirm(what: str) -> bool:
    if not sys.stdin.isatty():
        raise UsageError("Nothing removed: confirm with --yes when not running in a terminal.")
    return input(f"Remove {what}? [y/N] ").strip().lower() in ("y", "yes")


def cmd_uninstall(args) -> int:
    hotspot.require_root("Uninstalling apsta")
    inst = installation.detect()
    output.head("apsta — Uninstall")
    what = "apsta, its boot service, its log"
    what += "" if args.keep_config else f" and its settings and passwords ({paths.CONFIG_DIR})"
    output.info(f"apsta {__version__}, installed {HOW[inst.method]}")
    if not args.yes and not _confirm(what):
        output.info("Nothing removed.")
        return 1
    output.blank()

    guard.stop()  # first, so it can't restart what is being stopped
    st = hotspot.stop()
    if st:
        output.ok(f"Hotspot '{st.ssid}' stopped.")
    if boot_service_present():
        disable_boot_service()

    with tempfile.TemporaryDirectory() as tmp:
        REMOVE[inst.method](inst, Path(tmp))
    log.stop()  # its messages would recreate the log being removed
    for path in installation.remove_leftovers(args.keep_config):
        output.ok(f"Removed {path}")

    output.blank()
    if args.keep_config:
        output.ok(f"apsta removed. Your settings are still in {paths.CONFIG_DIR}.")
    else:
        output.ok("apsta removed completely.")
    output.blank()
    return 0
