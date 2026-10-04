"""Human-facing terminal output.

Colour is used only on a TTY and never when ``NO_COLOR`` is set
(https://no-color.org). Every message is mirrored to the structured log, so
callers must never pass secrets to these functions.
"""

from __future__ import annotations

import os
import sys

from . import log


def _use_color() -> bool:
    return "NO_COLOR" not in os.environ and sys.stdout.isatty()


class _Palette:
    def __getattr__(self, name: str) -> str:
        codes = {
            "RED": "\033[91m",
            "GREEN": "\033[92m",
            "YELLOW": "\033[93m",
            "BLUE": "\033[94m",
            "CYAN": "\033[96m",
            "BOLD": "\033[1m",
            "DIM": "\033[2m",
            "RESET": "\033[0m",
        }
        if name not in codes:
            raise AttributeError(name)
        return codes[name] if _use_color() else ""


C = _Palette()


def is_interactive() -> bool:
    """True when a person is watching stdout (not systemd, not a pipe)."""
    return sys.stdout.isatty()


def ok(msg: str) -> None:
    print(f"  {C.GREEN}✔{C.RESET}  {msg}")
    log.event("INFO", "ok", message=msg)


def err(msg: str) -> None:
    print(f"  {C.RED}✘{C.RESET}  {msg}", file=sys.stderr)
    log.event("ERROR", "err", message=msg)


def warn(msg: str) -> None:
    print(f"  {C.YELLOW}⚠{C.RESET}  {msg}", file=sys.stderr)
    log.event("WARN", "warn", message=msg)


def info(msg: str) -> None:
    print(f"  {C.CYAN}→{C.RESET}  {msg}")
    log.event("INFO", "info", message=msg)


def head(msg: str) -> None:
    print(f"\n{C.BOLD}{msg}{C.RESET}")


def detail(msg: str) -> None:
    """Indented continuation line under a previous message."""
    print(f"     {msg}")


def hint(msg: str) -> None:
    """Indented follow-up to an error or warning (goes to stderr with it)."""
    print(f"     {msg}", file=sys.stderr)


def blank() -> None:
    print()


def dbg(msg: str, **fields) -> None:
    log.event("DEBUG", "debug", message=msg, **fields)
    if log.debug_enabled():
        print(f"  {C.DIM}· {msg}{C.RESET}", file=sys.stderr)
