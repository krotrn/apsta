"""Command-layer paths not reached by the integration tests."""

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from apsta_cli.cmd import config as config_cmd
from apsta_cli.cmd import hotspot as hotspot_cmd
from apsta_cli.core.errors import UsageError
from apsta_cli.net import iface, supervisor
from tests.support import FakeShell, as_root, isolate_paths


class RunCommandTests(unittest.TestCase):
    def test_run_starts_a_watcher_with_signal_handlers(self):
        as_root(self)
        args = SimpleNamespace(method="hostapd", allow_disconnect=True, wait_sta=30, interface="wlo1")
        with mock.patch.object(hotspot_cmd.watch, "Watcher") as watcher_cls:
            watcher_cls.return_value.run.return_value = 0
            self.assertEqual(hotspot_cmd.cmd_run(args), 0)
        opts = watcher_cls.call_args[0][0]
        self.assertEqual(
            (opts.method, opts.allow_disconnect, opts.wait_sta, opts.interface), ("hostapd", True, 30.0, "wlo1")
        )
        watcher_cls.return_value.install_signal_handlers.assert_called_once()


class PasswordPromptTests(unittest.TestCase):
    def test_interactive_prompt_asks_twice(self):
        with (
            mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)),
            mock.patch.object(config_cmd.getpass, "getpass", side_effect=["s3cret-pass", "s3cret-pass"]),
        ):
            self.assertEqual(config_cmd.read_password_stdin(), "s3cret-pass")

    def test_interactive_mismatch(self):
        with (
            mock.patch("sys.stdin", mock.Mock(isatty=lambda: True)),
            mock.patch.object(config_cmd.getpass, "getpass", side_effect=["one-password", "another-one"]),
        ):
            with self.assertRaises(UsageError):
                config_cmd.read_password_stdin()

    def test_piped_input(self):
        with mock.patch("sys.stdin", io.StringIO("from-a-pipe\r\n")):
            self.assertEqual(config_cmd.read_password_stdin(), "from-a-pipe")


STUBBORN = """#!{python}
import os, signal, sys, time
pid = os.fork()
if pid:
    open(sys.argv[1], "w").write(str(pid))
    sys.exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a daemon that won't exit politely
os.setsid()
for fd in (0, 1, 2):
    os.close(fd)
time.sleep(60)
"""


class SupervisorEscalationTests(unittest.TestCase):
    def test_sigkill_after_ignored_sigterm(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "stubborn"
            exe.write_text(STUBBORN.format(python=sys.executable))
            exe.chmod(0o755)
            pidfile = Path(td) / "pid"
            d = supervisor.Daemon("stubborn", [], [str(exe), str(pidfile)], pidfile)
            sup = supervisor.PidfileSupervisor()
            sup.start(d)
            for _ in range(50):
                if sup.running(d):
                    break
                time.sleep(0.05)
            pid = int(pidfile.read_text())
            started = time.monotonic()
            sup.stop(d)
            self.assertLess(time.monotonic() - started, 10)
            time.sleep(0.2)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)  # gone (reaped by init once killed)


class InterfaceTests(unittest.TestCase):
    def test_wait_for_ap_mode_polls_until_ap(self):
        types = iter(["managed", "managed", "AP"])
        with (
            mock.patch.object(iface, "iface_type", side_effect=lambda _n: next(types)),
            mock.patch.object(iface.time, "sleep"),
        ):
            self.assertTrue(iface.wait_for_ap_mode("wlo1_ap", timeout=5))

    def test_wait_for_ap_mode_times_out(self):
        with mock.patch.object(iface, "iface_type", return_value="managed"):
            self.assertFalse(iface.wait_for_ap_mode("wlo1_ap", timeout=0.01))

    def test_create_replaces_leftover_and_warns_on_mac_failure(self):
        root = isolate_paths(self)
        (root / "sys/class/net/wlo1_ap").mkdir(parents=True)
        sh = FakeShell().on("ip", "link", "set", "wlo1_ap", "address", rc=2, stderr="busy").install(self)
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            name, mac = iface.create_virtual_ap("wlo1")
        self.assertEqual(name, "wlo1_ap")
        self.assertTrue(mac.startswith("02:"))
        self.assertTrue(sh.called("iw", "dev", "wlo1_ap", "del"))
        self.assertIn("separate MAC", err.getvalue())


HOOK = Path(__file__).resolve().parents[2] / "apsta_cli" / "data" / "apsta-sleep"


class SleepHookTests(unittest.TestCase):
    """The systemd/pm-utils sleep hook, run with fake apsta/systemctl/systemd-run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        for name in ("apsta", "systemctl", "systemd-run"):
            tool = self.bin / name
            tool.write_text(
                f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\n'
                f'case "{name} $*" in\n'
                f'  "systemctl is-active --quiet apsta.service") exit ${{SERVICE_ACTIVE:-1}} ;;\n'
                f'  "apsta status --check") exit ${{HOTSPOT_UP:-3}} ;;\n'
                "esac\nexit 0\n"
            )
            tool.chmod(0o755)

    def hook(self, *args, **env):
        full_env = {"PATH": f"{self.bin}:/usr/bin:/bin", "APSTA_RUN_DIR": str(self.run_dir), **env}
        result = subprocess.run(["sh", str(HOOK), *args], env=full_env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_does_nothing_when_the_service_manages_the_hotspot(self):
        calls = self.hook("pre", "suspend", SERVICE_ACTIVE="0", HOTSPOT_UP="0")
        self.assertEqual(calls, ["systemctl is-active --quiet apsta.service"])

    def test_stops_before_sleep_and_restarts_after(self):
        calls = self.hook("pre", "suspend", HOTSPOT_UP="0")
        self.assertIn("apsta stop", calls)
        self.assertTrue((self.run_dir / "resume-pending").exists())
        calls = self.hook("post", "suspend")
        self.assertIn("systemd-run --no-block --collect --unit=apsta-resume", calls[-1])
        self.assertIn("start --wait-sta 30", calls[-1])
        self.assertFalse((self.run_dir / "resume-pending").exists())

    def test_nothing_to_do_when_hotspot_was_off(self):
        self.assertNotIn("apsta stop", self.hook("pre", "suspend"))
        self.assertFalse(any("start" in c for c in self.hook("post", "suspend")))

    def test_pm_utils_arguments_and_unknown_ones(self):
        self.assertIn("apsta stop", self.hook("suspend", HOTSPOT_UP="0"))
        self.assertTrue(any("start" in c for c in self.hook("resume")))
        self.hook("bogus")  # must exit 0 without breaking the sleep sequence


if __name__ == "__main__":
    unittest.main()
