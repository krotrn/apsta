#!/usr/bin/env python3
"""Check that every place that carries the version agrees, before publishing.

scripts/release_check.py 0.7.0      # prints "version=0.7.0" on success
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from release_notes import section  # noqa: E402

SOURCES = {
    "pyproject.toml": r'^version\s*=\s*"([^"]+)"',
    "apsta_cli/__init__.py": r'__version__\s*=\s*"([^"]+)"',
    "packaging/arch/PKGBUILD": r"^pkgver=(\S+)",
    "debian/changelog": r"^apsta \(([^)-]+)",
    "apsta_gui/data/com.github.apsta.Gtk.metainfo.xml": r'<release version="([^"]+)"',
}


def versions() -> dict:
    found = {}
    for path, pattern in SOURCES.items():
        match = re.search(pattern, (ROOT / path).read_text(encoding="utf-8"), re.MULTILINE)
        found[path] = match.group(1) if match else None
    return found


def main(expected: str) -> int:
    problems = [f"{path} has {got!r}, expected {expected!r}" for path, got in versions().items() if got != expected]
    try:
        section(expected, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    except SystemExit as exc:
        problems.append(str(exc))
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"version={expected}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1]))
