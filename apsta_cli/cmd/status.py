"""status: read-only view of the hotspot (mutating flags are deprecated aliases)."""

from __future__ import annotations

import json

from ..core import output
from ..core.errors import UsageError
from ..core.output import C
from ..services import hotspot

EXIT_INACTIVE = 3


def cmd_status(args) -> int:
    deprecated = _forward_deprecated(args)
    if deprecated is not None:
        return deprecated

    if args.check:
        return 0 if hotspot.current() else EXIT_INACTIVE

    data = hotspot.status()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    if args.clients:
        from .clients import print_clients

        print_clients(data["clients"], data["active"])
        return 0

    output.head("apsta — Status")
    output.blank()
    hs = data["hotspot"]
    if hs:
        band = "5 GHz" if hs["band"] == "a" else "2.4 GHz"
        output.ok(
            f"Hotspot {C.BOLD}{hs['ssid']}{C.RESET} active on {hs['ap_interface']} "
            f"({hs['method']}, channel {hs['channel']}, {band})"
        )
        if hs.get("subnet"):
            output.detail(f"Subnet {hs['subnet']}, gateway {hs['gateway']}")
        output.detail(f"Clients connected: {len(data['clients'])}")
        if hs.get("notes"):
            output.blank()
            output.info("Why it runs this way:")
            for note in hs["notes"]:
                output.detail(note)
    else:
        output.info("Hotspot is not running.")
        if data["stale"]:
            output.warn("A previous hotspot stopped unexpectedly; the next start cleans it up.")

    output.blank()
    output.info("WiFi interfaces:")
    for iface in data["interfaces"]:
        if iface["connected_ssid"]:
            link = f"→ {C.GREEN}{iface['connected_ssid']}{C.RESET}"
        else:
            link = f"{C.DIM}{iface['type']}{C.RESET}"
        output.detail(f"{C.BOLD}{iface['name']}{C.RESET}  {link}")
    output.blank()
    cfg = data["config"]
    output.info(f"Profile: {cfg['active_profile']}  (SSID {cfg['ssid']})")
    output.blank()
    return 0


def _forward_deprecated(args):
    """Old ``status --disconnect/--limit-client/--use-profile`` flags still work, with a warning."""
    from types import SimpleNamespace

    from .clients import cmd_clients
    from .profile import cmd_profile

    if getattr(args, "use_profile", None):
        output.warn("'status --use-profile' is deprecated; use 'apsta profile use NAME'.")
        return cmd_profile(SimpleNamespace(action="use", name=args.use_profile))
    if getattr(args, "disconnect", None):
        output.warn("'status --disconnect' is deprecated; use 'apsta clients disconnect CLIENT'.")
        return cmd_clients(SimpleNamespace(action="disconnect", client=args.disconnect, block=False, json=False))
    if getattr(args, "limit_client", None) or getattr(args, "limit_kbps", None):
        output.warn("'status --limit-client' is deprecated; use 'apsta clients limit CLIENT KBPS'.")
        if not args.limit_client or args.limit_kbps is None:
            raise UsageError("--limit-client and --limit-kbps must be used together.")
        return cmd_clients(SimpleNamespace(action="limit", client=args.limit_client, kbps=args.limit_kbps, json=False))
    return None
