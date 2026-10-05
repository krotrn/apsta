"""config: view and change the active profile's settings."""

from __future__ import annotations

import getpass
import json
import sys
from typing import Dict, Optional

from ..config import model, store
from ..core import lock, output
from ..core.errors import UsageError
from ..core.output import C
from ..services import hotspot

SETTABLE = model.PROFILE_KEYS + ("active_profile",)


def read_password_stdin() -> str:
    """Read a password without it touching argv, shell history or sudo logs."""
    if sys.stdin.isatty():
        first = getpass.getpass("New hotspot password: ")
        if getpass.getpass("Repeat password: ") != first:
            raise UsageError("Passwords do not match.")
        return first
    return sys.stdin.readline().rstrip("\r\n")


def parse_assignment(text: str):
    key, sep, value = text.partition("=")
    key = key.strip()
    if not sep:
        raise UsageError(f"Expected KEY=VALUE, got '{text}'.")
    if key not in SETTABLE:
        raise UsageError(f"Unknown config key: {key}", hints=[f"Valid keys: {', '.join(SETTABLE)}"])
    return key, value


def display(key: str, value) -> Optional[str]:
    """A profile value as text, or None for "not set"."""
    if key == "hidden":
        return "yes" if value else "no"
    if key == "allowed_macs":
        return ", ".join(value) if value else None
    return value or None


def apply_settings(settings: Dict[str, Optional[str]], quiet: bool = False) -> dict:
    """Validate everything first, then save once, under the lock."""
    with lock.command_lock("config"):
        config = store.load()
        if "active_profile" in settings:
            model.use_profile(config, settings.pop("active_profile"))
        for key, value in settings.items():
            model.set_field(config, key, value)
        store.save(config)
    if not quiet:
        for key in settings:
            if key == "password":
                shown = "(hidden)"
            else:
                shown = display(key, config.get(key)) or (
                    "(anyone with the password)" if key == "allowed_macs" else "(auto)"
                )
            output.ok(f"Set {key} = {shown} for profile '{model.active_name(config)}'")
    return config


def show_profile(name: str, values: dict, reveal: bool) -> None:
    output.info(f"Profile: {C.BOLD}{name}{C.RESET}")
    for key in model.PROFILE_KEYS:
        value = values.get(key)
        if key == "password":
            if value is None:
                shown = f"{C.DIM}(not set or not readable — use sudo){C.RESET}"
            else:
                shown = value if reveal else f"{C.DIM}(hidden — --show-password){C.RESET}"
        elif display(key, value):
            shown = f"{C.YELLOW}{display(key, value)}{C.RESET}"
        else:
            shown = f"{C.DIM}{'(anyone with the password)' if key == 'allowed_macs' else '(auto)'}{C.RESET}"
        output.detail(f"{key:<12} {shown}")


def cmd_config(args) -> int:
    settings: Dict[str, Optional[str]] = {}
    for item in args.set or []:
        key, value = parse_assignment(item)
        if key == "password":
            output.warn("Passwords given on the command line end up in shell history and sudo logs.")
            output.detail("Prefer: sudo apsta config --password-stdin")
        settings[key] = value
    if args.password_stdin:
        settings["password"] = read_password_stdin()
    if args.generate_password:
        settings["password"] = store.generate_password()

    if settings:
        hotspot.require_root("Changing the configuration")
        output.head("apsta — Configuration")
        config = apply_settings(settings)
        if args.generate_password:
            output.reveal_secret("New password", config["password"])
        if hotspot.current():
            output.info("Restart the hotspot to apply: sudo apsta stop && sudo apsta start")
        output.blank()
        return 0

    if args.show_password:
        hotspot.require_root("Showing the password")
    config = store.load()
    if getattr(args, "json", False):
        data = {
            "active_profile": model.active_name(config),
            "profiles": model.profile_names(config),
            "settings": {k: v for k, v in model.active_profile(config).items() if k != "password"},
        }
        if args.show_password:
            data["password"] = config["password"]
        print(json.dumps(data, indent=2))
        return 0
    output.head("apsta — Configuration")
    output.blank()
    output.info(f"Profiles: {', '.join(model.profile_names(config))}")
    show_profile(model.active_name(config), model.active_profile(config), args.show_password)
    output.blank()
    output.info("Change with: sudo apsta config --set ssid=MyHotspot   |   sudo apsta config --password-stdin")
    output.blank()
    return 0
