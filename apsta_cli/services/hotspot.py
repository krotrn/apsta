"""Start, stop and inspect the hotspot.

This layer turns configuration + hardware facts into a :class:`StartContext`,
tries the strategies in order and records the result. It never prints how to
fix things itself; errors carry hints and the CLI renders them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .. import state as state_store
from ..config import model, store
from ..core import lock, log, output
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
    method: Optional[str] = None  # None: the profile's ``method`` setting
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
    if st.method == "p2p":
        return strategies.P2pStrategy.daemons_running(st)
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


def resolve_method(config: dict, opts: StartOptions) -> str:
    """``--method`` for this run, else the profile's setting."""
    return opts.method or config.get("method") or "auto"


# Methods that never share the WiFi's channel: the hotspot may use any channel the card allows.
OWN_CHANNEL_METHODS = ("p2p", "nmcli-single")


@dataclass
class ChannelChoice:
    plan: channels.ChannelPlan
    notes: List[str]  # the hotspot's channel explained, in words for the user
    problem: Optional[HardwareError] = None  # why the WiFi's channel can't host, if it can't

    @property
    def sta_channel_usable(self) -> bool:
        return self.problem is None


def choose_channel(
    cap: capability.HardwareCapability,
    sta_channel: Optional[channels.Channel],
    config: dict,
    method: str,
    allow_disconnect: bool,
    scan: Callable[[], Iterable[Tuple[int, int]]],
) -> ChannelChoice:
    """Share the WiFi's channel when the card needs to, else pick one; ``scan`` runs only for the latter."""
    allowed = channels.allowed_channels(cap.ap_frequencies)
    band, wanted = config["band"], config.get("channel")
    problem = None
    try:
        plan = channels.plan(sta_channel, cap.same_channel_required, band, wanted, (), allowed)
    except HardwareError as exc:
        if not (allow_disconnect or cap.p2p_go_own_channel or method in OWN_CHANNEL_METHODS):
            raise
        # The WiFi's channel can't host an AP. A Wi-Fi Direct group on a channel
        # of its own still can, or (if the user accepts dropping WiFi) the
        # single-interface method: either way, on a channel the card allows.
        problem = exc

    def own_channel() -> channels.ChannelPlan:
        return channels.plan(None, cap.same_channel_required, band, wanted, scan(), allowed)

    notes = [f"{problem.message.split('. ')[0]}."] if problem else []
    # The WiFi's channel, when the hotspot has to share it.
    tied = sta_channel if problem is None and cap.same_channel_required else None
    if tied is None:
        plan = own_channel()  # not tied to the WiFi: scan and pick
    elif method in OWN_CHANNEL_METHODS:
        # Forced to a method with its own channel: keep the WiFi's channel only
        # when it is what the settings ask for anyway (no radio switching then).
        if wanted not in (None, "auto") or tied.band != band:
            plan = own_channel()
        else:
            plan = channels.ChannelPlan(tied, "the same as your WiFi's, so the radio doesn't have to switch")
    else:
        notes.extend(_sharing_notes(cap, tied, band, wanted))
    notes.extend(plan.notes)  # what couldn't be followed, before what was done instead
    notes.append(f"Channel {plan.channel.number} ({plan.channel.label}): {plan.reason}.")
    return ChannelChoice(plan, notes, problem)


def _sharing_notes(
    cap: capability.HardwareCapability, tied: channels.Channel, band: str, wanted: Optional[str]
) -> List[str]:
    """Settings the hotspot can't follow because it shares the WiFi's channel."""
    if tied.band != band:
        hint = (
            "To always use your band, set method to p2p (Wi-Fi Direct; shares the radio's speed)."
            if cap.p2p_go_own_channel
            else "This card can't run the hotspot on another channel while connected."
        )
        return [
            f"Your band setting is {channels.Channel(1, band).label}, but the hotspot is on "
            f"{tied.label} because it shares your WiFi's channel. {hint}"
        ]
    if wanted not in (None, "auto") and int(wanted) != tied.number:
        return [f"Your channel setting ({wanted}) is not used: the hotspot shares your WiFi's channel."]
    return []


def _hardware(
    config: dict, opts: StartOptions
) -> Tuple[interfaces.WifiInterface, capability.HardwareCapability, Optional[interfaces.StaLink]]:
    """The interface to host on, what it can do, and its WiFi connection (waiting for it if asked)."""
    base = select_interface(config, opts.interface)
    cap = capability.probe(base.name)
    if not cap.supports_ap:
        raise HardwareError(
            f"{base.name} does not support AP (hotspot) mode.",
            hints=["See which USB adapters work: apsta recommend"],
        )
    link = interfaces.wait_for_sta(base.name, opts.wait_sta) if opts.wait_sta else interfaces.sta_link(base.name)
    return base, cap, link


