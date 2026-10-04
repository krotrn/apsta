"""Base class: a fresh fake world + fake toolchain per test, apsta run in-process as root."""

import io
import json
import os
import signal
import tempfile
import unittest
import unittest.mock as mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from apsta_cli import cli
from apsta_cli import state as state_store
from tests.integration import fakeworld
from tests.support import as_root, fixture, isolate_paths

HOME_24GHZ = {"ssid": "Home", "freq": 2437}


class FakeWorldTestCase(unittest.TestCase):
    phy = "iw/intel_alderlake.txt"
    link = HOME_24GHZ
    world_extra: dict = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        fakeworld.create(self.root, fixture(self.phy), link=self.link, **self.world_extra)
        fakeworld.install_tools(self.root / "bin")
        isolate_paths(self, self.root)  # fake sysfs lives inside the world root
        as_root(self)
        env = {"APSTA_PATH": str(self.root / "bin"), "APSTA_FAKE_ROOT": str(self.root), "APSTA_SUPERVISOR": "pidfile"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._kill_daemons)

    def _kill_daemons(self):
        for pidfile in (self.root / "run").glob("*.pid") if (self.root / "run").exists() else []:
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (ValueError, OSError):
                pass  # the daemon already exited

    # ── helpers ───────────────────────────────────────────────────────────────

    def apsta(self, *argv, stdin=None):
        """Run the real CLI; returns (exit_code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        stdin_patch = mock.patch("sys.stdin", io.StringIO(stdin)) if stdin is not None else mock.MagicMock()
        with redirect_stdout(out), redirect_stderr(err), stdin_patch:
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def apsta_json(self, *argv):
        code, out, err = self.apsta(*argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    @property
    def world(self):
        return fakeworld.load(self.root)

    def update_world(self, **changes):
        data = self.world
        data.update(changes)
        (self.root / "world.json").write_text(json.dumps(data))

    def state(self):
        return state_store.load()
