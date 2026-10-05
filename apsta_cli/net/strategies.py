"""Ways of bringing a hotspot up, tried in order of preference.

Each strategy:

* says up front whether it can run (``unavailable`` returns a reason or None);
* builds the hotspot inside a :class:`Transaction`, so a failure part-way
  leaves nothing behind;
* returns a :class:`HotspotState` holding everything ``stop`` needs.

To add a strategy, subclass :class:`Strategy`, implement the three methods
and register it in ``STRATEGIES``.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..core import fsutil, output, paths, shell
from ..core.errors import HardwareError, SetupError, UsageError
from ..hw.capability import HardwareCapability
from ..hw.interfaces import WifiInterface, parse_iw_dev
from ..state import HotspotState
from . import dnsmasq, firewall, hostapd, iface, nm, subnet, supervisor, wpa
from .channels import Channel
from .transaction import Transaction


@dataclass
class StartContext:
    base: WifiInterface
    capability: HardwareCapability
    ssid: str
    password: str
    channel: Channel
    country: Optional[str]
    sta_ssid: Optional[str]
    allow_disconnect: bool
    sta_channel_usable: bool = True  # False: the AP can't share the WiFi's channel
    hidden: bool = False
    allowed_macs: List[str] = field(default_factory=list)
    channel_problem: Optional[HardwareError] = None  # why the WiFi's channel can't host, if it can't


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Strategy(ABC):
    name: str = ""
    description: str = ""
    keeps_wifi: bool = True

    @abstractmethod
    def unavailable(self, ctx: StartContext) -> Optional[str]:
        """Why this strategy can't be used here, or None if it can."""

    @abstractmethod
    def start(self, ctx: StartContext, tx: Transaction) -> HotspotState:
        """Bring the hotspot up, registering an undo step on ``tx`` after every change."""

    @abstractmethod
    def stop(self, state: HotspotState) -> None:
        """Best-effort teardown; must tolerate partially-gone resources."""

    def _base_state(self, ctx: StartContext, ap_iface: str) -> HotspotState:
        return HotspotState(
            method=self.name,
            base_interface=ctx.base.name,
            ap_interface=ap_iface,
            ssid=ctx.ssid,
            channel=ctx.channel.number,
            band=ctx.channel.band,
            same_channel_required=ctx.capability.same_channel_required,
            # Only a hotspot that keeps WiFi up has a connection for the watcher to follow.
            sta_ssid_at_start=ctx.sta_ssid if self.keeps_wifi else None,
            started_at=_now(),
        )


# ── hostapd + dnsmasq on a virtual interface ──────────────────────────────────


def hostapd_daemon() -> supervisor.Daemon:
    conf = str(paths.HOSTAPD_CONF)
    return supervisor.Daemon(
        name="hostapd",
        foreground=["hostapd", conf],
        background=["hostapd", "-B", "-P", str(paths.HOSTAPD_PID), conf],
        pidfile=paths.HOSTAPD_PID,
    )


def dnsmasq_daemon() -> supervisor.Daemon:
    conf = f"--conf-file={paths.DNSMASQ_CONF}"
    return supervisor.Daemon(
        name="dnsmasq",
        foreground=["dnsmasq", "--keep-in-foreground", conf],
        background=["dnsmasq", conf, f"--pid-file={paths.DNSMASQ_PID}"],
        pidfile=paths.DNSMASQ_PID,
    )


def share_connection(ap: str, tx: Transaction, state: HotspotState) -> None:
    """Address ``ap``, serve DHCP/DNS on it and NAT it to the uplink; records it in ``state``."""
    sup = supervisor.get()
    net = subnet.pick(subnet.networks_in_use())
    gateway, dhcp_start, dhcp_end = subnet.addresses(net)
    iface.assign_address(ap, f"{gateway}/{net.prefixlen}")

    fsutil.remove(paths.DNSMASQ_LEASES)
    fsutil.atomic_write(
        paths.DNSMASQ_CONF,
        dnsmasq.render(dnsmasq.DnsmasqConfig(ap, gateway, dhcp_start, dhcp_end, str(paths.DNSMASQ_LEASES))),
    )
    tx.on_rollback("remove dnsmasq.conf", lambda: fsutil.remove(paths.DNSMASQ_CONF))
    dnsmasq_d = dnsmasq_daemon()
    sup.start(dnsmasq_d)
    tx.on_rollback("stop dnsmasq", lambda: sup.stop(dnsmasq_d))

    fw = firewall.apply(ap, str(net))
    tx.on_rollback("remove NAT rules", lambda: firewall.revert(ap, str(net), fw))

    state.subnet = str(net)
    state.gateway = gateway
    state.supervisor = sup.kind
    state.firewall = fw


