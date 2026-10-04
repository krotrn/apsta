import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from apsta_cli.core.errors import SetupError
from apsta_cli.net import supervisor
from tests.support import FakeShell

DAEMON = """#!{python}
import os, sys, time
pid = os.fork()
if pid:
    open(sys.argv[2], "w").write(str(pid))
    sys.exit(0)
os.setsid()
time.sleep(30)
"""


class PidfileSupervisorTests(unittest.TestCase):
    def test_start_running_stop(self):
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "fakedaemon"
            exe.write_text(DAEMON.format(python=sys.executable))
            exe.chmod(0o755)
            pidfile = Path(td) / "d.pid"
            d = supervisor.Daemon("fakedaemon", [str(exe)], [str(exe), "-P", str(pidfile)], pidfile)
            sup = supervisor.PidfileSupervisor()
            sup.start(d)
            for _ in range(50):
                if sup.running(d):
                    break
                time.sleep(0.05)
            self.assertTrue(sup.running(d))
            self.assertEqual(sup.logs(d), "")
            sup.stop(d)
            self.assertFalse(sup.running(d))
            self.assertFalse(pidfile.exists())

    def test_pidfile_pointing_at_other_process_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            pidfile = Path(td) / "d.pid"
            pidfile.write_text("1")  # init: never ours
            d = supervisor.Daemon("hostapd", [], [], pidfile)
            self.assertFalse(supervisor.PidfileSupervisor().running(d))
            with mock.patch("os.kill") as kill:
                supervisor.PidfileSupervisor().stop(d)
            kill.assert_not_called()

    def test_start_failure(self):
        with tempfile.TemporaryDirectory() as td:
            d = supervisor.Daemon("x", [], [sys.executable, "-c", "raise SystemExit(1)"], Path(td) / "p")
            with self.assertRaises(SetupError):
                supervisor.PidfileSupervisor().start(d)


class SystemdSupervisorTests(unittest.TestCase):
    def test_commands(self):
        sh = FakeShell().install(self)
        sh.on("systemctl", "is-active", rc=3)
        sh.on("journalctl", stdout="line1\nline2")
        d = supervisor.Daemon("hostapd", ["hostapd", "/run/c"], ["hostapd", "-B"], Path("/x"))
        sup = supervisor.SystemdSupervisor()
        sup.start(d)
        run = sh.matching("systemd-run")[0]
        self.assertIn("--unit=apsta-hostapd.service", run)
        self.assertEqual(run[-2:], ["hostapd", "/run/c"])
        self.assertFalse(sup.running(d))
        self.assertEqual(sup.logs(d), "line1\nline2")
        sup.stop(d)
        self.assertTrue(sh.called("systemctl", "stop", "apsta-hostapd.service"))

    def test_get(self):
        self.assertIsInstance(supervisor.get("systemd"), supervisor.SystemdSupervisor)
        self.assertIsInstance(supervisor.get("pidfile"), supervisor.PidfileSupervisor)
        FakeShell([]).install(self)
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("APSTA_SUPERVISOR", None)
            self.assertIsInstance(supervisor.get(), supervisor.PidfileSupervisor)


if __name__ == "__main__":
    unittest.main()
