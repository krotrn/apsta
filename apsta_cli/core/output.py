"""Human-facing terminal output.

Colour is used only on a TTY and never when ``NO_COLOR`` is set
(https://no-color.org). Every message is mirrored to the structured log, so
callers must never pass secrets to these functions.
"""

from __future__ import annotations

import os
import sys

from . import log

_MESSAGES_TO_STDERR = False


def machine_output(enabled: bool = True) -> None:
    """``--json`` mode: stdout carries only the JSON document; messages go to stderr."""
    global _MESSAGES_TO_STDERR
    _MESSAGES_TO_STDERR = enabled


def _stream():
    return sys.stderr if _MESSAGES_TO_STDERR else sys.stdout


def _use_color() -> bool:
    return "NO_COLOR" not in os.environ and _stream().isatty()


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
    print(f"  {C.GREEN}✔{C.RESET}  {msg}", file=_stream())
    log.event("INFO", "ok", message=msg)


def err(msg: str) -> None:
    print(f"  {C.RED}✘{C.RESET}  {msg}", file=sys.stderr)
    log.event("ERROR", "err", message=msg)


def warn(msg: str) -> None:
    print(f"  {C.YELLOW}⚠{C.RESET}  {msg}", file=sys.stderr)
    log.event("WARN", "warn", message=msg)


def info(msg: str) -> None:
    print(f"  {C.CYAN}→{C.RESET}  {msg}", file=_stream())
    log.event("INFO", "info", message=msg)


def head(msg: str) -> None:
    print(f"\n{C.BOLD}{msg}{C.RESET}", file=_stream())


def detail(msg: str) -> None:
    """Indented continuation line under a previous message."""
    print(f"     {msg}", file=_stream())


def reveal_secret(label: str, value: str, stream=None) -> None:
    """Show a secret the user explicitly asked to see. The only place apsta prints one.

    Never logged; callers only use it for an interactive terminal or for
    ``--show-password`` output requested by an authenticated (root) user.
    """
    print(f"  {C.CYAN}→{C.RESET}  {label}: {value}", file=stream or _stream())


def hint(msg: str) -> None:
    """Indented follow-up to an error or warning (goes to stderr with it)."""
    print(f"     {msg}", file=sys.stderr)


def blank() -> None:
    print(file=_stream())


def dbg(msg: str, **fields) -> None:
    log.event("DEBUG", "debug", message=msg, **fields)
    if log.debug_enabled():
        print(f"  {C.DIM}· {msg}{C.RESET}", file=sys.stderr)
