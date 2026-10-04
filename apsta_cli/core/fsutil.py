"""Crash-safe file writes."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """Write ``text`` so readers see either the old or the new file, never a torn one.

    The temporary file is created next to the target (same filesystem, so the
    rename is atomic) with a random name and ``O_EXCL`` semantics, which also
    makes this safe in directories other users can write to.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass  # the temp file was never created or is already gone
        raise


def remove(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass  # already absent, which is the goal
