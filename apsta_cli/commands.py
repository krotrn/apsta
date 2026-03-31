#!/usr/bin/env python3
"""Public command module aggregating user-facing command handlers."""

from .cmd.detect import cmd_detect
from .cmd.status_config import cmd_config, cmd_profile, cmd_status
from .cmd.usb import cmd_recommend, cmd_scan_usb

__all__ = [
    "cmd_detect",
    "cmd_status",
    "cmd_config",
    "cmd_scan_usb",
    "cmd_recommend",
    "cmd_profile",
]
