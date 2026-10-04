"""Process-wide mutual exclusion for state-changing commands."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from typing import Iterator

from . import paths
from .errors import ApstaError


@contextmanager
def command_lock(action: str, wait_seconds: float = 10.0) -> Iterator[None]:
    """Serialise start/stop/config/client changes across processes.

    Waits briefly instead of failing immediately, because the GUI, the sleep
    hook and the service can legitimately overlap for a moment.
    """
    lock_path = paths.LOCK_PATH
    lock_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as f:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                flock(f.fileno(), LOCK_EX | LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ApstaError(
                        "Another apsta action is already running.",
                        hints=["Wait for it to finish, then retry."],
                    ) from None
                time.sleep(0.1)
        f.seek(0)
        f.truncate(0)
        f.write(f"pid={os.getpid()} action={action} ts={datetime.now(timezone.utc).isoformat()}\n")
        f.flush()
        try:
            yield
        finally:
            flock(f.fileno(), LOCK_UN)
