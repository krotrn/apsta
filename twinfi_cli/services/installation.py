"""How this copy of twinfi was installed, and the latest release.

`twinfi update` and `twinfi uninstall` go through the same route the install
took: the pacman repository, the .deb (PPA or release asset), install.sh
(source, under PREFIX/lib/twinfi) or pip. Downloads come from GitHub releases;
release assets are checked against the release's SHA256SUMS.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core import fsutil, paths, shell
from ..core.errors import SetupError

REPO = "krotrn/twinfi"
LATEST = f"https://github.com/{REPO}/releases/latest/download"
PPA = "ppa:krotrn/apsta"
PACMAN_CONF = Path("/etc/pacman.conf")
PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # the directory holding twinfi_cli
DOWNLOAD_TIMEOUT = 60

# Files twinfi or its README may have created outside the package: the sleep
# hook `twinfi enable` adds for pip installs, and completions installed by hand.
EXTRA_FILES = (
    Path("/usr/lib/systemd/system-sleep/twinfi-sleep"),
    Path("/etc/bash_completion.d/twinfi"),
    Path("/usr/local/share/zsh/site-functions/_twinfi"),
    Path("/etc/fish/completions/twinfi.fish"),
)


@dataclass(frozen=True)
class Installation:
    method: str  # "pacman", "deb", "source" (install.sh), "checkout" (git) or "pip"
    root: Path  # the directory holding twinfi_cli

    @property
    def prefix(self) -> Path:
        """install.sh's PREFIX: a source install lives in PREFIX/lib/twinfi."""
        return self.root.parents[1]


def detect(root: Path = PACKAGE_ROOT) -> Installation:
    if root.name == "twinfi" and root.parent.name == "lib":
        return Installation("source", root)
    if (root / ".git").exists():
        return Installation("checkout", root)
    module = str(root / "twinfi_cli" / "__init__.py")
    if shell.have("dpkg") and shell.run(["dpkg", "-S", module]).ok:
        return Installation("deb", root)
    if shell.have("pacman") and shell.run(["pacman", "-Qo", module]).ok:
        return Installation("pacman", root)
    return Installation("pip", root)


def version_tuple(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def is_newer(candidate: str, current: str) -> bool:
    return version_tuple(candidate) > version_tuple(current)


@dataclass(frozen=True)
class Release:
    version: str
    sums: Dict[str, str]  # file name -> sha256

    def asset(self, suffix: str) -> Optional[str]:
        # Releases also carry an apsta-*.tar.gz, so `apsta update` (0.12 and older) finds them.
        return next((name for name in self.sums if name.startswith("twinfi") and name.endswith(suffix)), None)


def _download(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SetupError(f"Could not download {url}: {reason}", hints=["Check the internet connection."]) from exc


def latest_release() -> Release:
    sums = {}
    for line in _download(f"{LATEST}/SHA256SUMS").decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) == 2:
            sums[fields[1].lstrip("*")] = fields[0].lower()
    versions = [m.group(1) for m in (re.fullmatch(r"twinfi-([\d.]+)\.tar\.gz", name) for name in sums) if m]
    if not versions:
        raise SetupError("Could not tell the latest release's version from its SHA256SUMS.")
    return Release(versions[0], sums)


def download_asset(release: Release, name: str, dest: Path) -> Path:
    """One of the release's files, checked against SHA256SUMS."""
    data = _download(f"https://github.com/{REPO}/releases/download/v{release.version}/{name}")
    if hashlib.sha256(data).hexdigest() != release.sums.get(name):
        raise SetupError(f"{name} does not match the release's SHA256SUMS; not installing it.")
    path = dest / name
    path.write_bytes(data)
    path.chmod(0o644)  # apt reads it as its own user
    return path


def fetch_source(version: str, dest: Path) -> Path:
    """The source tree of release ``version`` (it has install.sh, unlike the sdist)."""
    data = _download(f"https://github.com/{REPO}/archive/refs/tags/v{version}.tar.gz")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(dest, filter="data")
        else:  # Python without the extraction filters (before 3.10.12)
            safe = []
            for member in tar.getmembers():
                if not (member.isfile() or member.isdir()) or os.path.isabs(member.name) or ".." in member.name:
                    raise SetupError(f"The twinfi {version} source has an unsafe entry: {member.name}")
                safe.append(member)
            tar.extractall(dest, members=safe)  # noqa: S202 - only the members checked above
    tree = dest / f"twinfi-{version}"
    if not (tree / "install.sh").is_file():
        raise SetupError(f"The twinfi {version} source has no install.sh.")
    return tree


def install_script(inst: Installation, version: str, dest: Path) -> Path:
    """A copy of install.sh to run --uninstall with (it deletes the installed one)."""
    target = dest / "install.sh"
    local = inst.root / "install.sh"
    if local.is_file():
        shutil.copy(local, target)
    else:  # installed by install.sh before it kept a copy of itself
        target.write_bytes(_download(f"https://raw.githubusercontent.com/{REPO}/v{version}/install.sh"))
    return target


def uses_ppa() -> bool:
    return "/krotrn/apsta/" in shell.out(["apt-cache", "policy", "twinfi"])


def without_pacman_repo(text: str) -> str:
    """pacman.conf without the [twinfi] (or old [apsta]) section (up to the next section or blank line)."""
    kept: List[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped in ("[twinfi]", "[apsta]")  # apsta: the name until 0.13
        elif not stripped:
            skipping = False
        if not skipping:
            kept.append(line)
    result = "".join(kept)
    return result.rstrip("\n") + "\n" if result != text else text


def remove_pacman_repo() -> bool:
    try:
        text = PACMAN_CONF.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    updated = without_pacman_repo(text)
    if updated == text:
        return False
    fsutil.atomic_write(PACMAN_CONF, updated, mode=0o644)
    return True


def remove_leftovers(keep_config: bool) -> List[Path]:
    """Delete what twinfi created outside its package; returns what was there."""
    targets = [paths.RUN_DIR, paths.LOG_PATH, paths.LOG_PATH.with_name(paths.LOG_PATH.name + ".1"), *EXTRA_FILES]
    if not keep_config:
        targets.append(paths.CONFIG_DIR)
    removed = []
    for path in targets:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
        else:
            continue
        removed.append(path)
    return removed
