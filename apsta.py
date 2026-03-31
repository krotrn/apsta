#!/usr/bin/env python3
"""apsta CLI entrypoint.

The heavy logic lives in focused modules:
- apsta_cli/commands.py
- apsta_cli/hotspot.py
- apsta_cli/system.py
- apsta_cli/completion.py
"""

import argparse
import sys

from apsta_cli.commands import cmd_config, cmd_detect, cmd_profile, cmd_recommend, cmd_scan_usb, cmd_status
from apsta_cli.common import C, __version__
from apsta_cli.completion import cmd_completion
from apsta_cli.hotspot import cmd_start, cmd_stop
from apsta_cli.system import _check_dependencies, cmd_disable, cmd_enable


def main():
    parser = argparse.ArgumentParser(
        prog="apsta",
        description="Smart AP+STA WiFi hotspot manager for Linux",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
commands:
  detect        Check hardware AP+STA capabilities
  start         Start hotspot (auto-detects best method)
  stop          Stop active hotspot
  status        Show current WiFi and hotspot state
  profile       Manage named hotspot profiles
  config        View or edit saved configuration
  enable        Install systemd service + sleep hook (auto-start on boot)
  disable       Uninstall systemd service + sleep hook
  scan-usb      Detect plugged-in USB WiFi adapters + AP+STA capability
  recommend     Suggest USB adapters to buy if built-in card lacks AP+STA
  completion    Print shell completion script

start methods (tried in order):
  1. nmcli virtual interface   — true concurrent AP+STA
  2. hostapd virtual interface — Intel AX200 and similar (needs hostapd + dnsmasq)
  3. nmcli --force             — disconnects WiFi

examples:
  apsta detect
  apsta detect --json
  sudo apsta start
  sudo apsta start --force
  apsta status --json
  apsta status --clients
  sudo apsta status --disconnect 7e:2f:aa:11:22:33
  apsta profile list
  apsta profile use home
  apsta config --set ssid=MyHotspot
  apsta completion zsh > ~/.zsh/completions/_apsta
  sudo apsta enable
        """,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_detect = sub.add_parser("detect", help="Detect hardware AP+STA capability")
    p_detect.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    p_start = sub.add_parser("start", help="Start hotspot")
    p_start.add_argument(
        "--force",
        action="store_true",
        help="Skip AP+STA attempts and force single-interface mode (drops WiFi)",
    )
    p_start.add_argument("--json", action="store_true", help=argparse.SUPPRESS)

    sub.add_parser("stop", help="Stop hotspot")

    p_status = sub.add_parser("status", help="Show status")
    p_status.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    p_status.add_argument("--clients", action="store_true", help="Show connected hotspot clients only")
    p_status.add_argument("--disconnect", metavar="CLIENT", help="Disconnect client by MAC, IP, or hostname")

    p_profile = sub.add_parser("profile", help="Manage named hotspot profiles")
    p_profile_sub = p_profile.add_subparsers(dest="action")

    p_profile_sub.add_parser("list", help="List available profiles")

    p_profile_show = p_profile_sub.add_parser("show", help="Show profile values")
    p_profile_show.add_argument("name", nargs="?", help="Profile name (default: active profile)")

    p_profile_use = p_profile_sub.add_parser("use", help="Set the active profile")
    p_profile_use.add_argument("name", help="Profile name")

    p_profile_create = p_profile_sub.add_parser("create", help="Create a profile")
    p_profile_create.add_argument("name", help="New profile name")
    p_profile_create.add_argument("--from", dest="from_profile", help="Source profile name (default: active)")

    p_profile_delete = p_profile_sub.add_parser("delete", help="Delete a profile")
    p_profile_delete.add_argument("name", help="Profile name")

    sub.add_parser("enable", help="Install systemd service + sleep hook")
    sub.add_parser("disable", help="Uninstall systemd service + sleep hook")
    sub.add_parser("scan-usb", help="Detect USB WiFi adapters + AP+STA capability")
    sub.add_parser("recommend", help="Suggest USB adapters to buy")

    p_completion = sub.add_parser("completion", help="Print shell completion script")
    p_completion.add_argument("shell", choices=["bash", "zsh", "fish"], help="Target shell")

    p_config = sub.add_parser("config", help="View/edit config")
    p_config.add_argument("--set", metavar="KEY=VALUE", help="Set a config value (e.g. --set ssid=MyHotspot)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands_without_deps = {"completion", "config", "profile"}
    if args.command not in commands_without_deps:
        _check_dependencies()

    dispatch = {
        "detect": cmd_detect,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "profile": cmd_profile,
        "config": cmd_config,
        "enable": cmd_enable,
        "disable": cmd_disable,
        "scan-usb": cmd_scan_usb,
        "recommend": cmd_recommend,
        "completion": cmd_completion,
    }

    try:
        dispatch[args.command](args)
    except KeyboardInterrupt:
        print(f"\n{C.DIM}Interrupted.{C.RESET}")
        sys.exit(130)


if __name__ == "__main__":
    main()
