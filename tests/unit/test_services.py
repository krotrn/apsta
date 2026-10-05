import threading
import unittest
import unittest.mock as mock

from apsta_cli import state as state_store
from apsta_cli.config import store
from apsta_cli.core import paths
from apsta_cli.core.errors import AlreadyRunning, ApstaError, HardwareError, PermissionDenied, SetupError, UsageError
from apsta_cli.hw.capability import HardwareCapability
from apsta_cli.hw.interfaces import StaLink, WifiInterface
from apsta_cli.net.channels import Channel
from apsta_cli.services import guard, hotspot, watch
from apsta_cli.state import HotspotState
from tests.support import FakeShell, as_root, isolate_paths


def make_state(**kw):
    base = dict(
        method="fake",
        base_interface="wlo1",
        ap_interface="wlo1_ap",
        ssid="S",
        channel=6,
        band="bg",
        same_channel_required=True,
        sta_ssid_at_start="Home",
    )
    base.update(kw)
    return HotspotState(**base)


class FakeStrategy:
    keeps_wifi = True

    def __init__(self, name, reason=None, error=None):
        self.name = name
        self.description = f"fake {name}"
        self.reason = reason
        self.error = error
        self.rolled_back = False
        self.started = False

    def unavailable(self, ctx):
        return self.reason

    def start(self, ctx, tx):
        tx.on_rollback("undo", lambda: setattr(self, "rolled_back", True))
        if self.error:
            raise SetupError(self.error)
        self.started = True
        return make_state(method=self.name, channel=ctx.channel.number)

    def stop(self, st):
        pass


CAP = HardwareCapability("wlo1", "phy0", True, True, True, True, 1)
IFACE = WifiInterface("wlo1", "aa", "phy0", "managed", "UP", "Home")


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        isolate_paths(self)
        as_root(self)
        self.out = mock.patch("sys.stdout").start()
        self.err = mock.patch("sys.stderr").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch("apsta_cli.services.hotspot.interfaces.client_interfaces", return_value=[IFACE]).start()
        mock.patch("apsta_cli.services.hotspot.capability.probe", return_value=CAP).start()
        mock.patch("apsta_cli.services.hotspot.interfaces.sta_link", return_value=StaLink("Home", 2437)).start()
        mock.patch("apsta_cli.services.hotspot.interfaces.reg_country", return_value="IN").start()
        mock.patch("apsta_cli.services.hotspot.nm.scan", return_value="").start()


class StartTests(ServiceTestCase):
    def test_first_available_strategy_wins(self):
        a, b = FakeStrategy("a", reason="missing"), FakeStrategy("b")
        result = hotspot.start(hotspot.StartOptions(), [a, b])
        self.assertEqual(result.strategy, b)
        self.assertEqual(result.skipped, {"a": "missing"})
        self.assertEqual(result.state.channel, 6)  # follows the STA on 2437 MHz
        self.assertEqual(state_store.load().method, "b")

    def test_failed_strategy_is_rolled_back_and_next_tried(self):
        a, b = FakeStrategy("a", error="hostapd exploded"), FakeStrategy("b")
        result = hotspot.start(hotspot.StartOptions(), [a, b])
        self.assertTrue(a.rolled_back)
        self.assertEqual(result.strategy.name, "b")

    def test_all_fail_reports_every_reason(self):
        with self.assertRaises(ApstaError) as ctx:
            hotspot.start(hotspot.StartOptions(), [FakeStrategy("a", reason="nope"), FakeStrategy("b", error="boom")])
        self.assertEqual(ctx.exception.hints, ["a: nope", "b: boom"])
        self.assertIsNone(state_store.load())

    def test_generates_password_once(self):
        result = hotspot.start(hotspot.StartOptions(), [FakeStrategy("a")])
        self.assertTrue(result.generated_password)
        self.assertEqual(store.load()["password"], result.generated_password)

    def test_refuses_when_already_running(self):
        state_store.save(make_state())
        with mock.patch.object(hotspot, "is_alive", return_value=True):
            with self.assertRaises(AlreadyRunning):
                hotspot.start(hotspot.StartOptions(), [FakeStrategy("a")])

    def test_cleans_up_stale_state(self):
        state_store.save(make_state(method="hostapd"))
        stale = mock.Mock()
        with (
            mock.patch.object(hotspot, "is_alive", return_value=False),
            mock.patch("apsta_cli.net.strategies.for_state", return_value=stale),
        ):
            hotspot.start(hotspot.StartOptions(), [FakeStrategy("a")])
        stale.stop.assert_called_once()

    def test_requires_root(self):
        with mock.patch("os.geteuid", return_value=1000):
            with self.assertRaises(PermissionDenied):
                hotspot.start(hotspot.StartOptions(), [])

    def test_no_ap_mode(self):
        no_ap = HardwareCapability("wlo1", "phy0", False, True, False, True, 1)
        with mock.patch("apsta_cli.services.hotspot.capability.probe", return_value=no_ap):
            with self.assertRaises(HardwareError):
                hotspot.start(hotspot.StartOptions(), [FakeStrategy("a")])

    def test_wait_sta_used_when_requested(self):
        with mock.patch("apsta_cli.services.hotspot.interfaces.wait_for_sta", return_value=None) as wait:
            result = hotspot.start(hotspot.StartOptions(wait_sta=5), [FakeStrategy("a")])
        wait.assert_called_once_with("wlo1", 5)
        self.assertEqual(result.state.channel, 6)


