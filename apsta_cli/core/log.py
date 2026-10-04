"""Structured JSON-lines log for privileged runs.

Only root writes the log: unprivileged commands (``apsta status`` polled by the
GUI) have nothing worth recording and must not create files in shared
locations. The file is size-capped with a single rotation.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from . import paths

MAX_BYTES = 1024 * 1024


def debug_enabled() -> bool:
    return os.environ.get("APSTA_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def _json_safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _rotate_if_needed(path) -> None:
    try:
        if path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass  # rotation is best-effort; logging continues in the same file


def event(level: str, name: str, **fields) -> None:
    if os.geteuid() != 0 and not os.environ.get("APSTA_LOG_PATH"):
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": name,
        "pid": os.getpid(),
        "command": " ".join(sys.argv[1:3]),
    }
    if fields:
        record["fields"] = _json_safe(fields)

    path = paths.LOG_PATH
    try:
        _rotate_if_needed(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)  # also tightens logs created world-readable by apsta <= 0.6
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        # Logging must never break a command.
        return
