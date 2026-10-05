"""Watch a hotspot started with ``apsta start``, as the service would.

On a single-channel card the hotspot holds the WiFi's channel. If the network
moves (a phone hotspot switching to 5 GHz), the WiFi can't follow while the
hotspot runs, so the laptop loses internet until someone stops the hotspot.
``start`` therefore launches ``apsta run`` as a transient systemd unit: it
adopts the running hotspot and stops, moves or restarts it like the service.

Only on systemd, and not when apsta.service already does the watching.
"""

from __future__ import annotations

from typing import List

from ..core import output, shell
from ..net import supervisor
from .hotspot import StartOptions

UNIT = "apsta-watch.service"
SERVICE_UNIT = "apsta.service"
WAIT_STA = 30


def available() -> bool:
    return supervisor.get().kind == "systemd"


def run_argv(binary: str, opts: StartOptions) -> List[str]:
    argv = [binary, "run", "--wait-sta", str(WAIT_STA), "--method", opts.method]
    if opts.interface:
        argv += ["--interface", opts.interface]
    if opts.allow_disconnect:
        argv.append("--allow-disconnect")
    return argv


def launch(binary: str, opts: StartOptions) -> bool:
    """Start the watcher for the hotspot that is up now; False if none was started."""
    if not available() or shell.run(["systemctl", "is-active", "--quiet", SERVICE_UNIT]).ok:
        return False
    # Never stop a running watcher here: it would take the new hotspot down with it.
    shell.run(["systemctl", "reset-failed", UNIT])
    result = shell.run(
        [
            "systemd-run",
            f"--unit={UNIT}",
            "--collect",
            "--quiet",
            "--description=apsta watcher",
            # Line by line into the journal, not in blocks when the buffer fills.
            "--setenv=PYTHONUNBUFFERED=1",
            "--",
            *run_argv(binary, opts),
        ]
    )
    if not result.ok:
        output.dbg("Watcher not started", stderr=result.stderr.strip())
    return result.ok


def active() -> bool:
    return available() and shell.run(["systemctl", "is-active", "--quiet", UNIT]).ok


def stop() -> bool:
    """Stop the watcher; True if one was running."""
    if not active():
        return False
    shell.run(["systemctl", "stop", UNIT], timeout=45)
    return True