class InterfaceSelectionTests(ServiceTestCase):
    def test_prefers_connected(self):
        idle = WifiInterface("wlan9", "bb", "phy1", "managed", "UP", None)
        with mock.patch("apsta_cli.services.hotspot.interfaces.client_interfaces", return_value=[idle, IFACE]):
            self.assertEqual(hotspot.select_interface({"interface": None}, None).name, "wlo1")

    def test_configured_missing(self):
        with self.assertRaises(UsageError):
            hotspot.select_interface({"interface": "wlx1"}, None)

    def test_override_and_none(self):
        self.assertEqual(hotspot.select_interface({"interface": "zzz"}, "wlo1").name, "wlo1")
        with mock.patch("apsta_cli.services.hotspot.interfaces.client_interfaces", return_value=[]):
            with self.assertRaises(HardwareError):
                hotspot.select_interface({}, None)


class StopStatusTests(ServiceTestCase):
    def test_stop(self):
        self.assertIsNone(hotspot.stop())
        state_store.save(make_state())
        strategy = mock.Mock()
        with mock.patch("apsta_cli.net.strategies.for_state", return_value=strategy):
            self.assertEqual(hotspot.stop().ssid, "S")
        strategy.stop.assert_called_once()
        self.assertIsNone(state_store.load())

    def test_is_alive(self):
        st = make_state(method="hostapd")
        self.assertFalse(hotspot.is_alive(st))
        (paths.SYSFS_NET / "wlo1_ap").mkdir()
        with (
            mock.patch("apsta_cli.services.hotspot.iface.is_broadcasting", return_value=True),
            mock.patch("apsta_cli.net.strategies.HostapdStrategy.daemons_running", return_value=True),
        ):
            self.assertTrue(hotspot.is_alive(st))
            self.assertTrue(hotspot.is_alive(make_state(method="nmcli")))
        with mock.patch("apsta_cli.services.hotspot.iface.is_broadcasting", return_value=False):
            self.assertFalse(hotspot.is_alive(st))

    def test_status_payload(self):
        state_store.save(make_state())
        with (
            mock.patch.object(hotspot, "is_alive", return_value=True),
            mock.patch("apsta_cli.services.hotspot.clients.list_clients", return_value=[]),
            mock.patch("apsta_cli.services.hotspot.interfaces.list_wifi_interfaces", return_value=[IFACE]),
        ):
            data = hotspot.status()
            self.assertTrue(data["active"])
            self.assertEqual(data["hotspot"]["ssid"], "S")
            self.assertEqual(data["interfaces"][0]["connected_ssid"], "Home")
            self.assertEqual(hotspot.with_running_state("x").ssid, "S")
        with (
            mock.patch.object(hotspot, "is_alive", return_value=False),
            mock.patch("apsta_cli.services.hotspot.interfaces.list_wifi_interfaces", return_value=[]),
        ):
            data = hotspot.status()
            self.assertTrue(data["stale"])
            with self.assertRaises(ApstaError):
                hotspot.with_running_state("x")


