"""Command-line interface: argument parsing, dispatch and error rendering.

This is the only module that turns :class:`ApstaError` into an exit code.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__
from .core import output, shell
from .core.errors import ApstaError

EPILOG = """
examples:
  apsta detect                         what your WiFi card can do
  sudo apsta start                     start (keeps WiFi when the card allows it)
  sudo apsta start --allow-disconnect  also allow modes that drop WiFi
  sudo apsta stop
  apsta status | apsta clients
  sudo apsta config --set ssid=MyHotspot
  sudo apsta config --password-stdin
  sudo apsta enable                    start at boot, recover after sleep
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apsta",
        description="Run a WiFi hotspot while staying connected to WiFi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("detect", help="Check what the WiFi hardware supports")
    p.add_argument("--json", action="store_true", help="Machine-readable output")

    def start_options(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--method",
            default="auto",
            choices=["auto", "hostapd", "nmcli", "nmcli-single"],
            help="Force a method (default: best available)",
        )
        p.add_argument(
            "--allow-disconnect",
            "--force",
            dest="allow_disconnect",
            action="store_true",
            help="Allow falling back to a mode that disconnects WiFi",
        )
        p.add_argument(
            "--wait-sta",
            metavar="SECONDS",
            type=float,
            default=0,
            help="Wait for WiFi to connect first, to share its channel",
        )
        p.add_argument("--interface", metavar="IFACE", help="WiFi interface to use (default: from config/auto)")

    p = sub.add_parser("start", help="Start the hotspot")
    start_options(p)
    p.add_argument("--ssid", help="Set the SSID of the active profile before starting")
    p.add_argument("--password-stdin", action="store_true", help="Read a new password from stdin before starting")
    p.add_argument("--json", action="store_true", help="Machine-readable output")

    sub.add_parser("stop", help="Stop the hotspot")

    p = sub.add_parser("run", help="Start and supervise the hotspot in the foreground (used by the service)")
    start_options(p)

    p = sub.add_parser("status", help="Show hotspot status")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p.add_argument("--clients", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--check", action="store_true", help="Exit 0 if the hotspot is running, 3 if not")
    # Deprecated aliases kept for scripts written against apsta <= 0.6.
    p.add_argument("--disconnect", metavar="CLIENT", help=argparse.SUPPRESS)
    p.add_argument("--limit-client", metavar="CLIENT", help=argparse.SUPPRESS)
    p.add_argument("--limit-kbps", type=int, help=argparse.SUPPRESS)
    p.add_argument("--use-profile", metavar="NAME", help=argparse.SUPPRESS)

    p = sub.add_parser("clients", help="List and manage hotspot clients")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    csub = p.add_subparsers(dest="action", metavar="ACTION")
    csub.add_parser("list", help="List connected clients")
    c = csub.add_parser("disconnect", help="Disconnect a client (MAC, IP or hostname)")
    c.add_argument("client")
    c.add_argument("--block", action="store_true", help="Also stop it reconnecting (hostapd mode)")
    c = csub.add_parser("unblock", help="Allow a blocked client again")
    c.add_argument("client")
    c = csub.add_parser("limit", help="Limit a client's bandwidth")
    c.add_argument("client")
    c.add_argument("kbps", type=int)
    c = csub.add_parser("unlimit", help="Remove a client's bandwidth limit")
    c.add_argument("client")

    p = sub.add_parser("config", help="View or change the active profile's settings")
    p.add_argument("--set", metavar="KEY=VALUE", action="append", help="Set a value (repeatable)")
    p.add_argument("--password-stdin", action="store_true", help="Read the new password from stdin/prompt")
    p.add_argument("--generate-password", action="store_true", help="Set a new random password")
    p.add_argument("--show-password", action="store_true", help="Show the password (root)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")

    p = sub.add_parser("profile", help="Manage named hotspot profiles")
    psub = p.add_subparsers(dest="action", metavar="ACTION")
    psub.add_parser("list", help="List profiles")
    c = psub.add_parser("show", help="Show a profile")
    c.add_argument("name", nargs="?")
    c = psub.add_parser("use", help="Switch the active profile")
    c.add_argument("name")
    c = psub.add_parser("create", help="Create a profile")
    c.add_argument("name")
    c.add_argument("--from", dest="from_profile", help="Copy from this profile (default: active)")
    c = psub.add_parser("delete", help="Delete a profile")
    c.add_argument("name")

    sub.add_parser("enable", help="Start the hotspot at boot (systemd, OpenRC, runit)")
    sub.add_parser("disable", help="Stop starting the hotspot at boot")
    sub.add_parser("scan-usb", help="Detect USB WiFi adapters")
    sub.add_parser("recommend", help="Suggest a USB adapter to buy")

    p = sub.add_parser("completion", help="Print a shell completion script")
    p.add_argument("shell", choices=["bash", "zsh", "fish"])
    return parser


# Tools a command can't work without (checked up front for a clear message).
REQUIRED_TOOLS = {
    "detect": ("iw",),
    "start": ("iw", "ip"),
    "run": ("iw", "ip"),
    "recommend": ("iw",),
}


def _handler(command: str):
    from .cmd import clients, completion, config, detect, hotspot, profile, service, status, usb

    return {
        "detect": detect.cmd_detect,
        "start": hotspot.cmd_start,
        "stop": hotspot.cmd_stop,
        "run": hotspot.cmd_run,
        "status": status.cmd_status,
        "clients": clients.cmd_clients,
        "config": config.cmd_config,
        "profile": profile.cmd_profile,
        "enable": service.cmd_enable,
        "disable": service.cmd_disable,
        "scan-usb": usb.cmd_scan_usb,
        "recommend": usb.cmd_recommend,
        "completion": completion.cmd_completion,
    }[command]


def _check_tools(command: str) -> None:
    missing = [t for t in REQUIRED_TOOLS.get(command, ()) if not shell.have(t)]
    if missing:
        raise ApstaError(
            f"Missing required tools: {', '.join(missing)}",
            hints=["Install the iw and iproute2 packages."],
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    args.parser = parser  # for `completion`, which describes the whole CLI
    output.machine_output(bool(getattr(args, "json", False)))
    try:
        _check_tools(args.command)
        return _handler(args.command)(args) or 0
    except ApstaError as exc:
        output.err(exc.message)
        for hint in exc.hints:
            output.hint(hint)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


def run() -> None:
    """Console-script entry point."""
    sys.exit(main())