def unshare_connection(state: HotspotState) -> None:
    if state.subnet:
        firewall.revert(state.ap_interface, state.subnet, state.firewall)
    supervisor.get(state.supervisor).stop(dnsmasq_daemon())
    for path in (paths.DNSMASQ_CONF, paths.DNSMASQ_LEASES):
        fsutil.remove(path)


class HostapdStrategy(Strategy):
    name = "hostapd"
    description = "hostapd + dnsmasq on a virtual interface (keeps WiFi, client management)"

    def unavailable(self, ctx: StartContext) -> Optional[str]:
        if not ctx.capability.ap_sta:
            return "the radio cannot run an AP and a WiFi connection at the same time"
        if not ctx.sta_channel_usable:
            return "the WiFi connection's channel can't host a hotspot on this card"
        missing = [b for b in ("hostapd", "dnsmasq") if not shell.have(b)]
        if missing:
            return f"{' and '.join(missing)} not installed"
        return None

    def start(self, ctx: StartContext, tx: Transaction) -> HotspotState:
        paths.ensure_run_dir()
        sup = supervisor.get()

        nm.keep_away(iface.ap_name(ctx.base.name))
        tx.on_rollback("let NetworkManager manage the interface again", nm.release)
        ap, _mac = iface.create_virtual_ap(ctx.base.name)
        tx.on_rollback(f"delete {ap}", lambda: iface.delete(ap))
        nm.set_managed(ap, False)

        accept_file = None
        if ctx.allowed_macs:
            fsutil.atomic_write(paths.HOSTAPD_ACCEPT, hostapd.render_accept(ctx.allowed_macs), mode=0o600)
            tx.on_rollback("remove the MAC allowlist", lambda: fsutil.remove(paths.HOSTAPD_ACCEPT))
            accept_file = str(paths.HOSTAPD_ACCEPT)
        cfg = hostapd.HostapdConfig(
            ap, ctx.ssid, ctx.password, ctx.channel, ctx.country, str(paths.HOSTAPD_CTRL_DIR), ctx.hidden, accept_file
        )
        fsutil.atomic_write(paths.HOSTAPD_CONF, hostapd.render(cfg), mode=0o600)
        tx.on_rollback("remove hostapd.conf", lambda: fsutil.remove(paths.HOSTAPD_CONF))

        hostapd_d = hostapd_daemon()
        sup.start(hostapd_d)
        tx.on_rollback("stop hostapd", lambda: sup.stop(hostapd_d))
        if not hostapd.wait_enabled(ap):
            logs = sup.logs(hostapd_d)
            raise SetupError(
                f"hostapd did not start broadcasting on {ap}.",
                hints=logs.splitlines()[-4:] if logs else ["Run with APSTA_DEBUG=1 for details."],
            )

        state = self._base_state(ctx, ap)
        share_connection(ap, tx, state)
        state.allowed_macs = list(ctx.allowed_macs)
        return state

    def stop(self, state: HotspotState) -> None:
        unshare_connection(state)
        supervisor.get(state.supervisor).stop(hostapd_daemon())
        if iface.exists(state.ap_interface):
            iface.delete(state.ap_interface)
        for path in (paths.HOSTAPD_CONF, paths.HOSTAPD_ACCEPT):
            fsutil.remove(path)
        nm.release()

    @staticmethod
    def daemons_running(state: HotspotState) -> bool:
        return supervisor.get(state.supervisor).running(hostapd_daemon())