class DecideTests(unittest.TestCase):
    def obs(self, alive=True, ch=6):
        return watch.Observation(True, alive, Channel(ch, "bg") if ch else None)

    def test_exit_when_stopped(self):
        self.assertEqual(watch.decide(None, watch.Observation(False, False, None), 0, watch.Memory()), "exit")

    def test_restart_when_down(self):
        self.assertIn("went down", watch.decide(make_state(), self.obs(alive=False), 0, watch.Memory()))

    def test_follow_channel_change(self):
        self.assertIn("channel 11", watch.decide(make_state(), self.obs(ch=11), 0, watch.Memory()))
        self.assertIsNone(watch.decide(make_state(), self.obs(ch=6), 0, watch.Memory()))

    def test_multichannel_radios_ignore_sta(self):
        st = make_state(same_channel_required=False)
        self.assertIsNone(watch.decide(st, self.obs(ch=11), 0, watch.Memory()))
        self.assertIsNone(watch.decide(st, self.obs(ch=None), 100, watch.Memory()))

    def test_sta_loss_needs_grace_period(self):
        mem, st = watch.Memory(), make_state()
        self.assertIsNone(watch.decide(st, self.obs(ch=None), 100, mem))
        self.assertIsNone(watch.decide(st, self.obs(ch=None), 110, mem))
        self.assertIn("lost", watch.decide(st, self.obs(ch=None), 100 + watch.STA_LOST_GRACE, mem))
        self.assertIsNone(mem.sta_lost_since)

    def test_reconnect_resets_grace(self):
        mem, st = watch.Memory(), make_state()
        watch.decide(st, self.obs(ch=None), 100, mem)
        watch.decide(st, self.obs(ch=6), 105, mem)
        self.assertIsNone(watch.decide(st, self.obs(ch=None), 130, mem))

    def test_offline_start_never_cycles(self):
        st = make_state(sta_ssid_at_start=None)
        self.assertIsNone(watch.decide(st, self.obs(ch=None), 1000, watch.Memory()))


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.out = mock.patch("sys.stdout").start()
        mock.patch("sys.stderr").start()
        self.addCleanup(mock.patch.stopall)

    def test_restarts_then_exits_when_stopped_externally(self):
        states = iter([make_state(), make_state(), None])
        result = mock.Mock(state=make_state())
        with (
            mock.patch.object(watch.hotspot, "start", return_value=result) as start,
            mock.patch.object(watch.hotspot, "stop") as stop,
            mock.patch.object(watch.state_store, "load", side_effect=lambda: next(states)),
            mock.patch.object(
                watch,
                "observe",
                side_effect=[
                    watch.Observation(True, False, None),
                    watch.Observation(True, True, None),
                    watch.Observation(False, False, None),
                ],
            ),
        ):
            code = watch.Watcher(hotspot.StartOptions(), poll=0.001).run()
        self.assertEqual(code, 0)
        self.assertEqual(start.call_count, 2)
        stop.assert_called_once()

    def test_adopts_running_hotspot_and_stops_on_signal(self):
        watcher = watch.Watcher(hotspot.StartOptions(), poll=0.001)
        with (
            mock.patch.object(watch.hotspot, "start", side_effect=AlreadyRunning("x")),
            mock.patch.object(watch.hotspot, "stop") as stop,
            mock.patch.object(watch.state_store, "load", return_value=make_state()),
            mock.patch.object(
                watch,
                "observe",
                side_effect=lambda st: (watcher.stop_event.set(), watch.Observation(True, True, Channel(6, "bg")))[1],
            ),
        ):
            self.assertEqual(watcher.run(), 0)
        stop.assert_called_once()

    def test_retry_backoff_until_stopped(self):
        watcher = watch.Watcher(hotspot.StartOptions())
        calls = []

        def failing_start(opts):
            calls.append(1)
            if len(calls) == 2:
                watcher.stop_event.set()
            raise ApstaError("no radio", hints=["plug it in"])

        with (
            mock.patch.object(watch.hotspot, "start", side_effect=failing_start),
            mock.patch.object(watch, "RETRY_MIN", 0.001),
        ):
            self.assertEqual(watcher.run(), 0)
        self.assertEqual(len(calls), 2)

    def test_waits_for_wifi_without_holding_the_lock(self):
        watcher = watch.Watcher(hotspot.StartOptions(wait_sta=30))
        links = iter([None, None, StaLink("Home", 2437)])
        result = mock.Mock(state=make_state())
        with (
            mock.patch.object(watch.store, "load", return_value={}),
            mock.patch.object(watch.hotspot, "select_interface", return_value=IFACE),
            mock.patch.object(watch.interfaces, "sta_link", side_effect=lambda name: next(links)),
            mock.patch.object(watcher.stop_event, "wait", return_value=False) as wait,
            mock.patch.object(watch.hotspot, "start", return_value=result) as start,
        ):
            self.assertTrue(watcher._start())
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(start.call_args[0][0].wait_sta, 0)  # the wait already happened

    def test_stop_during_wifi_wait_skips_start(self):
        watcher = watch.Watcher(hotspot.StartOptions(wait_sta=30))
        watcher.stop_event.set()
        with (
            mock.patch.object(watch.store, "load", return_value={}),
            mock.patch.object(watch.hotspot, "select_interface", return_value=IFACE),
            mock.patch.object(watch.interfaces, "sta_link", return_value=None),
            mock.patch.object(watch.hotspot, "start") as start,
        ):
            self.assertEqual(watcher.run(), 0)
        start.assert_not_called()

    def test_signal_handlers(self):
        watcher = watch.Watcher(hotspot.StartOptions())
        with mock.patch("signal.signal") as sig:
            watcher.install_signal_handlers()
        handler = sig.call_args_list[0][0][1]
        handler(15, None)
        self.assertTrue(watcher.stop_event.is_set())

    def test_observe(self):
        self.assertFalse(watch.observe(None).state_present)
        with (
            mock.patch.object(watch.interfaces, "sta_link", return_value=StaLink("x", 2462)),
            mock.patch.object(watch.hotspot, "is_alive", return_value=True),
        ):
            obs = watch.observe(make_state())
        self.assertEqual(obs.sta_channel, Channel(11, "bg"))
        self.assertIsInstance(threading.Event(), type(watch.Watcher(hotspot.StartOptions()).stop_event))


