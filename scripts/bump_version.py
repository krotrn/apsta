#!/usr/bin/env python3
"""Bump apsta version across project metadata files."""

import re
import sys
from pathlib import Path


TARGETS = [
    (Path("pyproject.toml"), r'^(version\s*=\s*")([^"]+)(")', re.MULTILINE),
    (Path("setup.py"), r'(version\s*=\s*")([^"]+)(")', 0),
    (Path("apsta_cli/common.py"), r'(__version__\s*=\s*")([^"]+)(")', 0),
    (Path("apsta_gui/helpers.py"), r'(VERSION\s*=\s*")([^"]+)(")', 0),
]


def bump_version(new_version: str) -> int:
    if not re.match(r"^\d+\.\d+\.\d+(?:[-a-zA-Z0-9\.]+)?$", new_version):
        print(f"Invalid version: {new_version}")
        return 2

    for path, pattern, flags in TARGETS:
        content = path.read_text(encoding="utf-8")
        updated, count = re.subn(pattern, rf"\g<1>{new_version}\g<3>", content, count=1, flags=flags)
        if count != 1:
            print(f"Could not update version in {path}")
            return 3
        path.write_text(updated, encoding="utf-8")

    print(f"Updated version to {new_version}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: scripts/bump_version.py <new-version>")
        raise SystemExit(1)
    raise SystemExit(bump_version(sys.argv[1]))
