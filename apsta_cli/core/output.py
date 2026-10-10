"""Human-facing terminal output.

Colour is used only on a TTY and never when ``NO_COLOR`` is set
(https://no-color.org). Every message is mirrored to the structured log, so
callers must never pass secrets to these functions.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sys
from typing import Iterator

from . import log

_messages_to_stderr = contextvars.ContextVar("messages_to_stderr", default=False)


@contextlib.contextmanager
def machine_output(enabled: bool = True) -> Iterator[None]:
    """``--json`` mode while in this block: stdout carries only the JSON document; messages go to stderr."""
    token = _messages_to_stderr.set(enabled)
    try:
        yield
    finally:
        _messages_to_stderr.reset(token)


def _stream():
    return sys.stderr if _messages_to_stderr.get() else sys.stdout


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


# ── Messages from the lower layers ────────────────────────────────────────────
#
# hw/, net/ and config/ don't decide what the user sees: they report problems
# they work around with ``logging.getLogger(__name__).warning(...)``. The CLI
# renders those records like its own warnings (and so mirrors them to the log).


class _TerminalHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        warn(record.getMessage())


def show_library_messages() -> None:
    """Render warnings logged under ``apsta_cli.*`` on the terminal. Idempotent."""
    logger = logging.getLogger("apsta_cli")
    if not any(isinstance(h, _TerminalHandler) for h in logger.handlers):
        logger.addHandler(_TerminalHandler())
    logger.setLevel(logging.WARNING)
    logger.propagate = False