if __name__ == "__main__":
    unittest.main()


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.sh = FakeShell()
        mock.patch("apsta_cli.core.shell.run", self.sh).start()
        mock.patch.object(guard.supervisor, "get", return_value=mock.Mock(kind="systemd")).start()
        self.addCleanup(mock.patch.stopall)

    def test_launch_runs_the_watcher_as_a_transient_unit(self):
        self.sh.on("systemctl", "is-active", "--quiet", "apsta.service", rc=3)
        opts = hotspot.StartOptions(method="hostapd", allow_disconnect=True, interface="wlo1")
        self.assertTrue(guard.launch("/usr/bin/apsta", opts))
        run = next(c for c in self.sh.calls if c[0] == "systemd-run")
        self.assertIn("--unit=apsta-watch.service", run)
        self.assertIn("--setenv=PYTHONUNBUFFERED=1", run)
        self.assertEqual(
            run[run.index("--") + 1 :],
            [
                "/usr/bin/apsta",
                "run",
                "--wait-sta",
                "30",
                "--method",
                "hostapd",
                "--interface",
                "wlo1",
                "--allow-disconnect",
            ],
        )
        self.assertFalse(self.sh.called("systemctl", "stop"))  # would take the new hotspot down

    def test_not_launched_when_the_service_watches(self):
        self.assertFalse(guard.launch("/usr/bin/apsta", hotspot.StartOptions()))
        self.assertFalse(self.sh.called("systemd-run"))

    def test_not_launched_without_systemd(self):
        guard.supervisor.get.return_value = mock.Mock(kind="pidfile")
        self.assertFalse(guard.launch("/usr/bin/apsta", hotspot.StartOptions()))
        self.assertFalse(guard.stop())
        self.assertEqual(self.sh.calls, [])

    def test_launch_failure_is_not_fatal(self):
        self.sh.on("systemctl", "is-active", rc=3).on("systemd-run", rc=1, stderr="no bus")
        self.assertFalse(guard.launch("/usr/bin/apsta", hotspot.StartOptions()))

    def test_stop(self):
        self.assertTrue(guard.stop())
        self.assertTrue(self.sh.called("systemctl", "stop", "apsta-watch.service"))
        self.sh.on("systemctl", "is-active", rc=3)
        self.assertFalse(guard.stop())


class AutostartTests(unittest.TestCase):
    def test_systemd(self):
        from apsta_cli.services import autostart
        from tests.support import FakeShell

        FakeShell(["systemctl"]).on("systemctl", "is-active", rc=3).install(self)
        self.assertEqual(autostart.info("systemd"), {"init": "systemd", "enabled": True, "running": False})

    def test_openrc_and_unknown(self):
        from apsta_cli.services import autostart
        from tests.support import FakeShell

        FakeShell(["rc-update"]).on("rc-update", "show", stdout="  apsta | default\n  sshd | default").install(self)
        self.assertTrue(autostart.info("openrc")["enabled"])
        self.assertIsNone(autostart.info("runit")["enabled"])
        self.assertIn(autostart.detect_init(), ("systemd", "openrc", "runit", "unknown"))