def build_context(config: dict, opts: StartOptions, method: str = "auto") -> StartContext:
    base, cap, link = _hardware(config, opts)
    sta_channel = channels.from_freq(link.freq) if link else None
    choice = choose_channel(
        cap,
        sta_channel,
        config,
        method,
        opts.allow_disconnect,
        scan=lambda: list(channels.parse_nmcli_scan(nm.scan(base.name))),
    )
    plan = choice.plan
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
        sta_channel_usable=choice.sta_channel_usable,
        hidden=bool(config.get("hidden")),
        allowed_macs=list(config.get("allowed_macs") or []),
        channel_problem=choice.problem,
        notes=choice.notes,
        sta_channel=sta_channel,
    )


def start(opts: StartOptions, candidates: Optional[Sequence[Strategy]] = None) -> StartResult:
    require_root("Starting the hotspot")
    with lock.command_lock("start"):
        _reap_stale()
        config = store.load()
        generated = ensure_password(config)
        method = resolve_method(config, opts)
        ctx = build_context(config, opts, method)

        skipped: Dict[str, str] = {}
        failures: List[str] = []
        for strategy in candidates if candidates is not None else strategies.candidates(method):
            reason = strategy.unavailable(ctx)
            if reason:
                skipped[strategy.name] = reason
                output.dbg("Strategy unavailable", strategy=strategy.name, reason=reason)
                continue
            output.info(f"Using {strategy.description}")
            if not strategy.keeps_wifi and ctx.sta_ssid:
                output.warn(f"Your WiFi connection to '{ctx.sta_ssid}' will drop while the hotspot runs.")
            try:
                with Transaction() as tx:
                    st = strategy.start(ctx, tx)
                    st.notes = _decision_notes(method, strategy, ctx, skipped, failures) + st.notes
                    state_store.save(st)
                    tx.commit()
            except SetupError as exc:
                output.warn(f"{strategy.name}: {exc.message}")
                failures.append(f"{strategy.name}: {exc.message}")
                continue
            log.event("info", "hotspot_started", method=st.method, channel=st.channel, notes=st.notes)
            return StartResult(st, strategy, generated, skipped)

        hints = [f"{name}: {why}" for name, why in skipped.items()] + failures
        if ctx.channel_problem is not None:
            # Say why the WiFi's channel doesn't work, not only why each method was skipped.
            problem = ctx.channel_problem
            tried = [h for h in hints if h.startswith(("p2p:", "nmcli-single:"))]
            raise HardwareError(problem.message, hints=problem.hints + tried)
        raise ApstaError("Could not start the hotspot.", hints=hints)


def _decision_notes(
    method: str, strategy: Strategy, ctx: StartContext, skipped: Dict[str, str], failures: List[str]
) -> List[str]:
    """Why the hotspot runs the way it does, in words for the user (CLI, status, GUI, log)."""
    if method != "auto":
        notes = [f"Method {strategy.name}: chosen in your settings."]
    elif not skipped and not failures:
        notes = [f"Method {strategy.name}: the best one for this card and setup."]
    else:
        by_reason: Dict[str, List[str]] = {}
        for name, why in skipped.items():
            by_reason.setdefault(why, []).append(name)
        passed = [
            f"{' and '.join(names)} {'was' if len(names) == 1 else 'were'} skipped ({why})"
            for why, names in by_reason.items()
        ]
        passed += [f"{failure} (failed)" for failure in failures]
        notes = [f"Method {strategy.name}: " + "; ".join(passed) + "."]
    return notes + ctx.notes


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
    live = st if st is not None and is_alive(st) else None
    config = store.load()
    return {
        "active": live is not None,
        "stale": st is not None and live is None,
        "hotspot": live.to_dict() if live else None,
        "clients": [c.to_dict() for c in clients.list_clients(live)] if live else [],
        "interfaces": [interfaces.to_json(i) for i in interfaces.list_wifi_interfaces()],
        "autostart": autostart.info(),
        "config": {
            "active_profile": model.active_name(config),
            "profiles": model.profile_names(config),
            "ssid": config["ssid"],
            "band": config["band"],
            "channel": config["channel"],
            "method": config["method"],
            "interface": config["interface"],
            "hidden": config["hidden"],
            "allowed_macs": config["allowed_macs"],
        },
    }


def with_running_state(action: str):
    """Load the live state for a client-management action (root, locked)."""
    require_root(action)
    st = current()
    if st is None:
        raise ApstaError("The hotspot is not running.", hints=["Start it: sudo apsta start"])
    return st
