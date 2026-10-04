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

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..core import fsutil, output, paths, shell
from ..core.errors import SetupError, UsageError
from ..hw.capability import HardwareCapability
from ..hw.interfaces import WifiInterface
from ..state import HotspotState
from . import dnsmasq, firewall, hostapd, iface, nm, subnet, supervisor
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
    def start(self, ctx: StartContext, tx: Transaction) -> HotspotState: ...

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
            sta_ssid_at_start=ctx.sta_ssid,
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


class HostapdStrategy(Strategy):
    name = "hostapd"
    description = "hostapd + dnsmasq on a virtual interface (keeps WiFi, client management)"

    def unavailable(self, ctx: StartContext) -> Optional[str]:
        if not ctx.capability.ap_sta:
            return "the radio cannot run an AP and a WiFi connection at the same time"
        missing = [b for b in ("hostapd", "dnsmasq") if not shell.have(b)]
        if missing:
            return f"{' and '.join(missing)} not installed"
        return None

    def start(self, ctx: StartContext, tx: Transaction) -> HotspotState:
        paths.ensure_run_dir()
        sup = supervisor.get()

        ap, _mac = iface.create_virtual_ap(ctx.base.name)
        tx.on_rollback(f"delete {ap}", lambda: iface.delete(ap))
        nm.set_managed(ap, False)

        net = subnet.pick(subnet.networks_in_use())
        gateway, dhcp_start, dhcp_end = subnet.addresses(net)

        fsutil.atomic_write(
            paths.HOSTAPD_CONF,
            hostapd.render(
                hostapd.HostapdConfig(ap, ctx.ssid, ctx.password, ctx.channel, ctx.country, str(paths.HOSTAPD_CTRL_DIR))
            ),
            mode=0o600,
        )
        tx.on_rollback("remove hostapd.conf", lambda: fsutil.remove(paths.HOSTAPD_CONF))

        hostapd_d = hostapd_daemon()
        sup.start(hostapd_d)
        tx.on_rollback("stop hostapd", lambda: sup.stop(hostapd_d))
        if not iface.wait_for_ap_mode(ap):
            logs = sup.logs(hostapd_d)
            raise SetupError(
                f"hostapd did not bring {ap} up in AP mode.",
                hints=logs.splitlines()[-4:] if logs else ["Run with APSTA_DEBUG=1 for details."],
            )

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

        state = self._base_state(ctx, ap)
        state.subnet = str(net)
        state.gateway = gateway
        state.supervisor = sup.kind
        state.firewall = fw
        return state

    def stop(self, state: HotspotState) -> None:
        sup = supervisor.get(state.supervisor)
        if state.subnet:
            firewall.revert(state.ap_interface, state.subnet, state.firewall)
        sup.stop(dnsmasq_daemon())
        sup.stop(hostapd_daemon())
        if iface.exists(state.ap_interface):
            iface.delete(state.ap_interface)
        for path in (paths.HOSTAPD_CONF, paths.DNSMASQ_CONF, paths.DNSMASQ_LEASES):
            fsutil.remove(path)

    @staticmethod
    def daemons_running(state: HotspotState) -> bool:
        return supervisor.get(state.supervisor).running(hostapd_daemon())


# ── NetworkManager ────────────────────────────────────────────────────────────


class _NmStrategy(Strategy):
    virtual = True

    def start(self, ctx: StartContext, tx: Transaction) -> HotspotState:
        if self.virtual:
            ap, mac = iface.create_virtual_ap(ctx.base.name)
            tx.on_rollback(f"delete {ap}", lambda: iface.delete(ap))
            nm.set_managed(ap, True)
        else:
            ap, mac = ctx.base.name, None

        nm.install_connection(
            nm.render_keyfile(interface=ap, ssid=ctx.ssid, password=ctx.password, channel=ctx.channel, cloned_mac=mac)
        )
        tx.on_rollback("delete NM connection", nm.remove_connection)
        nm.up()
        tx.on_rollback("deactivate NM connection", nm.down)
        if not iface.wait_for_ap_mode(ap, timeout=5):
            raise SetupError(f"NetworkManager reported success but {ap} is not in AP mode.")

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
        if not ctx.capability.ap_sta:
            return "the radio cannot run an AP and a WiFi connection at the same time"
        if not shell.have("nmcli"):
            return "NetworkManager (nmcli) not installed"
        return None


class NmSingleStrategy(_NmStrategy):
    name = "nmcli-single"
    description = "NetworkManager hotspot on the WiFi interface itself (disconnects WiFi)"
    keeps_wifi = False
    virtual = False

    def unavailable(self, ctx: StartContext) -> Optional[str]:
        if not ctx.capability.supports_ap:
            return "the WiFi card does not support AP mode"
        if not shell.have("nmcli"):
            return "NetworkManager (nmcli) not installed"
        if ctx.sta_ssid and not ctx.allow_disconnect:
            return f"it would disconnect you from '{ctx.sta_ssid}' (pass --allow-disconnect to accept)"
        return None


STRATEGIES: List[Strategy] = [HostapdStrategy(), NmVirtualStrategy(), NmSingleStrategy()]
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
