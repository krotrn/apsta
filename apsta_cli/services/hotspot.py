"""Start, stop and inspect the hotspot.

This layer turns configuration + hardware facts into a :class:`StartContext`,
tries the strategies in order and records the result. It never prints how to
fix things itself; errors carry hints and the CLI renders them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .. import state as state_store
from ..config import model, store
from ..core import lock, output
from ..core.errors import AlreadyRunning, ApstaError, HardwareError, PermissionDenied, SetupError, UsageError
from ..hw import capability, interfaces
from ..net import channels, clients, iface, nm, strategies
from ..net.strategies import StartContext, Strategy
from ..net.transaction import Transaction
from ..state import HotspotState
from . import autostart


def require_root(action: str = "This command") -> None:
    if os.geteuid() != 0:
        raise PermissionDenied(f"{action} needs root.", hints=["Run it with sudo."])


@dataclass
class StartOptions:
    method: str = "auto"
    allow_disconnect: bool = False
    wait_sta: float = 0.0
    interface: Optional[str] = None


@dataclass
class StartResult:
    state: HotspotState
    strategy: Strategy
    generated_password: Optional[str] = None
    skipped: Dict[str, str] = field(default_factory=dict)


# ── liveness ──────────────────────────────────────────────────────────────────


def is_alive(st: HotspotState) -> bool:
    if not state_store.interface_exists(st.ap_interface):
        return False
    if not iface.is_broadcasting(st.ap_interface):
        return False
    if st.method == "hostapd":
        return strategies.HostapdStrategy.daemons_running(st)
    return True


def _reap_stale() -> None:
    st = state_store.load()
    if st is None:
        return
    if is_alive(st):
        raise AlreadyRunning(
            f"A hotspot is already running on {st.ap_interface} ('{st.ssid}').",
            hints=["Stop it first: sudo apsta stop"],
        )
    output.warn("Cleaning up a hotspot that stopped unexpectedly.")
    strategies.for_state(st).stop(st)
    state_store.clear()


# ── start ─────────────────────────────────────────────────────────────────────


def select_interface(config: dict, override: Optional[str]) -> interfaces.WifiInterface:
    ifaces = interfaces.client_interfaces()
    if not ifaces:
        raise HardwareError("No WiFi interfaces found.", hints=["Check: apsta detect"])
    wanted = override or config.get("interface")
    if wanted:
        for iface in ifaces:
            if iface.name == wanted:
                return iface
        raise UsageError(
            f"Configured interface '{wanted}' not found.",
            hints=["Reset to auto-detect: sudo apsta config --set interface=auto", "List interfaces: apsta detect"],
        )
    # Prefer the interface that carries the current WiFi connection.
    return sorted(ifaces, key=lambda i: (i.connected_ssid is None, i.state != "UP", i.name))[0]


def ensure_password(config: dict) -> Optional[str]:
    """Generate and save a random password if the active profile has none."""
    if config.get("password"):
        return None
    password = store.generate_password()
    model.set_field(config, "password", password)
    store.save(config)
    return password


def build_context(config: dict, opts: StartOptions) -> StartContext:
    base = select_interface(config, opts.interface)
    cap = capability.probe(base.name)
    if not cap.supports_ap:
        raise HardwareError(
            f"{base.name} does not support AP (hotspot) mode.",
            hints=["See which USB adapters work: apsta recommend"],
        )
    link = interfaces.wait_for_sta(base.name, opts.wait_sta) if opts.wait_sta else interfaces.sta_link(base.name)
    sta_channel = channels.from_freq(link.freq) if link else None
    pinned = sta_channel is not None and cap.same_channel_required
    scan = () if pinned else list(channels.parse_nmcli_scan(nm.scan(base.name)))
    plan = channels.plan(sta_channel, cap.same_channel_required, config["band"], config.get("channel"), scan)
    output.dbg("Channel plan", channel=plan.channel.number, band=plan.channel.band, reason=plan.reason)
    return StartContext(
        base=base,
        capability=cap,
        ssid=config["ssid"],
        password=config["password"],
        channel=plan.channel,
        country=interfaces.reg_country(cap.phy or base.phy),
        sta_ssid=link.ssid if link else None,
        allow_disconnect=opts.allow_disconnect,
    )


def start(opts: StartOptions, candidates: Optional[Sequence[Strategy]] = None) -> StartResult:
    require_root("Starting the hotspot")
    with lock.command_lock("start"):
        _reap_stale()
        config = store.load()
        generated = ensure_password(config)
        ctx = build_context(config, opts)

        skipped: Dict[str, str] = {}
        failures: List[str] = []
        for strategy in candidates if candidates is not None else strategies.candidates(opts.method):
            reason = strategy.unavailable(ctx)
            if reason:
                skipped[strategy.name] = reason
                output.dbg("Strategy unavailable", strategy=strategy.name, reason=reason)
                continue
            output.info(f"Using {strategy.description}")
            if not strategy.keeps_wifi:
                strategies.warn_disconnect(ctx)
            try:
                with Transaction() as tx:
                    st = strategy.start(ctx, tx)
                    state_store.save(st)
                    tx.commit()
            except SetupError as exc:
                output.warn(f"{strategy.name}: {exc.message}")
                failures.append(f"{strategy.name}: {exc.message}")
                continue
            return StartResult(st, strategy, generated, skipped)

        hints = [f"{name}: {why}" for name, why in skipped.items()] + failures
        raise ApstaError("Could not start the hotspot.", hints=hints)


# ── stop / status ─────────────────────────────────────────────────────────────


def stop() -> Optional[HotspotState]:
    require_root("Stopping the hotspot")
    with lock.command_lock("stop"):
        st = state_store.load()
        if st is None:
            return None
        strategies.for_state(st).stop(st)
        state_store.clear()
        return st


def current() -> Optional[HotspotState]:
    """The running hotspot, or None (also when the recorded one has died)."""
    st = state_store.load()
    return st if st is not None and is_alive(st) else None


def status() -> dict:
    st = state_store.load()
    alive = st is not None and is_alive(st)
    config = store.load()
    return {
        "active": alive,
        "stale": st is not None and not alive,
        "hotspot": st.to_dict() if alive else None,
        "clients": [c.to_dict() for c in clients.list_clients(st)] if alive else [],
        "interfaces": [interfaces.to_json(i) for i in interfaces.list_wifi_interfaces()],
        "autostart": autostart.info(),
        "config": {
            "active_profile": model.active_name(config),
            "profiles": model.profile_names(config),
            "ssid": config["ssid"],
            "band": config["band"],
            "channel": config["channel"],
            "interface": config["interface"],
        },
    }


def with_running_state(action: str):
    """Load the live state for a client-management action (root, locked)."""
    require_root(action)
    st = current()
    if st is None:
        raise ApstaError("The hotspot is not running.", hints=["Start it: sudo apsta start"])
    return st