# ── Wi-Fi Direct group owner on its own channel ───────────────────────────────


def _group_interfaces(phy: Optional[str]) -> Dict[str, Optional[str]]:
    """Wi-Fi Direct group owner interfaces on ``phy``: name -> SSID (None until it beacons)."""
    found = parse_iw_dev(shell.out(["iw", "dev"]))
    return {i.name: i.connected_ssid for i in found if i.iftype == "P2P-GO" and (phy is None or i.phy == phy)}


def _wait_for_group(phy: Optional[str], before: set, timeout: float = 15.0) -> Optional[str]:
    deadline = time.monotonic() + timeout
    while True:
        for name, ssid in _group_interfaces(phy).items():
            if name not in before and ssid:
                return name
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


class P2pStrategy(Strategy):
    """What Windows' Mobile Hotspot does: the hotspot gets a channel of its own.

    Used when the WiFi connection's channel can't host an AP (DFS, or "no IR"
    on the card) on cards that pin an AP to that channel but let a Wi-Fi
    Direct group owner use another one. The radio switches between the two
    channels, so the hotspot and the WiFi share its airtime.
    """

    name = "p2p"
    description = "Wi-Fi Direct group on its own channel (keeps WiFi, shares the radio's speed)"

    def unavailable(self, ctx: StartContext) -> Optional[str]:
        if ctx.allowed_macs:
            return "Wi-Fi Direct can't limit which devices join (allowed_macs needs hostapd)"
        if not ctx.capability.p2p_go_own_channel:
            return "the card can't run a Wi-Fi Direct group on a channel of its own"
        if not shell.have("dnsmasq"):
            return "dnsmasq not installed"
        if not wpa.available(ctx.base.name):
            return f"wpa_supplicant has no Wi-Fi Direct device for {ctx.base.name} (p2p-dev-{ctx.base.name})"
        return None

    def start(self, ctx: StartContext, tx: Transaction) -> HotspotState:
        paths.ensure_run_dir()
        base, phy = ctx.base.name, ctx.base.phy
        before = set(_group_interfaces(phy))

        net_id = wpa.add_group_network(base, ctx.ssid, ctx.password, ctx.hidden)
        tx.on_rollback("forget the Wi-Fi Direct network", lambda: wpa.remove_network(base, net_id))

        def remove_groups() -> None:
            for name in set(_group_interfaces(phy)) - before:
                wpa.remove_group(base, name)
                if iface.exists(name):
                    iface.delete(name)

        wpa.start_group(base, net_id, ctx.channel.freq)
        tx.on_rollback("stop the Wi-Fi Direct group", remove_groups)
        ap = _wait_for_group(phy, before)
        if ap is None:
            raise SetupError(f"wpa_supplicant didn't start the Wi-Fi Direct group on channel {ctx.channel.number}.")
        nm.set_managed(ap, False)  # NetworkManager must not run DHCP or a hotspot of its own on it

        state = self._base_state(ctx, ap)
        # The group keeps its channel wherever the WiFi goes: nothing to follow.
        state.same_channel_required = False
        state.p2p_network = net_id
        share_connection(ap, tx, state)
        if ctx.sta_ssid:
            output.info(
                f"The hotspot has its own channel ({ctx.channel.number}); the card switches between it "
                f"and '{ctx.sta_ssid}', so they share its speed."
            )
        return state

    def stop(self, state: HotspotState) -> None:
        unshare_connection(state)
        base = state.base_interface
        wpa.remove_group(base, state.ap_interface)
        if iface.exists(state.ap_interface):
            iface.delete(state.ap_interface)
        if state.p2p_network is not None:
            wpa.remove_network(base, state.p2p_network)

    @staticmethod
    def daemons_running(state: HotspotState) -> bool:
        return supervisor.get(state.supervisor).running(dnsmasq_daemon())


# ── NetworkManager ────────────────────────────────────────────────────────────


