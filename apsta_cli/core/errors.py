"""Exception hierarchy.

Every expected failure is raised as an :class:`ApstaError`. The CLI entrypoint is
the only place that turns one into a message and an exit code, so library code
never calls ``sys.exit`` and can be reused (GUI, watcher, tests).
"""

from __future__ import annotations

from typing import Iterable, List, Optional


class ApstaError(Exception):
    exit_code = 1

    def __init__(self, message: str, hints: Optional[Iterable[str]] = None):
        super().__init__(message)
        self.message = message
        self.hints: List[str] = list(hints or [])


class UsageError(ApstaError):
    """Bad arguments or invalid configuration values."""

    exit_code = 2


class PermissionDenied(ApstaError):
    exit_code = 4


class HardwareError(ApstaError):
    """The radio cannot do what was asked (no AP mode, DFS channel, ...)."""


class SetupError(ApstaError):
    """A step of bringing the hotspot up or down failed."""


class AlreadyRunning(ApstaError):
    exit_code = 3


class NotRunning(ApstaError):
    exit_code = 3
