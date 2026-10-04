#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a version (used as GitHub release notes).

scripts/release_notes.py 0.7.0
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def section(version: str, text: str) -> str:
    """The body under ``## [version]`` up to the next ``## [`` heading."""
    match = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.MULTILINE | re.DOTALL)
    if not match or not match.group(1).strip():
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    return match.group(1).strip() + "\n"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.stdout.write(section(sys.argv[1], CHANGELOG.read_text(encoding="utf-8")))