class _NmStrategy(Strategy):
    virtual = True

    @staticmethod
    def _unsupported(ctx: StartContext) -> Optional[str]:
        # NetworkManager has no MAC filter for hotspots; never run one open to all.
        if ctx.allowed_macs:
            return "NetworkManager can't limit which devices join (allowed_macs needs hostapd)"
        return None

    def start(self, ctx: StartContext, tx: Transaction) -> HotspotState:
        if self.virtual:
            ap, mac = iface.create_virtual_ap(ctx.base.name)
            tx.on_rollback(f"delete {ap}", lambda: iface.delete(ap))
            nm.set_managed(ap, True)
            if not nm.wait_until_available(ap):
                output.warn(f"NetworkManager hasn't adopted {ap} yet; trying anyway.")
        else:
            ap, mac = ctx.base.name, None

        nm.install_connection(
            nm.render_keyfile(
                interface=ap,
                ssid=ctx.ssid,
                password=ctx.password,
                channel=ctx.channel,
                cloned_mac=mac,
                hidden=ctx.hidden,
            )
        )
        tx.on_rollback("delete NM connection", nm.remove_connection)
        nm.up()
        tx.on_rollback("deactivate NM connection", nm.down)
        if not iface.wait_for_broadcast(ap, timeout=10):
            raise SetupError(f"NetworkManager reported success but {ap} is not broadcasting.")

        state = self._base_state(ctx, ap)
        state.connection_id = nm.CONNECTION_ID
        return state

    def stop(self, state: HotspotState) -> None:
        nm.down()
        nm.remove_connection()
        if self.virtual and state.ap_interface != state.base_interface and iface.exists(state.ap_interface):
            iface.delete(state.ap_interface)


class NmVirtualStrategy(_NmStrategy):
    name = "nmcli"
    description = "NetworkManager shared connection on a virtual interface (keeps WiFi)"

    def unavailable(self, ctx: StartContext) -> Optional[str]:
        if self._unsupported(ctx):
            return self._unsupported(ctx)
        if not ctx.capability.ap_sta:
            return "the radio cannot run an AP and a WiFi connection at the same time"
        if not ctx.sta_channel_usable:
            return "the WiFi connection's channel can't host a hotspot on this card"
        if not shell.have("nmcli"):
            return "NetworkManager (nmcli) not installed"
        return None


class NmSingleStrategy(_NmStrategy):
    name = "nmcli-single"
    description = "NetworkManager hotspot on the WiFi interface itself (disconnects WiFi)"
    keeps_wifi = False
    virtual = False

    def unavailable(self, ctx: StartContext) -> Optional[str]:
        if self._unsupported(ctx):
            return self._unsupported(ctx)
        if not ctx.capability.supports_ap:
            return "the WiFi card does not support AP mode"
        if not shell.have("nmcli"):
            return "NetworkManager (nmcli) not installed"
        if ctx.sta_ssid and not ctx.allow_disconnect:
            return f"it would disconnect you from '{ctx.sta_ssid}' (pass --allow-disconnect to accept)"
        return None


STRATEGIES: List[Strategy] = [HostapdStrategy(), NmVirtualStrategy(), P2pStrategy(), NmSingleStrategy()]
BY_NAME: Dict[str, Strategy] = {s.name: s for s in STRATEGIES}
# Names used in state/CLI by apsta <= 0.6.
BY_NAME["nmcli-force"] = BY_NAME["nmcli-single"]


def for_state(state: HotspotState) -> Strategy:
    return BY_NAME.get(state.method, BY_NAME["nmcli-single"])


def candidates(method: str) -> List[Strategy]:
    if method in ("", "auto", None):
        return list(STRATEGIES)
    if method not in BY_NAME:
        raise UsageError(f"Unknown method '{method}'. Choose from: auto, {', '.join(s.name for s in STRATEGIES)}")
    return [BY_NAME[method]]


def describe_unavailable(reasons: Dict[str, str]) -> List[str]:
    return [f"{name}: {reason}" for name, reason in reasons.items()]


def warn_disconnect(ctx: StartContext) -> None:
    if ctx.sta_ssid:
        output.warn(f"Your WiFi connection to '{ctx.sta_ssid}' will drop while the hotspot runs.")
