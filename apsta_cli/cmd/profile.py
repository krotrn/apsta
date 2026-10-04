"""profile: named sets of hotspot settings."""

from __future__ import annotations

from ..config import model, store
from ..core import lock, output
from ..core.errors import UsageError
from ..core.output import C
from ..services import hotspot
from .config import show_profile


def cmd_profile(args) -> int:
    action = getattr(args, "action", None) or "list"

    if action == "list":
        config = store.load()
        output.head("apsta — Profiles")
        output.blank()
        for name in model.profile_names(config):
            marker = f"{C.GREEN}*{C.RESET}" if name == model.active_name(config) else " "
            output.detail(f"{marker} {name}")
        output.blank()
        return 0

    if action == "show":
        config = store.load()
        name = args.name or model.active_name(config)
        if name not in config["profiles"]:
            raise UsageError(f"Profile not found: {name}")
        output.blank()
        show_profile(name, config["profiles"][name], reveal=False)
        output.blank()
        return 0

    hotspot.require_root("Changing profiles")
    with lock.command_lock("profile"):
        config = store.load()
        if action == "use":
            model.use_profile(config, args.name)
            message = f"Active profile: {args.name}"
        elif action == "create":
            model.create_profile(config, args.name, args.from_profile)
            message = f"Created profile: {args.name.strip()}"
        elif action == "delete":
            model.delete_profile(config, args.name)
            message = f"Deleted profile: {args.name}"
        else:
            raise UsageError(f"Unknown profile action: {action}")
        store.save(config)
    output.ok(message)
    if action == "use" and hotspot.current():
        output.info("Restart the hotspot to apply: sudo apsta stop && sudo apsta start")
    return 0
