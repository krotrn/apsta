"""start / stop / run."""

from __future__ import annotations

import json

from ..core import output
from ..core.output import C
from ..hw import interfaces
from ..services import guard, hotspot, watch
from .service import apsta_binary


def _options(args) -> hotspot.StartOptions:
    return hotspot.StartOptions(
        method=getattr(args, "method", None) or None,  # None: the profile's setting
        allow_disconnect=bool(getattr(args, "allow_disconnect", False)),
        wait_sta=float(getattr(args, "wait_sta", 0) or 0),
        interface=getattr(args, "interface", None),
    )


def _apply_overrides(args) -> None:
    """``start --ssid/--password-stdin``: save to the active profile first (one pkexec prompt for the GUI)."""
    from .config import apply_settings, read_password_stdin

    settings = {}
    if getattr(args, "ssid", None):
        settings["ssid"] = args.ssid
    if getattr(args, "password_stdin", False):
        settings["password"] = read_password_stdin()
    if settings:
        hotspot.require_root("Changing the configuration")
        apply_settings(settings, quiet=True)


def cmd_start(args) -> int:
    hotspot.require_root("Starting the hotspot")
    _apply_overrides(args)
    if not args.json:
        output.head("apsta — Starting hotspot")
    opts = _options(args)
    if hotspot.current() is None:
        guard.stop()  # a watcher still retrying an earlier hotspot would race this start
    result = hotspot.start(opts)
    st = result.state
    watching = guard.launch(apsta_binary(), opts)

    if args.json:
        print(json.dumps({"hotspot": st.to_dict(), "method": result.strategy.name}, indent=2))
        return 0

    output.ok(
        f"Hotspot '{st.ssid}' is live on {C.BOLD}{st.ap_interface}{C.RESET} "
        f"({st.method}, channel {st.channel}, {'5 GHz' if st.band == 'a' else '2.4 GHz'})"
    )
    link = interfaces.sta_link(st.base_interface)
    if link and result.strategy.keeps_wifi:
        output.ok(f"Still connected to '{link.ssid}'")
    for note in st.notes:
        output.info(note)
    if st.subnet:
        output.info(f"Clients get addresses in {st.subnet} (gateway {st.gateway})")
    if result.generated_password:
        output.info("No password was set, so a random one was generated.")
    if output.is_interactive():
        # Only on a terminal: under systemd stdout lands in the journal.
        from ..config import store

        output.reveal_secret("Password", store.load()["password"])
    else:
        output.info("Show the password with: sudo apsta config --show-password")
    if watching and st.same_channel_required:
        output.info("If your WiFi network changes channel, the hotspot steps aside and comes back when it can.")
    output.info("Stop with: sudo apsta stop")
    output.blank()
    return 0


def cmd_stop(args) -> int:
    hotspot.require_root("Stopping the hotspot")
    output.head("apsta — Stopping hotspot")
    before = hotspot.current()
    watched = guard.stop()  # first, so it can't restart what is being stopped
    st = hotspot.stop() or before
    if st is None:
        output.info("Stopped waiting to restart the hotspot." if watched else "No hotspot is running.")
    else:
        output.ok(f"Hotspot '{st.ssid}' on {st.ap_interface} stopped.")
    output.blank()
    return 0


def cmd_run(args) -> int:
    hotspot.require_root("Running the hotspot service")
    watcher = watch.Watcher(_options(args))
    watcher.install_signal_handlers()
    return watcher.run()
